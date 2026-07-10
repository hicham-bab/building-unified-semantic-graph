"""Parse Power BI metadata into the unified graph (platform = "powerbi").

Input is the directory produced by `powerbi/pull_powerbi_metadata.py`
(`metadata_output/` by default), *not* a single file — Power BI metadata is a
tree of JSON: `reports.json`, `datasets/<id>.json` (tables / columns / measures /
relationships pulled via DAX `INFO.VIEW.*`), and an optional
`report_column_usage.json` (which report touches which fields).

How the concepts map onto the unified graph:

    dataset            -> SemanticModel          (a Power BI semantic model)
    table              -> PhysicalTable          (a warehouse relation)
    column             -> Column                 (HAS_COLUMN of the table)
    measure (DAX)      -> Measure                (canonical_expr = the DAX)
    relationship       -> JOINS                  (from many-side to one-side)
    report             -> SavedQuery             (a consumer of the dataset)
    report field usage -> GROUPED_BY             (report -> column / measure)

Power BI exposes a table's warehouse binding only as `server;database` (no
schema), so a table's physical id is `<database>.<table>` lowercased when a
database is known, else just the table name. `link_to_dbt()` uses that to draw
`DEPENDS_ON` edges from a Power BI table to the dbt-built relation of the same
database + identifier — the bridge that makes dbt the spine for Power BI too.
"""

from __future__ import annotations

import glob
import json
import os

from graph_model import (
    Graph, PHYSICAL_TABLE, COLUMN, SEMANTIC_MODEL, MEASURE, SAVED_QUERY,
    JOINS, HAS_COLUMN, HAS_MEASURE, BOUND_TO, DEPENDS_ON, GROUPED_BY,
)

PLATFORM = "powerbi"

# Power BI auto-creates a hidden date table per Date column; lineage noise.
_AUTO_TABLE_PREFIXES = ("LocalDateTable", "DateTableTemplate")


def _is_auto_table(name: str | None) -> bool:
    return bool(name) and name.startswith(_AUTO_TABLE_PREFIXES)


def _load(path: str) -> object | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _rows(model: dict, key: str) -> list[dict]:
    return (model.get(key) or {}).get("rows", []) or []


def _snowflake_database(detail: dict) -> tuple[str | None, str | None]:
    """Best-effort (server, database) from a dataset's Snowflake datasource."""
    for s in detail.get("datasources") or []:
        cd = s.get("connectionDetails", {}) if isinstance(s, dict) else {}
        if (cd.get("kind") or "").lower() == "snowflake" and cd.get("path"):
            parts = cd["path"].split(";")
            return parts[0], (parts[1] if len(parts) > 1 else None)
    return None, None


def _table_key(database: str | None, table: str) -> str:
    """Physical id qualifier: `<database>.<table>` lowercased, or the table."""
    return (f"{database}.{table}" if database else table).lower()


def parse_powerbi_dir(metadata_dir: str, graph: Graph, warn: list[str]) -> None:
    """Parse a Power BI metadata dump directory into `graph`."""
    metadata_dir = os.path.expanduser(metadata_dir)
    if not os.path.isdir(metadata_dir):
        warn.append(f"[powerbi] not a directory: {metadata_dir}")
        return

    reports = _load(os.path.join(metadata_dir, "reports.json")) or []
    usage = _load(os.path.join(metadata_dir, "report_column_usage.json")) or {}

    # dataset_id -> {"sm": node_id, "database": str, "tables": {table_name: node_id}}
    ds_context: dict[str, dict] = {}

    dataset_files = sorted(glob.glob(os.path.join(metadata_dir, "datasets", "*.json")))
    if not dataset_files:
        warn.append(f"[powerbi] no datasets/*.json under {metadata_dir}")

    for path in dataset_files:
        _parse_dataset(_load(path), graph, warn, ds_context, origin=path)

    _parse_reports(reports, usage, graph, ds_context)


