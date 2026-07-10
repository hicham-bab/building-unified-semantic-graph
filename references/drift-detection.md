# Drift detection — matching rules, subtypes, crosswalk, outputs

Drift detection reconciles "the same" concept defined across platforms and reports
where the definitions disagree. It runs over the merged graph in `detect_drift.py`.

## What gets compared

- **Comparable nodes**: `Metric` and `Measure` (the consumer-facing named
  aggregations). dbt metrics, Snowflake/Databricks measures, OSI metrics, and LookML
  measures all share this comparison space.
- **Dimensions** are compared separately for time-granularity only.
- **Match key**: `normalized_name` (lowercased, non-alphanumerics → `_`), optionally
  re-keyed through a crosswalk file for deliberate renames.
- **In-play platforms**: only the platforms that appear in the graph are candidates,
  so a concept is "missing" only relative to specs actually loaded.

For each concept present on ≥2 platforms, an `EQUIVALENT_TO` edge is emitted per
platform pair; mismatches add `DRIFT` edges.

## Drift subtypes

| Subtype | Fires when | Why it matters |
|---|---|---|
| `missing_on_platform` | concept defined on some in-play platforms, absent on another | coverage gap; a metric governed in dbt but never defined downstream (or vice-versa) |
| `expression_mismatch` | matched concept has different `canonical_expr` (normalized) | **highest value** — "identical" metrics that compute different SQL return different numbers |
| `filter_mismatch` | dbt applies an explicit metric/measure filter; the match elsewhere inlines it | the filter semantics live in different places and can drift independently |
| `grain_mismatch` | matched time dimension declared at different granularities | rollups won't line up |

`canonical_expr` is normalized (lowercased, whitespace-collapsed) before comparison,
so `SUM(x)` == `sum( x )`. Genuinely different SQL — e.g. dbt `sum(gross_revenue)`
vs LookML `sum(${TABLE}.gross_revenue)` vs Snowflake `AVG(CASE WHEN ...)` — is
reported. Some expression mismatches are cosmetic (table-qualified column refs);
read them alongside the actual SQL, which is in the node `props`.

## Crosswalk file (for renames)

When the same concept is named differently across platforms, map the names so they
collapse into one concept. JSON, passed via `--crosswalk`:

```json
{
  "concepts": [
    { "canonical": "recognised_revenue",
      "dbt": "total_recognised_revenue",
      "snowflake": "total_net_revenue",
      "databricks": "total_revenue",
      "osi": "recognised_revenue",
      "lookml": "revenue" }
  ]
}
```

Each per-platform value may be a string or a list. Any platform key from the
supported set (`dbt`, `snowflake`, `databricks`, `osi`, `lookml`, `powerbi`) is honored.
Without a crosswalk, only concepts whose names already agree will match.

## Outputs

Three files (stem = the `--out` path without extension):

1. **`<stem>.json`** — the full property graph (`nodes`, `edges`, `metadata`).
2. **`<stem>_drift_report.md`** — human-readable, grouped by subtype.
3. **`<stem>_concepts.json`** — the **easy-to-consume** catalog: one row per concept
   with `defined_on`, `consistent`, `drift[]`, and a `definitions` map of each
   platform's expression/display_name/synonyms. This is the artifact to feed an
   agent or BI tool that just wants "here is every metric, where it lives, and
   whether the definitions agree."

Example `_concepts.json` row:

```json
{
  "concept": "return_rate",
  "defined_on": ["databricks", "dbt", "osi", "snowflake"],
  "consistent": false,
  "drift": ["expression_mismatch:dbt~snowflake", "missing_on:lookml"],
  "definitions": {
    "dbt": {"type": "Metric", "expression": "returned_orders / total_orders", ...},
    "snowflake": {"type": "Measure", "expression": "COUNT_IF(is_returned)::FLOAT / ...", ...}
  }
}
```
