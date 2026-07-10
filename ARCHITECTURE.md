# Architecture

This document is the map of how the skill works end to end. Deep detail lives in
[`references/`](references/); this ties the pieces together.

## Purpose

Take the semantic definitions scattered across a data stack — dbt, Snowflake,
Databricks, OSI, LookML, and Power BI — plus what dbt Fusion's static analysis
knows about the project, merge them into **one property graph**, and report where
"the same" metric defined in multiple places actually disagrees (**cross-platform
drift**). The Power BI side additionally traces reports back through their datasets
and tables to the dbt models that build them.

The repo pairs the framework (`scripts/`) with the Power BI tooling (`powerbi/`)
that produces the metadata dump the framework consumes; see the README for the
`powerbi/` pull/link/diagram workflow.

## Data flow

```
              ┌───────────────────────── INPUTS ─────────────────────────┐
 dbt Fusion   │ semantic_manifest.json ─┐                                 │
 artifacts ───┤ manifest.json           ├─► parse_dbt.py ─────┐           │
 (the "LSP    │ catalog.json            │   (+ raw YAML        │           │
  infos")     │ CLL parquet             │    fallback)         │           │
              │ models/**/*.yml ────────┘                      │           │
              │                                                 │          │
 spec files ──┤ *.sql  ─► parse_snowflake_semantic_view.py ────┤          │
              │ *.yml  ─► parse_databricks_metric_view.py ──────┤  merge   │
              │ *.osi  ─► parse_osi.py ─────────────────────────┤  into    │
              │ *.lkml ─► parse_lookml.py ──────────────────────┤  one     │
              │ metadata_output/ ─► parse_powerbi.py ───────────┘  Graph   │
              │        (Power BI dump)   │ + link_to_dbt()               │ │
              └──────────────────────────────────────────────────────────┘
                                                                    │
                     ┌──────────────────────────────────┐          │
 live server ───────►│ lsp_client.py  (optional enrich) │──────────┤
 (dbt lsp --socket)  └──────────────────────────────────┘          │
                                                                    ▼
                                              detect_drift.py  (match + drift)
                                                                    │
                          ┌─────────────────────────────────────────┼──────────────┐
                          ▼                                          ▼              ▼
                  semantic_graph.json                 ..._drift_report.md   ..._concepts.json
                  (full property graph)               (human-readable)      (per-concept catalog)
```

`build_graph.py` is the orchestrator that runs this pipeline.

## Pipeline stages

1. **Parse** — each source is parsed by its own module into the shared graph
   vocabulary. dbt is parsed first because it is the spine (see below).
2. **Merge** — all parsers write into a single `Graph`. Nodes de-duplicate by a
   stable id (`platform:type:qualifier`), so re-parsing or overlapping inputs merge
   cleanly rather than duplicate.
3. **Enrich (optional)** — `lsp_client.py` connects to a live Fusion language server
   for diagnostics/capabilities. Never required; failures are warnings.
4. **Detect drift** — `detect_drift.py` matches comparable concepts across platforms
   and emits `EQUIVALENT_TO` / `DRIFT` edges plus a denormalized concept catalog.
5. **Emit** — three files: the full graph, a Markdown drift report, and the
   easy-to-consume `_concepts.json`.

## Why dbt is the spine

dbt is the only spec that carries a **metric-composition graph** (ratio/derived
metrics, `MEASURE_OF`/`COMPOSED_OF`), **saved queries**, and fully-resolved
**physical back-links** (`node_relation`). So it is parsed first; the other specs
attach to the same physical tables (by `relation_name`) and the same concept space
(by normalized name), rather than living in disconnected islands. Power BI is the
clearest case: it has no schema in its warehouse binding, so `parse_powerbi.link_to_dbt()`
matches its tables to dbt-built relations by database + identifier and draws
`DEPENDS_ON` edges into the dbt spine.

## Module map (`scripts/`)

