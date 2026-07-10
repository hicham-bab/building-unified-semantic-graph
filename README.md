# building-unified-semantic-graph

A [dbt Wizard](https://docs.getdbt.com/docs/dbt-ai/about-dbt-wizard-cli) skill that
unifies semantic-layer definitions from across a data stack into **one knowledge
graph** and flags **cross-platform drift**.

It ingests everything dbt Fusion's static analysis (the language server) knows about
a project — models, columns, lineage — plus every semantic spec in play, and merges
them into a single JSON property graph. It then reconciles "the same" metric defined
in multiple places and reports where the definitions disagree. It also ingests a
**Power BI** workspace's metadata and traces reports back through their datasets and
tables to the dbt models that build them.

## Supported semantic specs

| Spec | Format | Source |
|---|---|---|
| dbt Semantic Layer / MetricFlow | `semantic_manifest.json` | dbt Fusion artifacts |
| Snowflake Semantic Views | `CREATE SEMANTIC VIEW` SQL | `.sql` files |
| Databricks Metric Views | YAML (spec `version: 1.1`) | `.yml` files |
| Open Semantic Interchange (OSI) | YAML or JSON core-spec | [OSI](https://github.com/open-semantic-interchange/OSI) |
| LookML | Looker `view` / `explore` DSL | `.lkml` files |
| Power BI | REST + DAX metadata dump | `powerbi/pull_powerbi_metadata.py` |

Plus dbt Fusion "LSP infos": `manifest.json`, `catalog.json`, and column-level
lineage — via artifacts and, optionally, a live `dbt lsp --socket`.

## Outputs

Given `--out semantic_graph.json`, you get three files:

- **`semantic_graph.json`** — the full `{nodes, edges, metadata}` property graph.
- **`semantic_graph_drift_report.md`** — human-readable drift, grouped by subtype.
- **`semantic_graph_concepts.json`** — the **easy-to-consume** catalog: one row per
  concept with its per-platform definitions and drift flags. Feed this straight to
  an agent or BI tool.

Drift subtypes: `missing_on_platform`, `expression_mismatch`, `filter_mismatch`,
`grain_mismatch`. See [`references/drift-detection.md`](references/drift-detection.md).

## Install as a dbt Wizard skill

```bash
./install.sh          # copies this repo into ~/.dbt/wizard/skills/building-unified-semantic-graph
```

Then in a project just ask Wizard to *"build a unified semantic knowledge graph and
flag cross-platform drift."*

## Run the scripts directly

Requires Python 3.11+ and the deps in `requirements.txt` (`pyyaml`, and `pyarrow`
only for column-level-lineage parquet):

```bash
pip install -r requirements.txt

python3 scripts/build_graph.py \
  --dbt-project /path/to/dbt_project \
  --snowflake-sql '/path/to/semantic_views/*.sql' \
  --databricks-yaml '/path/to/metric_views/*.yml' \
  --osi '/path/to/*.osi.yml' \
  --lookml '/path/to/*.lkml' \
  --powerbi metadata_output \
  --crosswalk crosswalk.json \
  --out semantic_graph.json
```

Every flag is optional and repeatable — provide only the specs you have. Quote the
globs so the script expands them. `--dbt-project` accepts a project dir (its
`target/` is used) or a `target/` dir directly, and may be repeated for a dbt Mesh.
`--powerbi` takes a Power BI metadata dump directory (see below).

### Try the examples

```bash
python3 scripts/build_graph.py \
  --osi examples/orders.osi.yml \
  --lookml examples/orders.view.lkml \
  --out /tmp/example_graph.json
cat /tmp/example_graph_drift_report.md
```

## Power BI (`powerbi/`)

The `powerbi/` tooling pulls a Power BI workspace's metadata (via the Power BI REST +
DAX APIs, authenticating with a token borrowed from your `az login`) into a
`metadata_output/` directory, which `--powerbi` then reads.

```bash
# 1. Pull workspace / dataset / report metadata
python3 powerbi/pull_powerbi_metadata.py --workspace <workspace-id>

# 2. (optional) Parse which report visuals reference which fields
python3 powerbi/pull_report_usage.py

# 3a. Render standalone Mermaid lineage diagrams (source -> model -> report), or
python3 powerbi/mermaid_graph.py

# 3b. …link Power BI tables to a dbt project's manifest directly
python3 powerbi/dbt_lineage.py run --project-id <dbt-project-id> --match name
```

`parse_powerbi.py` maps a dataset to a `SemanticModel`, each table to a
`PhysicalTable` (`<database>.<table>` — Power BI exposes no schema), each report to a
`SavedQuery`, and report field-usage to `GROUPED_BY` edges. When both dbt and Power BI
are present, the builder matches Power BI tables to dbt-built relations by
database + identifier and draws `DEPENDS_ON` edges into the dbt spine. Full detail:
[`references/powerbi.md`](references/powerbi.md).

## How it works

Each spec is parsed into a common vocabulary — `PhysicalTable`, `Column`,
`SemanticModel`, `Entity`, `Dimension`, `Measure`, `Metric`, `SavedQuery` — with
edges for lineage (`DEPENDS_ON`, `DERIVED_FROM`), joins (`JOINS`), composition
(`MEASURE_OF`, `COMPOSED_OF`), cross-platform matches (`EQUIVALENT_TO`), and
inconsistencies (`DRIFT`). dbt is the spine: it is the only spec with a metric
composition graph and saved queries, and its resolved artifacts carry physical
back-links the others attach to. Concepts are matched by normalized name, with an
optional crosswalk file for deliberate renames.

**Start with [`ARCHITECTURE.md`](ARCHITECTURE.md)** for the full data flow, module
map, and design rationale. Then [`references/`](references/) has the field-level
detail: the graph schema, spec crosswalk, artifact generation, drift rules, and the
Power BI mapping.

## Layout

```
ARCHITECTURE.md               # full design: data flow, module map, rationale
SKILL.md                      # the Wizard skill definition
scripts/                      # parsers + graph model + CLI (dependency-light Python)
powerbi/                      # Power BI metadata pullers + Mermaid + dbt-manifest linker
references/                   # schema, crosswalk, artifacts, drift, Power BI (loaded on demand)
examples/                     # OSI + LookML fixtures and a sample crosswalk
```
