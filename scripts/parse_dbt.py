"""Parse dbt Fusion artifacts into the unified graph (platform = "dbt").

Consumes, from a project's `target/` directory:
  - semantic_manifest.json : semantic models, entities, dimensions, measures,
                             metrics, saved_queries (fully resolved, with
                             `node_relation` physical back-links).
  - manifest.json          : model/source nodes, columns, model-level lineage
                             (`parent_map`).
  - catalog.json           : (optional) column data types.
  - column-level lineage   : (optional, best-effort) parquet under target/compiled/**/cll.

dbt is the spine of the merged graph: it is the only spec that carries a metric
composition graph and saved queries, so we resolve those edges here.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Any

import yaml

from graph_model import (
    Graph, PHYSICAL_TABLE, COLUMN, SEMANTIC_MODEL, ENTITY, DIMENSION, MEASURE,
    METRIC, SAVED_QUERY, DEPENDS_ON, DERIVED_FROM, JOINS, HAS_DIMENSION,
    HAS_MEASURE, HAS_ENTITY, BOUND_TO, MEASURE_OF, COMPOSED_OF, GROUPED_BY,
    normalize_name,
)

PLATFORM = "dbt"


def _load(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _rel(relation_name: str | None) -> str:
    return (relation_name or "").lower()


def _count_dbt_metrics(graph: Graph) -> int:
    return sum(1 for n in graph.nodes.values()
               if n.platform == PLATFORM and n.type == METRIC)


def parse_dbt_project(target_dir: str, graph: Graph, warn: list[str]) -> None:
    """Parse one project's target/ dir into `graph`. Missing files are tolerated.

    If `semantic_manifest.json` yields no metrics (e.g. the project uses the legacy
    semantic YAML that Fusion doesn't compile into artifacts), fall back to scanning
    the project's raw semantic YAML so the dbt side is still captured.
    """
    manifest = _load(os.path.join(target_dir, "manifest.json"))
    sem = _load(os.path.join(target_dir, "semantic_manifest.json"))
    catalog = _load(os.path.join(target_dir, "catalog.json"))

    project_dir = os.path.dirname(os.path.normpath(target_dir))

    if manifest is None and sem is None:
        warn.append(
            f"[dbt] no manifest.json or semantic_manifest.json in {target_dir}; "
            "scanning raw semantic YAML instead"
        )
        scan_project_semantic_yaml(project_dir, graph, warn)
        return

    if manifest:
        _parse_manifest(manifest, catalog, graph)

    before = _count_dbt_metrics(graph)
    if sem:
        _parse_semantic_manifest(sem, graph, warn)
    if _count_dbt_metrics(graph) == before:
        # semantic_manifest had no metrics for this project — use raw YAML
        n = scan_project_semantic_yaml(project_dir, graph, warn)
        if n:
            warn.append(
                f"[dbt] semantic_manifest.json had no metrics; recovered {n} from raw "
                f"YAML in {project_dir} (legacy spec — consider migrating to current spec)"
            )

    _load_cll(target_dir, graph, warn)


# ---- manifest.json: physical layer + lineage --------------------------------

def _parse_manifest(manifest: dict, catalog: dict | None, graph: Graph) -> None:
    nodes = manifest.get("nodes", {})
    sources = manifest.get("sources", {})

    # relation_name (lowercased) -> physical table node id, for semantic back-links
    rel_index: dict[str, str] = {}
    uid_to_rel: dict[str, str] = {}

    catalog_nodes = (catalog or {}).get("nodes", {}) if catalog else {}

    def add_table(uid: str, obj: dict, is_source: bool) -> None:
        rel = _rel(obj.get("relation_name"))
        if not rel:
            return
        t = graph.node(
            PHYSICAL_TABLE, PLATFORM, rel, obj.get("name", rel),
            relation_name=rel,
            database=obj.get("database"),
            schema=obj.get("schema"),
            is_source=is_source,
            dbt_unique_id=uid,
        )
        rel_index[rel] = t.id
        uid_to_rel[uid] = rel
        # columns
        cat = catalog_nodes.get(uid, {})
        cat_cols = cat.get("columns", {}) if cat else {}
        for cname, cmeta in (obj.get("columns") or {}).items():
            col_type = None
            if cname in cat_cols:
                col_type = cat_cols[cname].get("type")
            c = graph.node(
                COLUMN, PLATFORM, f"{rel}.{cname.lower()}", cname,
                table=rel, data_type=col_type,
                description=(cmeta or {}).get("description"),
            )
            graph.add_edge("HAS_COLUMN", t.id, c.id)

    for uid, obj in nodes.items():
        if obj.get("resource_type") == "model":
            add_table(uid, obj, is_source=False)
    for uid, obj in sources.items():
        add_table(uid, obj, is_source=True)

    # model-level lineage from parent_map (child -> [parents])
    for child_uid, parents in (manifest.get("parent_map") or {}).items():
        child_rel = uid_to_rel.get(child_uid)
        if not child_rel:
            continue
        for parent_uid in parents:
            parent_rel = uid_to_rel.get(parent_uid)
            if parent_rel:
                graph.add_edge(
                    DEPENDS_ON, rel_index[child_rel], rel_index[parent_rel]
                )

    graph.metadata.setdefault("dbt_relation_index", {}).update(rel_index)


# ---- semantic_manifest.json: semantic layer ---------------------------------

def _parse_semantic_manifest(sem: dict, graph: Graph, warn: list[str]) -> None:
    rel_index = graph.metadata.get("dbt_relation_index", {})

    # measure name -> measure node id (metrics reference measures by name)
    measure_index: dict[str, str] = {}
    # entity name -> list of (semantic_model node id, entity type)
    entity_occurrences: dict[str, list[tuple[str, str]]] = {}

    for sm in sem.get("semantic_models", []):
        sm_name = sm.get("name")
        sm_node = graph.node(
            SEMANTIC_MODEL, PLATFORM, sm_name, sm_name,
            description=sm.get("description"),
            label=sm.get("label"),
        )
        # bind to physical table
        nr = sm.get("node_relation") or {}
        rel = _rel(nr.get("relation_name"))
        if rel:
            table_id = rel_index.get(rel) or graph.node(
                PHYSICAL_TABLE, PLATFORM, rel, nr.get("alias", rel),
                relation_name=rel, database=nr.get("database"),
                schema=nr.get("schema_name"),
            ).id
            graph.add_edge(BOUND_TO, sm_node.id, table_id)
            sm_node.props["relation_name"] = rel

        for ent in sm.get("entities", []):
            e = graph.node(
                ENTITY, PLATFORM, f"{sm_name}.{ent['name']}", ent["name"],
                entity_type=ent.get("type"), expr=ent.get("expr"),
            )
            graph.add_edge(HAS_ENTITY, sm_node.id, e.id)
            entity_occurrences.setdefault(ent["name"], []).append(
                (sm_node.id, ent.get("type"))
            )

        for dim in sm.get("dimensions", []):
            tp = dim.get("type_params") or {}
            graph.node(
                DIMENSION, PLATFORM, f"{sm_name}.{dim['name']}", dim["name"],
                dimension_type=dim.get("type"),
                time_granularity=tp.get("time_granularity"),
                expr=dim.get("expr"),
                display_name=dim.get("label"),
                description=dim.get("description"),
            )
            graph.add_edge(
                HAS_DIMENSION, sm_node.id, f"{PLATFORM}:{DIMENSION}:{sm_name}.{dim['name']}"
            )

        for mea in sm.get("measures", []):
            m = graph.node(
                MEASURE, PLATFORM, f"{sm_name}.{mea['name']}", mea["name"],
                agg=mea.get("agg"), expr=mea.get("expr"),
                canonical_expr=f"{mea.get('agg')}({mea.get('expr')})",
                display_name=mea.get("label"),
                description=mea.get("description"),
                has_filter=False,
            )
            graph.add_edge(HAS_MEASURE, sm_node.id, m.id)
            measure_index[mea["name"]] = m.id

    # dbt joins: same entity as primary in one model, foreign in another
    for ent_name, occ in entity_occurrences.items():
        primaries = [nid for nid, t in occ if t == "primary"]
        foreigns = [nid for nid, t in occ if t == "foreign"]
        for f_id in foreigns:
            for p_id in primaries:
                if f_id != p_id:
                    graph.add_edge(
                        JOINS, f_id, p_id,
                        cardinality="many_to_one", on_entity=ent_name,
                        inferred=True,
                    )

    _parse_metrics(sem, graph, measure_index)
    _parse_saved_queries(sem, graph)


def _parse_metrics(sem: dict, graph: Graph, measure_index: dict[str, str]) -> None:
    for mt in sem.get("metrics", []):
        name = mt.get("name")
        mtype = mt.get("type")
        tp = mt.get("type_params") or {}
        canonical = None
        has_filter = bool(mt.get("filter"))

        node = graph.node(
            METRIC, PLATFORM, name, name,
            metric_type=mtype,
            description=mt.get("description"),
            display_name=mt.get("label"),
        )

        if mtype == "simple":
            measure = tp.get("measure") or {}
            mref = measure.get("name")
            if measure.get("filter"):
                has_filter = True
            if mref and mref in measure_index:
                graph.add_edge(MEASURE_OF, node.id, measure_index[mref])
                mnode = graph.nodes[measure_index[mref]]
                canonical = mnode.props.get("canonical_expr")
        elif mtype == "ratio":
            num = (tp.get("numerator") or {}).get("name")
            den = (tp.get("denominator") or {}).get("name")
            canonical = f"{num} / {den}"
            for ref in (num, den):
                if ref:
                    graph.add_edge(COMPOSED_OF, node.id, f"{PLATFORM}:{METRIC}:{ref}")
        elif mtype in ("derived", "cumulative", "conversion"):
            canonical = tp.get("expr")
            for ref in tp.get("metrics", []) or []:
                graph.add_edge(
                    COMPOSED_OF, node.id, f"{PLATFORM}:{METRIC}:{ref.get('name')}",
                    alias=ref.get("alias"),
                    offset_window=ref.get("offset_window"),
                )

        node.props["canonical_expr"] = canonical
        node.props["has_filter"] = has_filter


def _parse_saved_queries(sem: dict, graph: Graph) -> None:
    for sq in sem.get("saved_queries", []):
        name = sq.get("name")
        qp = sq.get("query_params") or {}
        node = graph.node(
            SAVED_QUERY, PLATFORM, name, name,
            description=sq.get("description"),
            group_by=qp.get("group_by"),
        )
        for metric_name in qp.get("metrics", []) or []:
            graph.add_edge(
                GROUPED_BY, node.id, f"{PLATFORM}:{METRIC}:{metric_name}"
            )


# ---- column-level lineage (best-effort) -------------------------------------

def _load_cll(target_dir: str, graph: Graph, warn: list[str]) -> None:
    """Best-effort read of Fusion column-level-lineage parquet into DERIVED_FROM.

    CLL is produced by:
        dbt compile --write-metadata --write-lineage --static-analysis strict
    which writes parquet under target/compiled/**/cll. The parquet schema is not
    guaranteed stable; we read defensively and skip if pyarrow is unavailable or
    no files exist. The live LSP path (lsp_client.py) is the alternative source.
    """
    parquet_files = glob.glob(
        os.path.join(target_dir, "**", "cll", "**", "*.parquet"), recursive=True
    )
    if not parquet_files:
        return
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        warn.append(
            "[dbt] found CLL parquet but pyarrow is not installed; skipping "
            "column-level lineage (use --lsp-socket for live CLL instead)"
        )
        return

    added = 0
    for pf in parquet_files:
        try:
            table = pq.read_table(pf)
            rows = table.to_pylist()
        except Exception as exc:  # noqa: BLE001
            warn.append(f"[dbt] could not read CLL parquet {pf}: {exc}")
            continue
        for row in rows:
            src = _first(row, ("source_column", "upstream_column", "from_column", "parent"))
            tgt = _first(row, ("target_column", "downstream_column", "to_column", "child"))
            if src and tgt:
                s = graph.node(COLUMN, PLATFORM, str(src).lower(), str(src))
                t = graph.node(COLUMN, PLATFORM, str(tgt).lower(), str(tgt))
                graph.add_edge(DERIVED_FROM, t.id, s.id, source="cll_parquet")
                added += 1
    if added:
        graph.metadata["dbt_cll_edges"] = added


def _first(row: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in row and row[k]:
            return row[k]
    return None


# ---- raw semantic YAML (fallback / legacy spec) -----------------------------

_REF_RE = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]")


def scan_project_semantic_yaml(project_dir: str, graph: Graph, warn: list[str]) -> int:
    """Scan a dbt project's YAML for raw `semantic_models:`/`metrics:` and parse them.

    Used when compiled artifacts don't carry the semantic layer. Returns the number
    of metrics recovered. Node ids match the manifest scheme, so if both are present
    they merge cleanly.
    """
    recovered = 0
    for pattern in ("models/**/*.yml", "models/**/*.yaml"):
        for path in sorted(glob.glob(os.path.join(project_dir, pattern), recursive=True)):
            try:
                with open(path) as f:
                    doc = yaml.safe_load(f)
            except (yaml.YAMLError, OSError) as exc:
                warn.append(f"[dbt] could not read semantic YAML {path}: {exc}")
                continue
            if not isinstance(doc, dict):
                continue
            if "semantic_models" not in doc and "metrics" not in doc:
                continue
            recovered += _parse_semantic_yaml_doc(doc, graph)
    return recovered


def _resolve_ref_table(model_expr: str | None, graph: Graph) -> str | None:
    """Map a `ref('name')` to an existing dbt PhysicalTable node id, or make one."""
    if not model_expr:
        return None
    m = _REF_RE.search(model_expr)
    name = m.group(1) if m else model_expr
    for node in graph.nodes.values():
        if node.platform != PLATFORM or node.type != PHYSICAL_TABLE:
            continue
        uid = node.props.get("dbt_unique_id", "")
        if uid.startswith("model.") and uid.rsplit(".", 1)[-1] == name:
            return node.id
        if node.name == name:
            return node.id
    return graph.node(
        PHYSICAL_TABLE, PLATFORM, f"ref:{name}", name, ref=name, resolved=False
    ).id


def _parse_semantic_yaml_doc(doc: dict, graph: Graph) -> int:
    measure_index: dict[str, str] = {}
    entity_occurrences: dict[str, list[tuple[str, str]]] = {}

    for sm in doc.get("semantic_models", []) or []:
        sm_name = sm.get("name")
        sm_node = graph.node(
            SEMANTIC_MODEL, PLATFORM, sm_name, sm_name,
            description=sm.get("description"), label=sm.get("label"),
            spec="raw_yaml",
        )
        table_id = _resolve_ref_table(sm.get("model"), graph)
        if table_id:
            graph.add_edge(BOUND_TO, sm_node.id, table_id)

        for ent in sm.get("entities", []) or []:
            e = graph.node(
                ENTITY, PLATFORM, f"{sm_name}.{ent['name']}", ent["name"],
                entity_type=ent.get("type"), expr=ent.get("expr"),
            )
            graph.add_edge(HAS_ENTITY, sm_node.id, e.id)
            entity_occurrences.setdefault(ent["name"], []).append(
                (sm_node.id, ent.get("type"))
            )

        for dim in sm.get("dimensions", []) or []:
            tp = dim.get("type_params") or {}
            node = graph.node(
                DIMENSION, PLATFORM, f"{sm_name}.{dim['name']}", dim["name"],
                dimension_type=dim.get("type"),
                time_granularity=tp.get("time_granularity"),
                expr=dim.get("expr"), display_name=dim.get("label"),
                description=dim.get("description"),
            )
            graph.add_edge(HAS_DIMENSION, sm_node.id, node.id)

        for mea in sm.get("measures", []) or []:
            m = graph.node(
                MEASURE, PLATFORM, f"{sm_name}.{mea['name']}", mea["name"],
                agg=mea.get("agg"), expr=mea.get("expr"),
                canonical_expr=f"{mea.get('agg')}({mea.get('expr')})",
                display_name=mea.get("label"), description=mea.get("description"),
                has_filter=False,
            )
            graph.add_edge(HAS_MEASURE, sm_node.id, m.id)
            measure_index[mea["name"]] = m.id

    for ent_name, occ in entity_occurrences.items():
        primaries = [nid for nid, t in occ if t == "primary"]
        foreigns = [nid for nid, t in occ if t == "foreign"]
        for f_id in foreigns:
            for p_id in primaries:
                if f_id != p_id:
                    graph.add_edge(JOINS, f_id, p_id, cardinality="many_to_one",
                                   on_entity=ent_name, inferred=True)

    count = 0
    for mt in doc.get("metrics", []) or []:
        _parse_yaml_metric(mt, graph, measure_index)
        count += 1
    return count


def _parse_yaml_metric(mt: dict, graph: Graph, measure_index: dict[str, str]) -> None:
    name = mt.get("name")
    mtype = mt.get("type")
    tp = mt.get("type_params") or {}
    canonical = None
    has_filter = bool(mt.get("filter"))

    node = graph.node(
        METRIC, PLATFORM, name, name,
        metric_type=mtype, description=mt.get("description"),
        display_name=mt.get("label"),
    )

    if mtype == "simple":
        measure = tp.get("measure")
        mref = measure.get("name") if isinstance(measure, dict) else measure
        if isinstance(measure, dict) and measure.get("filter"):
            has_filter = True
        if mref and mref in measure_index:
            graph.add_edge(MEASURE_OF, node.id, measure_index[mref])
            canonical = graph.nodes[measure_index[mref]].props.get("canonical_expr")
    elif mtype == "ratio":
        num = tp.get("numerator")
        den = tp.get("denominator")
        num_name = num.get("name") if isinstance(num, dict) else num
        den_name = den.get("name") if isinstance(den, dict) else den
        if (isinstance(num, dict) and num.get("filter")) or \
           (isinstance(den, dict) and den.get("filter")):
            has_filter = True
        canonical = f"{num_name} / {den_name}"
        for ref in (num_name, den_name):
            if ref:
                graph.add_edge(COMPOSED_OF, node.id, f"{PLATFORM}:{METRIC}:{ref}")
    elif mtype in ("derived", "cumulative", "conversion"):
        canonical = tp.get("expr")
        for ref in tp.get("metrics", []) or []:
            ref_name = ref.get("name") if isinstance(ref, dict) else ref
            graph.add_edge(COMPOSED_OF, node.id, f"{PLATFORM}:{METRIC}:{ref_name}")

    node.props["canonical_expr"] = canonical
    node.props["has_filter"] = has_filter