def _parse_dataset(detail, graph, warn, ds_context, origin) -> None:
    if not isinstance(detail, dict):
        warn.append(f"[powerbi] unreadable dataset file: {origin}")
        return
    ds_id = detail.get("dataset_id") or detail.get("dataset", {}).get("id") or origin
    ds_name = detail.get("dataset", {}).get("name", ds_id)
    server, database = _snowflake_database(detail)
    model = detail.get("model") or {}

    sm = graph.node(
        SEMANTIC_MODEL, PLATFORM, ds_id, ds_name,
        kind="powerbi_dataset", workspace=detail.get("home_workspace"),
        database=database, server=server, origin=origin,
    )

    ctx = ds_context.setdefault(ds_id, {"sm": sm.id, "database": database, "tables": {}})

    # tables -> physical relations
    for t in _rows(model, "tables"):
        name = t.get("[Name]")
        if _is_auto_table(name) or t.get("[IsHidden]"):
            continue
        key = _table_key(database, name)
        table = graph.node(
            PHYSICAL_TABLE, PLATFORM, key, name,
            relation_name=key, database=database, table=name,
            dataset_id=ds_id, server=server, is_source=False,
        )
        graph.add_edge(BOUND_TO, sm.id, table.id)
        ctx["tables"][name] = table.id

    # columns -> Column nodes hung off their table
    for c in _rows(model, "columns"):
        tname = c.get("[Table]")
        if tname not in ctx["tables"] or c.get("[IsHidden]"):
            continue
        if c.get("[Type]") == "RowNumber" or c.get("[DataCategory]") == "RowNumber":
            continue
        cname = c.get("[Name]")
        key = _table_key(database, tname)
        col = graph.node(
            COLUMN, PLATFORM, f"{key}.{str(cname).lower()}", cname,
            table=key, data_type=c.get("[DataType]"),
            description=c.get("[Description]"),
        )
        graph.add_edge(HAS_COLUMN, ctx["tables"][tname], col.id)

    # measures (DAX) -> Measure nodes
    for m in _rows(model, "measures"):
        mname = m.get("[Name]")
        if not mname:
            continue
        expr = m.get("[Expression]")
        measure = graph.node(
            MEASURE, PLATFORM, f"{ds_id}.{mname}", mname,
            canonical_expr=expr, expr=expr, has_filter=False,
            table=m.get("[Table]"), display_name=mname,
            description=m.get("[Description]"),
        )
        graph.add_edge(HAS_MEASURE, sm.id, measure.id)

    # relationships -> JOINS (from = many side, to = one side)
    for r in _rows(model, "relationships"):
        frm, to = r.get("[FromTable]"), r.get("[ToTable]")
        if _is_auto_table(frm) or _is_auto_table(to):
            continue
        if frm not in ctx["tables"] or to not in ctx["tables"]:
            continue
        graph.add_edge(
            JOINS, ctx["tables"][frm], ctx["tables"][to],
            cardinality="many_to_one", active=r.get("[IsActive]", True),
            left_columns=[r.get("[FromColumn]")], right_columns=[r.get("[ToColumn]")],
            name=r.get("[Name]"),
        )


def _parse_reports(reports, usage, graph, ds_context) -> None:
    for r in reports:
        rid = r.get("id")
        if not rid:
            continue
        ds_id = r.get("datasetId")
        sq = graph.node(
            SAVED_QUERY, PLATFORM, rid, r.get("name", rid),
            kind="powerbi_report", web_url=r.get("webUrl"),
            dataset_id=ds_id, workspace=r.get("containingWorkspaceId"),
        )
        ctx = ds_context.get(ds_id)
        if ctx:
            graph.add_edge(DEPENDS_ON, sq.id, ctx["sm"])

        # report field usage -> GROUPED_BY the exact columns / measures used
        rusage = usage.get(rid) if isinstance(usage, dict) else None
        tabs = rusage.get("tables") if isinstance(rusage, dict) else None
        if not (tabs and ctx):
            continue
        database = ctx["database"]
        for tname, fields in tabs.items():
            if _is_auto_table(tname):
                continue
            key = _table_key(database, tname)
            for col in fields.get("columns", []):
                graph.add_edge(
                    GROUPED_BY, sq.id, f"{PLATFORM}:{COLUMN}:{key}.{str(col).lower()}",
                    field=f"{tname}.{col}", kind="column",
                )
            for meas in fields.get("measures", []):
                graph.add_edge(
                    GROUPED_BY, sq.id, f"{PLATFORM}:{MEASURE}:{ds_id}.{meas}",
                    field=f"{tname}.{meas}", kind="measure",
                )


def link_to_dbt(graph: Graph, warn: list[str]) -> int:
    """Bridge Power BI tables to dbt-built relations.

    Power BI has no schema, so we match a Power BI PhysicalTable to a dbt
    PhysicalTable by (database, trailing identifier), case-insensitively, and add
    a `DEPENDS_ON` edge (Power BI table depends on the dbt relation that builds
    it). Returns the number of edges added.
    """
    dbt_by_db_ident: dict[tuple[str, str], list[str]] = {}
    for n in graph.nodes.values():
        if n.platform != "dbt" or n.type != PHYSICAL_TABLE:
            continue
        db = (n.props.get("database") or "").lower()
        ident = (n.props.get("relation_name") or "").split(".")[-1].strip('"').lower()
        if ident:
            dbt_by_db_ident.setdefault((db, ident), []).append(n.id)

    if not dbt_by_db_ident:
        return 0

    added = 0
    for n in list(graph.nodes.values()):
        if n.platform != PLATFORM or n.type != PHYSICAL_TABLE:
            continue
        db = (n.props.get("database") or "").lower()
        ident = (n.props.get("table") or "").lower()
        for dbt_id in dbt_by_db_ident.get((db, ident), []):
            graph.add_edge(DEPENDS_ON, n.id, dbt_id, match="db+identifier")
            added += 1
    return added