| Module | Responsibility |
|---|---|
| `graph_model.py` | The property graph: `Node`, `Edge`, `Graph`; node/edge type constants; `normalize_name` / `normalize_expr`; JSON serialization; edge de-dup. **Start here.** |
| `build_graph.py` | CLI orchestrator — wires inputs → parsers → merge → enrich → drift → outputs. |
| `parse_dbt.py` | dbt Fusion artifacts (`manifest`, `semantic_manifest`, `catalog`, CLL parquet) **and** a raw `models/**/*.yml` fallback for legacy-spec projects. |
| `parse_snowflake_semantic_view.py` | Tolerant parser for `CREATE SEMANTIC VIEW` DDL (TABLES/DIMENSIONS/FACTS/MEASURES/RELATIONSHIPS). |
| `parse_databricks_metric_view.py` | Databricks Metric View YAML (v1.1); normalizes `synonyms`/`format`. |
| `parse_osi.py` | Open Semantic Interchange core-spec (YAML/JSON); multi-dialect expressions. |
| `parse_lookml.py` | LookML `view`/`explore` DSL (custom tokenizer). |
| `parse_powerbi.py` | Power BI metadata dump (datasets/reports/columns/measures/relationships) → graph, plus `link_to_dbt()` bridging Power BI tables to dbt relations. |
| `detect_drift.py` | Cross-platform matching, `EQUIVALENT_TO`/`DRIFT` edges, drift report, concept catalog, crosswalk loading. |
| `lsp_client.py` | Optional live Fusion LSP enrichment (listen + spawn + JSON-RPC handshake). |

The Power BI **pullers** live under `powerbi/` (not `scripts/`): `pbi_client.py`,
`pull_powerbi_metadata.py`, `pull_report_usage.py` produce `metadata_output/`;
`mermaid_graph.py` and `dbt_lineage.py` are the standalone Mermaid / dbt-manifest
lineage tools. `scripts/parse_powerbi.py` reads their JSON output — the framework
has no import dependency on `powerbi/`.

Dependencies are light: standard library + `pyyaml`, plus `pyarrow` only for the CLL
parquet path. Every parser is independent and degrades gracefully — provide only the
specs you have.

## The graph model

Nodes: `PhysicalTable`, `Column`, `SemanticModel`, `Entity`, `Dimension`, `Measure`,
`Metric`, `SavedQuery`. Edges: `DEPENDS_ON`, `DERIVED_FROM`, `JOINS`, `HAS_*`,
`MEASURE_OF`, `COMPOSED_OF`, `GROUPED_BY`, `EQUIVALENT_TO`, `DRIFT`. Every node/edge
carries a `platform` tag and every comparable node a `normalized_name`.

Full field-by-field spec: [`references/graph-schema.md`](references/graph-schema.md).
How each platform's fields map onto it: [`references/spec-crosswalk.md`](references/spec-crosswalk.md).

## Matching & drift

Comparable nodes (`Metric` + `Measure`) are grouped by `normalized_name` (re-keyed
via an optional crosswalk file for deliberate renames). Each concept present on ≥2
platforms gets `EQUIVALENT_TO` edges; disagreements become `DRIFT` edges with a
`subtype`: `missing_on_platform`, `expression_mismatch`, `filter_mismatch`,
`grain_mismatch`. Rules and the crosswalk format:
[`references/drift-detection.md`](references/drift-detection.md).

## dbt "LSP infos": artifacts vs. live server

The dbt side is built from Fusion's static-analysis **artifacts** (deterministic,
batch, persistable). A live `dbt lsp --socket` is **optional** enrichment for
interactive/positional data. Note two things this project pinned down empirically:

- Fusion's socket is **reversed** — the server connects *out* to a listening client,
  so `lsp_client.py` listens and spawns the server.
- **Column-level lineage is not an LSP method** — use the `dbt compile
  --write-lineage` parquet. The LSP is for hover/definition/references/live-compile.

Details and how to generate each artifact:
[`references/fusion-artifacts.md`](references/fusion-artifacts.md).

## Outputs

| File | For |
|---|---|
| `<out>.json` | the full `{nodes, edges, metadata}` property graph |
| `<out>_drift_report.md` | humans — drift grouped by subtype |
| `<out>_concepts.json` | agents / BI — one row per concept: `defined_on`, `consistent`, `drift[]`, per-platform `definitions` |

## Extending to a new spec

1. Write `scripts/parse_<platform>.py` that emits into a `Graph` using the shared
   node/edge constants and the `platform:type:qualifier` id scheme.
2. Add the platform to `PLATFORMS` in `graph_model.py` (drift/crosswalk pick it up
   automatically).
3. Add a `--<platform>` input in `build_graph.py`.
4. Add a row to the crosswalk table in `references/spec-crosswalk.md`.
