#!/usr/bin/env python3
"""Build a unified semantic knowledge graph across dbt, Snowflake, Databricks,
OSI, LookML, and Power BI.

Ingests Fusion static artifacts (the "LSP infos" — manifest.json,
semantic_manifest.json, catalog.json, column-level lineage) plus every
semantic-layer spec in play (dbt MetricFlow, Snowflake Semantic Views, Databricks
Metric Views, OSI, LookML) and a Power BI metadata dump, merges them into one
JSON property graph, and flags cross-platform drift.

Usage:
  python3 build_graph.py \
    --dbt-project PATH [--dbt-project PATH ...] \
    --snowflake-sql 'GLOB' [--snowflake-sql 'GLOB' ...] \
    --databricks-yaml 'GLOB' [--databricks-yaml 'GLOB' ...] \
    --powerbi DIR [--powerbi DIR ...] \
    [--lsp-socket PORT] \
    [--crosswalk crosswalk.json] \
    [--out semantic_graph.json]

`--dbt-project` accepts a project dir (its `target/` is used) or a `target/` dir
directly. `--powerbi` takes a Power BI metadata dump directory (the output of
`powerbi/pull_powerbi_metadata.py`). Outputs `<out>` and a sibling
`<out stem>_drift_report.md`.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

from graph_model import Graph, dedupe_edges
import parse_dbt
import parse_snowflake_semantic_view as sf
import parse_databricks_metric_view as dbx
import parse_osi
import parse_lookml
import parse_powerbi
import detect_drift
import lsp_client


def _expand(patterns: list[str]) -> list[str]:
    files: list[str] = []
    for pat in patterns or []:
        files.extend(sorted(glob.glob(os.path.expanduser(pat), recursive=True)))
    return files


def _target_dir(project: str) -> str:
    project = os.path.expanduser(project)
    if os.path.basename(os.path.normpath(project)) == "target":
        return project
    return os.path.join(project, "target")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build unified semantic knowledge graph")
    ap.add_argument("--dbt-project", action="append", default=[],
                    help="dbt project dir (uses its target/) or a target/ dir")
    ap.add_argument("--dbt-yaml", action="append", default=[],
                    help="glob of raw dbt semantic YAML files (semantic_models/metrics), "
                         "for projects with no compiled artifacts")
    ap.add_argument("--snowflake-sql", action="append", default=[],
                    help="glob of CREATE SEMANTIC VIEW .sql files")
    ap.add_argument("--databricks-yaml", action="append", default=[],
                    help="glob of Databricks Metric View .yml files")
    ap.add_argument("--osi", action="append", default=[],
                    help="glob of OSI core-spec .yml/.yaml/.json files")
    ap.add_argument("--lookml", action="append", default=[],
                    help="glob of LookML .lkml files (views + explores)")
    ap.add_argument("--powerbi", action="append", default=[],
                    help="Power BI metadata dump dir (output of "
                         "powerbi/pull_powerbi_metadata.py)")
    ap.add_argument("--lsp-socket", type=int, default=None,
                    help="listen on this port and spawn `dbt lsp --socket PORT` to "
                         "connect back for live column-lineage/diagnostics (optional)")
    ap.add_argument("--dbt-executable", default="dbt",
                    help="dbt Fusion executable used to spawn the language server")
    ap.add_argument("--crosswalk", default=None,
                    help="optional JSON crosswalk for renamed concepts")
    ap.add_argument("--out", default="semantic_graph.json")
    args = ap.parse_args(argv)

    graph = Graph()
    warn: list[str] = []
    sources_used: list[str] = []

    # 1. dbt Fusion artifacts (the spine)
    for project in args.dbt_project:
        target = _target_dir(project)
        parse_dbt.parse_dbt_project(target, graph, warn)
        sources_used.append(f"dbt:{target}")

    # 1b. Raw dbt semantic YAML (explicit; for un-built projects)
    for path in _expand(args.dbt_yaml):
        with open(path) as f:
            doc = __import__("yaml").safe_load(f)
        if isinstance(doc, dict):
            parse_dbt._parse_semantic_yaml_doc(doc, graph)
        sources_used.append(f"dbt-yaml:{path}")

    # 2. Snowflake Semantic Views
    for path in _expand(args.snowflake_sql):
        with open(path) as f:
            sf.parse_semantic_view_sql(f.read(), graph, warn, origin=path)
        sources_used.append(f"snowflake:{path}")

    # 3. Databricks Metric Views
    for path in _expand(args.databricks_yaml):
        with open(path) as f:
            dbx.parse_metric_view_yaml(f.read(), graph, warn, origin=path)
        sources_used.append(f"databricks:{path}")

    # 3b. OSI core-spec files
    for path in _expand(args.osi):
        with open(path) as f:
            parse_osi.parse_osi(f.read(), graph, warn, origin=path)
        sources_used.append(f"osi:{path}")

    # 3c. LookML views + explores
    for path in _expand(args.lookml):
        with open(path) as f:
            parse_lookml.parse_lookml(f.read(), graph, warn, origin=path)
        sources_used.append(f"lookml:{path}")

    # 3d. Power BI metadata dumps
    for metadata_dir in args.powerbi:
        parse_powerbi.parse_powerbi_dir(metadata_dir, graph, warn)
        sources_used.append(f"powerbi:{os.path.expanduser(metadata_dir)}")

    # 3e. Bridge Power BI tables to dbt-built relations (dbt is the spine)
    pbi_dbt_links = parse_powerbi.link_to_dbt(graph, warn)

    # 4. Optional live LSP enrichment
    if args.lsp_socket:
        project_dir = os.path.expanduser(args.dbt_project[0]) if args.dbt_project else os.getcwd()
        if os.path.basename(os.path.normpath(project_dir)) == "target":
            project_dir = os.path.dirname(os.path.normpath(project_dir))
        lsp_client.enrich_from_lsp(
            graph, args.lsp_socket, project_dir, warn,
            executable=args.dbt_executable,
        )

    if not graph.nodes:
        print("ERROR: no inputs produced any nodes. Check paths/globs.", file=sys.stderr)
        for w in warn:
            print("  " + w, file=sys.stderr)
        return 2

    # 5. Drift detection
    alias = detect_drift.load_crosswalk(args.crosswalk, warn)
    summary = detect_drift.detect_drift(graph, alias, warn)

    dedupe_edges(graph)
    graph.metadata.update({
        "sources": sources_used,
        "warnings": warn,
        "powerbi_dbt_links": pbi_dbt_links,
        "drift_summary": {
            "concepts_compared": summary["concepts_compared"],
            "equivalences": summary["equivalences"],
            "findings": {k: len(v) for k, v in summary["findings"].items()},
        },
    })

    out = os.path.expanduser(args.out)
    graph.to_json(out)
    stem = os.path.splitext(out)[0]
    report_path = stem + "_drift_report.md"
    with open(report_path, "w") as f:
        f.write(detect_drift.render_report(summary))
    # denormalized, easy-to-consume concept catalog (one row per concept)
    concepts_path = stem + "_concepts.json"
    import json as _json
    with open(concepts_path, "w") as f:
        _json.dump({
            "platforms": graph.platforms_present(),
            "concepts": summary["concepts"],
        }, f, indent=2)

    # console summary
    print(f"Wrote {out}")
    print(f"  nodes: {len(graph.nodes)}  edges: {len(graph.edges)}  "
          f"platforms: {', '.join(graph.platforms_present()) or 'none'}")
    print(f"  concepts compared: {summary['concepts_compared']}  "
          f"equivalences: {summary['equivalences']}")
    if args.powerbi:
        print(f"  Power BI -> dbt table links: {pbi_dbt_links}")
    findings = summary["findings"]
    total = sum(len(v) for v in findings.values())
    print(f"  drift findings: {total} "
          f"({', '.join(f'{k}={len(v)}' for k, v in findings.items() if v) or 'none'})")
    print(f"Wrote {report_path}")
    print(f"Wrote {concepts_path}")
    if warn:
        print(f"  {len(warn)} warning(s):", file=sys.stderr)
        for w in warn:
            print("    " + w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
