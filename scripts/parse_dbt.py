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
from typing import Any

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


def parse_dbt_project(target_dir: str, graph: Graph, warn: list[str]) -> None:
    """Parse one project's target/ dir into `graph`. Missing files are tolerated."""
    manifest = _load(os.path.join(target_dir, "manifest.json"))
    sem = _load(os.path.join(target_dir, "semantic_manifest.json"))
    catalog = _load(os.path.join(target_dir, "catalog.json"))

    if manifest is None and sem is None:
        warn.append(f"[dbt] no manifest.json or semantic_manifest.json in {target_dir}")
        return

    if manifest:
        _parse_manifest(manifest, catalog, graph)
    if sem:
        _parse_semantic_manifest(sem, graph, warn)

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
