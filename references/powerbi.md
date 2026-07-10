# Power BI → unified graph

How `scripts/parse_powerbi.py` turns a Power BI metadata dump into graph nodes and
edges, and how it bridges into the dbt spine. Load this when working on the Power BI
parser or debugging Power BI lineage.

## Input: the metadata dump directory

The parser reads the directory produced by `powerbi/pull_powerbi_metadata.py`
(default `metadata_output/`), **not** a single file. It uses:

| File | Used for |
|---|---|
| `datasets/<dataset_id>.json` | tables, columns, measures, relationships, and the Snowflake datasource binding (`server;database`) |
| `reports.json` | one `SavedQuery` per report, and the report→dataset edge |
| `report_column_usage.json` *(optional)* | which columns/measures each report actually references (`GROUPED_BY` edges) |

The `model` block inside each dataset file is the DAX `INFO.VIEW.*` output:
`tables`, `columns`, `measures`, `relationships`, each as `{"rows": [...]}` with
bracketed keys (`[Name]`, `[Table]`, `[Expression]`, `[FromTable]`, …).

## Mapping

| Power BI | Graph | Notes |
|---|---|---|
| dataset | `SemanticModel` (`powerbi:SemanticModel:<dataset_id>`) | props: `workspace`, `database`, `server` |
| table | `PhysicalTable` (`powerbi:PhysicalTable:<database>.<table>`) | `relation_name`/`database`/`table`; `BOUND_TO` from the dataset |
| column | `Column` (`…:<database>.<table>.<col>`) | `HAS_COLUMN` from its table; carries `data_type` |
| DAX measure | `Measure` | `canonical_expr` = the DAX; `HAS_MEASURE` from the dataset |
| relationship | `JOINS` (from many-side → one-side) | props: `active`, `left_columns`, `right_columns` |
| report | `SavedQuery` (`powerbi:SavedQuery:<report_id>`) | `web_url`, `dataset_id`; `DEPENDS_ON` → its dataset |
| report field usage | `GROUPED_BY` (report → column / measure) | resolved from `report_column_usage.json` |

### What is dropped

- **Auto date tables** — `LocalDateTable_*` and `DateTableTemplate_*` are Power BI
  lineage noise (one per Date column) and are skipped for tables, columns, and
  relationships.
- **Hidden columns** and internal `RowNumber` columns (`[Type] == "RowNumber"` or
  `[DataCategory] == "RowNumber"`).

## The physical binding (no schema)

Power BI exposes a table's warehouse origin only through the dataset's datasource
`connectionDetails.path`, which is `server;database` — there is **no schema**. So a
table's physical id is `<database>.<table>` lowercased (or just `<table>` when no
Snowflake datasource is found). This is deliberately the same shape the standalone
`powerbi/dbt_lineage.py` uses to match models.

## Bridging to dbt: `link_to_dbt()`

Because there is no schema, a Power BI `PhysicalTable` cannot share an id with a dbt
`PhysicalTable` (whose id is the full `database.schema.identifier` relation). Instead,
after all parsing, `link_to_dbt()`:

1. Indexes every dbt `PhysicalTable` by `(database, trailing-identifier)` — the last
   `.`-segment of `relation_name`, quotes stripped, lowercased.
2. For each Power BI `PhysicalTable`, looks up `(database, table)` and adds a
   `DEPENDS_ON` edge (Power BI table → dbt relation) with `match="db+identifier"`.

`build_graph.py` calls this automatically and records the count in
`metadata.powerbi_dbt_links`. The result: a report traces
`SavedQuery → SemanticModel → PhysicalTable → (dbt relation) → … → dbt sources`.

## Drift

Power BI `Measure` nodes participate in cross-platform drift like any other measure:
they are compared by `normalized_name`. A DAX `canonical_expr` will not textually
match a SQL one, so a same-named dbt/Snowflake measure vs a Power BI DAX measure
surfaces as an `expression_mismatch` — which is usually the point (the definitions
*are* expressed differently and are worth reviewing). Datasets in this dump had no
model measures, so no measure drift is emitted for them.

## Not modeled

- Row-level security, calculation groups, and perspectives.
- Power Query "M" table→source mapping at the column level (the raw `INFO.PARTITIONS`
  / `INFO.EXPRESSIONS` tables are blocked for non-admin tokens; see
  `powerbi/pbi_client.py`). The database-level binding above is what a delegated
  user token can see.
