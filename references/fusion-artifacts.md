# dbt Fusion artifacts — the "LSP infos"

The dbt side of the graph is built from what Fusion's static analysis (the same
engine behind the language server) produces. This is the deterministic base; the
live LSP is optional enrichment on top.

## Artifacts and how to generate them

All land in the project's `target/` directory.

| Artifact | Contains | How to produce |
|---|---|---|
| `manifest.json` | model/source nodes, `columns`, `parent_map`/`child_map`, `depends_on` | `dbt parse` |
| `semantic_manifest.json` | semantic models, entities, dimensions, measures, metrics, saved_queries — fully resolved with `node_relation` back-links | `dbt parse` (populated only for **current-spec** semantic models) |
| `catalog.json` | column data types | `dbt compile --write-catalog` |
| column-level lineage (parquet) | column → column edges | `dbt compile --write-metadata --write-lineage --static-analysis strict` |

Typical sequence:

```bash
dbt parse                                              # manifest + semantic_manifest
dbt compile --write-catalog                            # + catalog.json (column types)
dbt compile --write-metadata --write-lineage --static-analysis strict   # + CLL parquet
```

`parse_dbt.py` reads whichever of these exist and tolerates the rest. `catalog.json`
only adds column `data_type`s; CLL only adds `DERIVED_FROM` edges. Neither is
required for the metric/drift analysis.

## Gotchas

- **Legacy-YAML semantic models don't compile into `semantic_manifest.json`** under
  Fusion — you'll see a `SemanticModelDeprecated` warning and the file will have
  `semantic_models: []`. Migrate with the `building-dbt-semantic-layer` skill first,
  or the dbt side of the graph will be empty.
- **`dbt parse` may still need env vars** to render `profiles.yml` (e.g.
  `env_var('DBX_HOST')`). Parse does not connect to the warehouse, so dummy values
  are fine just to get past profile rendering.
- **CLL parquet requires `pyarrow`** (installed here) and `--static-analysis strict`,
  which also surfaces static-analysis errors — the graph still builds if CLL is
  partial or absent.
- A dbt **Mesh** is handled by passing `--dbt-project` once per project; their nodes
  merge into one graph by physical `relation_name`.

## Live LSP enrichment (optional)

Start the server in the project, then point the builder at its port:

```bash
dbt lsp --socket 8765 --static-analysis strict
python3 scripts/build_graph.py --dbt-project . --lsp-socket 8765 --out graph.json
```

`lsp_client.py` performs the LSP `initialize`/`initialized` handshake, records the
server's capabilities and any published diagnostics into `graph.metadata`, and
attempts a best-effort column-lineage request. The column-lineage method
(`CLL_METHOD` in `lsp_client.py`, default `dbt/columnLineage`) is a Fusion server
extension — if your build names it differently, adjust that constant. Connection
failures are recorded as warnings and never abort the build.
