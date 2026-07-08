# Spec crosswalk — how each platform maps to the shared concepts

Five spec formats feed the graph. They share a core (physical table, dimension,
measure/metric) and diverge on the extras. This table drives the parsers.

| Concept | dbt MetricFlow | Snowflake Semantic View | Databricks Metric View | OSI core-spec | LookML |
|---|---|---|---|---|---|
| **Physical table** | `model: ref()` + resolved `node_relation` | `TABLES(...)` FQ names | `source:` FQ UC table | `dataset.source` | `sql_table_name` |
| **Semantic container** | semantic_model | semantic view (multi-table) | metric view (1 table) | semantic_model → datasets | view |
| **Dimension** | `dimensions` (time/categorical) | `DIMENSIONS(...)` | `dimensions` (+`format`,`synonyms`) | `dataset.fields` (+`dimension.is_time`) | `dimension` / `dimension_group` |
| **Measure / metric** | `measures` + first-class `metrics` | `MEASURES ... AS <sql>` | `measures` (`expr`) | `metrics` (`expression.dialects`) | `measure` (`type`+`sql`) |
| **Raw fact column** | — (measure `expr`) | `FACTS(...)` split | — | field expression | dimension `sql` |
| **Join / entity** | `entities` primary/foreign (inferred) | `RELATIONSHIPS ... MANY TO ONE` | none (single-source) | `relationships` (`from`→many, `to`→one) | `explore` `join` + `relationship` |
| **Metric composition** | ratio/derived, offset, filter | inlined into `expr` | inlined into `expr` | inlined into expression | inlined into `sql` (or `type`) |
| **Saved query** | `saved_queries` | — | — | — | — (Looks / dashboards, not parsed) |
| **NL / presentation** | `label`, `description` | `LABEL`, `COMMENT` | `display_name`, `format`, `synonyms` | `description`, `ai_context` | `label`, `group_label` |
| **Format** | YAML (→ artifacts) | SQL DDL | YAML | YAML **or** JSON | LookML DSL |

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

## Normalizing across the spread

- All presentation metadata collapses to `display_name` / `description` /
  `synonyms[]` / `format{}`, null where a platform doesn't express it.
- Comparable concepts (for drift) are `Metric` and `Measure` nodes, keyed by
  `normalized_name`. Snowflake/Databricks/OSI "measures/metrics" and LookML
  "measures" all map to the same comparison space as dbt metrics.
- Genuine cross-platform renames are reconciled with a crosswalk file (see
  [drift-detection.md](drift-detection.md)) — never by loosening name normalization.
