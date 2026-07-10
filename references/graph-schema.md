# Unified property-graph schema

The graph is a single JSON document:

```json
{
  "metadata": { "node_count": 349, "edge_count": 460,
                "platforms": ["dbt","snowflake","databricks"],
                "sources": ["dbt:.../target", "snowflake:.../view.sql", ...],
                "warnings": [...], "drift_summary": {...} },
  "nodes": [ { "id","type","name","platform","normalized_name","props" }, ... ],
  "edges": [ { "type","source","target","props" }, ... ]
}
```

Every node/edge carries a `platform` provenance tag (`dbt` | `snowflake` |
`databricks` | `osi` | `lookml` | `powerbi`). Node ids are stable and readable:
`"<platform>:<type>:<qualifier>"`,
e.g. `dbt:Metric:total_gross_revenue`,
`snowflake:PhysicalTable:atlas_platform.marts_core.fct_orders`. `normalized_name`
(lowercased, non-alphanumerics collapsed to `_`) is the cross-platform match key.

## Node types

| Type | Meaning | Key props |
|---|---|---|
| `PhysicalTable` | Warehouse table/view a spec binds to | `relation_name`, `database`, `schema`, `is_source`, `dbt_unique_id` |
| `Column` | A physical column (or a Snowflake FACT) | `table`, `data_type`, `is_fact` |
| `SemanticModel` | dbt semantic_model / Snowflake semantic view / Databricks metric view | `kind`, `relation_name`/`source`, `description` |
| `Entity` | Join key (primary/foreign) | `entity_type`, `expr` |
| `Dimension` | Grouping attribute | `dimension_type`, `time_granularity`, `display_name`, `synonyms`, `format` |
| `Measure` | A named aggregation (dbt measure, Snowflake/Databricks measure) | `agg`, `expr`, `canonical_expr`, `has_filter`, `synonyms`, `format` |
| `Metric` | A governed metric (dbt only, first-class) | `metric_type` (simple/ratio/derived/…), `canonical_expr`, `has_filter` |
| `SavedQuery` | A metric+group_by bundle (dbt only) | `group_by`, `description` |

`canonical_expr` is the comparison string: for a dbt simple metric it is
`agg(measure_expr)`; for a ratio it is `numerator / denominator`; for Snowflake/
Databricks measures it is the raw aggregation SQL. `has_filter` is `true` only when
dbt attaches an explicit metric/measure filter (others inline filters into `expr`).

## Edge types

| Type | Direction | Meaning |
|---|---|---|
| `DEPENDS_ON` | model → upstream model/source | dbt model-level lineage (`parent_map`) |
| `HAS_COLUMN` | table → column | physical schema |
| `DERIVED_FROM` | column → upstream column | column-level lineage (CLL parquet or live LSP) |
| `BOUND_TO` | semantic model → physical table | which table a spec sits on |
| `HAS_ENTITY` / `HAS_DIMENSION` / `HAS_MEASURE` | semantic model → part | composition of a spec |
| `JOINS` | table/model → table/model | join with `cardinality`; `inferred=true` for dbt shared-entity joins, `false` for Snowflake `RELATIONSHIPS` |
| `MEASURE_OF` | metric → measure | dbt metric's input measure |
| `COMPOSED_OF` | metric → metric | dbt ratio/derived inputs (`alias`, `offset_window`) |
| `GROUPED_BY` | saved query → metric/dimension | dbt saved query members |
| `EQUIVALENT_TO` | node ↔ node | cross-platform concept match (`concept`, `match`) |
| `DRIFT` | node → node | inconsistency; `props.subtype` = `missing_on_platform` / `expression_mismatch` / `filter_mismatch` / `grain_mismatch` |

`missing_on_platform` DRIFT edges are self-loops (source == target) on a
representative node, with `props.missing_platform`; all other DRIFT edges connect the
two mismatched nodes. `EQUIVALENT_TO` edges are undirected in meaning (stored once
per platform pair).

## Extending

Add node/edge type constants in `scripts/graph_model.py` and emit them from the
relevant parser. Keep the id scheme (`platform:type:qualifier`) so `Graph.add_node`
de-duplication and cross-platform matching keep working.
