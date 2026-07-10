# Spec crosswalk — how each platform maps to the shared concepts

Six spec formats feed the graph. They share a core (physical table, dimension,
measure/metric) and diverge on the extras. This table drives the parsers.

| Concept | dbt MetricFlow | Snowflake Semantic View | Databricks Metric View | OSI core-spec | LookML | Power BI |
|---|---|---|---|---|---|---|
| **Physical table** | `model: ref()` + resolved `node_relation` | `TABLES(...)` FQ names | `source:` FQ UC table | `dataset.source` | `sql_table_name` | dataset table → `<database>.<table>` (no schema) |
| **Semantic container** | semantic_model | semantic view (multi-table) | metric view (1 table) | semantic_model → datasets | view | dataset |
| **Dimension** | `dimensions` (time/categorical) | `DIMENSIONS(...)` | `dimensions` (+`format`,`synonyms`) | `dataset.fields` (+`dimension.is_time`) | `dimension` / `dimension_group` | table `Column` (no semantic dim layer) |
| **Measure / metric** | `measures` + first-class `metrics` | `MEASURES ... AS <sql>` | `measures` (`expr`) | `metrics` (`expression.dialects`) | `measure` (`type`+`sql`) | DAX `Measure` (`canonical_expr` = DAX) |
| **Raw fact column** | — (measure `expr`) | `FACTS(...)` split | — | field expression | dimension `sql` | `Column` |
| **Join / entity** | `entities` primary/foreign (inferred) | `RELATIONSHIPS ... MANY TO ONE` | none (single-source) | `relationships` (`from`→many, `to`→one) | `explore` `join` + `relationship` | model `relationships` (many→one) |
| **Metric composition** | ratio/derived, offset, filter | inlined into `expr` | inlined into `expr` | inlined into expression | inlined into `sql` (or `type`) | inlined into DAX |
| **Saved query** | `saved_queries` | — | — | — | — (Looks / dashboards, not parsed) | report → `SavedQuery` (+`GROUPED_BY` field usage) |
| **NL / presentation** | `label`, `description` | `LABEL`, `COMMENT` | `display_name`, `format`, `synonyms` | `description`, `ai_context` | `label`, `group_label` | column/measure `Description` |
| **Format** | YAML (→ artifacts) | SQL DDL | YAML | YAML **or** JSON | LookML DSL | REST + DAX `INFO.VIEW.*` JSON dump |

## Per-platform notes for parsing

- **dbt** (`parse_dbt.py`) — read the *resolved* `semantic_manifest.json`, not the
  raw YAML. It is the only spec with `MEASURE_OF`/`COMPOSED_OF` edges and
  `SavedQuery` nodes. `has_filter=true` when a metric/measure carries an explicit
  MetricFlow filter.
- **Snowflake** (`parse_snowflake_semantic_view.py`) — one view spans many tables;
  `FACTS` become `Column(is_fact=true)`, `MEASURES` become `Measure` with the `AS`
  expression as `canonical_expr`. `RELATIONSHIPS` give `JOINS` with `inferred=false`.
- **Databricks** (`parse_databricks_metric_view.py`) — single `source`; no joins.
  Carries the richest NL payload (`synonyms`, `format`) — normalized onto nodes.
- **OSI** (`parse_osi.py`) — `datasets`→tables+dimensions, `metrics`→measures,
  `relationships`→joins (`from` is the many side). Expressions are multi-dialect;
  we prefer `ANSI_SQL`, then Snowflake/Databricks, then the first available.
- **LookML** (`parse_lookml.py`) — `view`→semantic model + table; `dimension`/
  `dimension_group`→dimensions; `measure`→measure with `canonical_expr` built from
  `type(sql)` (e.g. `sum(${TABLE}.amount)`). `explore` `join` blocks →`JOINS` using
  the `relationship` as cardinality. A `primary_key: yes` dimension also emits an
  `Entity`.
- **Power BI** (`parse_powerbi.py`) — reads a metadata dump *directory*, not a file:
  dataset→`SemanticModel`, table→`PhysicalTable` (`<database>.<table>`, no schema),
  column→`Column`, DAX measure→`Measure`, report→`SavedQuery`, report field-usage→
  `GROUPED_BY`. `link_to_dbt()` then matches Power BI tables to dbt relations by
  database + identifier and adds `DEPENDS_ON` edges. Auto date tables
  (`LocalDateTable*`/`DateTableTemplate*`) and hidden/`RowNumber` columns are
  dropped. Full detail: [powerbi.md](powerbi.md).

## Normalizing across the spread

- All presentation metadata collapses to `display_name` / `description` /
  `synonyms[]` / `format{}`, null where a platform doesn't express it.
- Comparable concepts (for drift) are `Metric` and `Measure` nodes, keyed by
  `normalized_name`. Snowflake/Databricks/OSI "measures/metrics" and LookML
  "measures" all map to the same comparison space as dbt metrics.
- Genuine cross-platform renames are reconciled with a crosswalk file (see
  [drift-detection.md](drift-detection.md)) — never by loosening name normalization.
