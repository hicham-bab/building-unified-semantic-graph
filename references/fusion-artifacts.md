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

## Raw semantic YAML (fallback / legacy spec)

The dbt side is normally read from `semantic_manifest.json`. But **legacy-spec
semantic models are not compiled into that file** by Fusion. So `parse_dbt.py`
automatically falls back to scanning `models/**/*.yml|*.yaml` for raw
`semantic_models:` / `metrics:` blocks whenever the manifest yields no metrics, and
parses them directly (resolving `model: ref('x')` to the physical table when the
manifest is present). You can also point at raw YAML explicitly for an un-built
project:

```bash
python3 scripts/build_graph.py --dbt-yaml 'models/**/*.yml' --out graph.json
```

Raw-YAML nodes use the same id scheme as manifest nodes, so if both are present they
merge. This is what lets the graph capture (and drift-check) a project's governed
`filter:`-based metrics even before migration.

## Live LSP enrichment (optional)

Fusion's socket transport is **reversed**: `dbt lsp --socket <PORT>` does not listen
— it connects *out* to a client already listening on `<PORT>`. So `lsp_client.py`
listens on the port and spawns the server pointed back at it:

```bash
python3 scripts/build_graph.py --dbt-project . --lsp-socket 8765 \
  --dbt-executable "$(command -v dbt)" --out graph.json
```

It performs the LSP `initialize`/`initialized` handshake (verified against
`dbt-lsp 2.0.0-preview.189`) and records the server's `serverInfo` + capabilities
into `graph.metadata.lsp`. Connection/timeout failures are warnings, never fatal.

**Column-level lineage is *not* an LSP method.** The Fusion LSP advertises standard
providers (`hover`, `definition`, `references`, `semanticTokens`, `codeLens`) plus
`executeCommand` with: `dbt.listNodes`, `dbt.getCurrentNode`, `dbt.compileFile`,
`dbt.compileLsp`, `dbt.clearTarget`, `dbt.getProjectInfo`, `dbt.show` — none return
column lineage. So for CLL use the parquet path (`dbt compile --write-lineage`)
above; the LSP is for interactive/positional queries (hover types, go-to-definition,
find-references, live compile/diagnostics). `lsp_client.CLL_METHODS` probes a few
candidate names and safely reports "unsupported" if the extension isn't present.
