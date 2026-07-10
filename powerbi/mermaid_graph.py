#!/usr/bin/env python3
"""
Build graphical lineage from the pulled metadata, as Mermaid diagrams that
render in VS Code (Markdown preview) and GitHub.

Two diagrams are produced in metadata_output/graph.md:
  1. Model graph  - Reports -> Semantic Models -> Tables, with star-schema
     relationships. Reports connect to the specific tables they use; tables a
     report actually references are highlighted; edges show used-column counts.
  2. Column graph - Reports -> the exact TABLE.COLUMN fields each one uses
     (from report_column_usage.json, if present).

Usage:
    python3 mermaid_graph.py [--out metadata_output]
"""

import argparse
import glob
import json
import os
import re

from pbi_client import is_auto_table


def load(path):
    with open(path) as f:
        return json.load(f)


def maybe_load(path):
    return load(path) if os.path.exists(path) else None


def node_id(prefix, raw):
    """Mermaid-safe node id (alphanumeric + underscore only)."""
    return re.sub(r"\W+", "_", f"{prefix}_{raw}")


def table_id(ds_id, table):
    return node_id("T", f"{ds_id}_{table}")


def collect(out_dir):
    """Read the pulled JSON into a small in-memory model for graphing."""
    reports = maybe_load(os.path.join(out_dir, "reports.json")) or []
    usage = maybe_load(os.path.join(out_dir, "report_column_usage.json")) or {}

    datasets = {}
    for path in sorted(glob.glob(os.path.join(out_dir, "datasets", "*.json"))):
        d = load(path)
        ds_id = d.get("dataset_id") or d.get("dataset", {}).get("id")
        model = d.get("model", {})
        tables = [t["[Name]"] for t in model.get("tables", {}).get("rows", [])
                  if not is_auto_table(t["[Name]"])]
        rels = []
        for r in model.get("relationships", {}).get("rows", []):
            if is_auto_table(r["[FromTable]"]) or is_auto_table(r["[ToTable]"]):
                continue
            rels.append({"from": r["[FromTable]"], "from_col": r["[FromColumn]"],
                         "to": r["[ToTable]"], "to_col": r["[ToColumn]"],
                         "active": r.get("[IsActive]", True)})
        sources = []
        for s in (d.get("datasources") or []):
            if isinstance(s, dict):
                cd = s.get("connectionDetails", {})
                sources.append({"kind": cd.get("kind") or s.get("datasourceType"),
                                "path": cd.get("path") or cd.get("server", "")})
        datasets[ds_id] = {"name": d.get("dataset", {}).get("name", ds_id),
                           "tables": tables, "relationships": rels,
                           "sources": sources}
    return reports, datasets, usage


def build_model_graph(reports, datasets, usage):
    lines = ["flowchart LR"]
    used_tables = set()  # (ds_id, table) the reports touch -> highlight

    for r in reports:
        lines.append(f'  {node_id("R", r["id"])}["&#128202; {r["name"]}"]')  # 📊

    for ds_id, ds in datasets.items():
        lines.append(f'  subgraph {node_id("DS", ds_id)}["&#129513; {ds["name"]}"]')
        lines.append("    direction TB")
        for t in ds["tables"]:
            lines.append(f'    {table_id(ds_id, t)}["{t}"]')
        for rel in ds["relationships"]:
            arrow = "-->" if rel["active"] else "-.->"
            lines.append(f'    {table_id(ds_id, rel["from"])} {arrow}'
                         f'|{rel["from_col"]}| {table_id(ds_id, rel["to"])}')
        lines.append("  end")

    # Report -> the specific tables it uses (fall back to the dataset node if we
    # have no parsed usage for that report).
    for r in reports:
        rid, ds_id = node_id("R", r["id"]), r.get("datasetId")
        rusage = usage.get(r["id"], {})
        tabs = rusage.get("tables") if isinstance(rusage, dict) else None
        if tabs and ds_id in datasets:
            for t, fields in tabs.items():
                if is_auto_table(t):
                    continue
                ncols = len(fields.get("columns", [])) + len(fields.get("measures", []))
                lines.append(f'  {rid} ==>|{ncols} cols| {table_id(ds_id, t)}')
                used_tables.add((ds_id, t))
        elif ds_id in datasets:
            lines.append(f'  {rid} ==> {node_id("DS", ds_id)}')

    lines.append("  classDef report fill:#FFF3CD,stroke:#B8860B,color:#000;")
    lines.append("  classDef used fill:#D4EDDA,stroke:#28A745,color:#000;")
    rids = " ".join(node_id("R", r["id"]) for r in reports)
    if rids:
        lines.append(f"  class {rids} report;")
    if used_tables:
        lines.append("  class " + " ".join(table_id(d, t) for d, t in used_tables) + " used;")
    return "\n".join(lines)


def build_combined_graph(reports, datasets, usage):
    """
    End-to-end lineage in one diagram:
        data source (e.g. Snowflake)  ->  semantic-model tables  ->  reports
    Tables a report uses are highlighted; report edges are labeled with the
    number of fields used.
    """
    lines = ["flowchart LR"]
    used_tables = set()

    # source nodes (deduped across datasets), linked to the model they feed
    source_ids = {}
    for ds_id, ds in datasets.items():
        for s in ds["sources"]:
            label = f'{s["kind"]}<br/>{s["path"]}' if s["path"] else s["kind"]
            sid = node_id("SRC", f'{s["kind"]}_{s["path"]}')
            source_ids.setdefault(sid, label)
    for sid, label in source_ids.items():
        lines.append(f'  {sid}[("&#128202; {label}")]')

    for ds_id, ds in datasets.items():
        lines.append(f'  subgraph {node_id("DS", ds_id)}["&#129513; {ds["name"]}"]')
        lines.append("    direction TB")
        for t in ds["tables"]:
            lines.append(f'    {table_id(ds_id, t)}["{t}"]')
        for rel in ds["relationships"]:
            arrow = "-->" if rel["active"] else "-.->"
            lines.append(f'    {table_id(ds_id, rel["from"])} {arrow}'
                         f'|{rel["from_col"]}| {table_id(ds_id, rel["to"])}')
        lines.append("  end")
        # source -> model (table-level M mapping isn't available via the
        # delegated token, so we link the source to the model as a whole)
        for s in ds["sources"]:
            sid = node_id("SRC", f'{s["kind"]}_{s["path"]}')
            lines.append(f'  {sid} ==> {node_id("DS", ds_id)}')

    for r in reports:
        rid, ds_id = node_id("R", r["id"]), r.get("datasetId")
        lines.append(f'  {rid}["&#128202; {r["name"]}"]')
        rusage = usage.get(r["id"], {})
        tabs = rusage.get("tables") if isinstance(rusage, dict) else None
        if tabs and ds_id in datasets:
            for t, fields in tabs.items():
                if is_auto_table(t):
                    continue
                ncols = len(fields.get("columns", [])) + len(fields.get("measures", []))
                lines.append(f'  {table_id(ds_id, t)} ==>|{ncols} cols| {rid}')
                used_tables.add((ds_id, t))
        elif ds_id in datasets:
            lines.append(f'  {node_id("DS", ds_id)} ==> {rid}')

    lines.append("  classDef report fill:#FFF3CD,stroke:#B8860B,color:#000;")
    lines.append("  classDef source fill:#CCE5FF,stroke:#004085,color:#000;")
    lines.append("  classDef used fill:#D4EDDA,stroke:#28A745,color:#000;")
    rids = " ".join(node_id("R", r["id"]) for r in reports)
    if rids:
        lines.append(f"  class {rids} report;")
    if source_ids:
        lines.append("  class " + " ".join(source_ids) + " source;")
    if used_tables:
        lines.append("  class " + " ".join(table_id(d, t) for d, t in used_tables) + " used;")
    return "\n".join(lines)


def build_column_graph(reports, usage):
    """Report -> exact TABLE.COLUMN fields used. Empty string if no usage data."""
    if not usage:
        return ""
    lines = ["flowchart LR"]
    any_edge = False
    for r in reports:
        rid = node_id("R", r["id"])
        rusage = usage.get(r["id"], {})
        tabs = rusage.get("tables") if isinstance(rusage, dict) else None
        if not tabs:
            continue
        lines.append(f'  {rid}["&#128202; {r["name"]}"]')
        for t, fields in tabs.items():
            if is_auto_table(t):
                continue
            for col in fields.get("columns", []):
                cid = node_id("C", f"{t}_{col}")
                lines.append(f'  {cid}["{t}.{col}"]')
                lines.append(f"  {rid} --> {cid}")
                any_edge = True
            for m in fields.get("measures", []):
                mid = node_id("M", f"{t}_{m}")
                lines.append(f'  {mid}(["{t}.{m}"])')
                lines.append(f"  {rid} --> {mid}")
                any_edge = True
    lines.append("  classDef report fill:#FFF3CD,stroke:#B8860B,color:#000;")
    rids = " ".join(node_id("R", r["id"]) for r in reports if usage.get(r["id"], {}).get("tables"))
    if rids:
        lines.append(f"  class {rids} report;")
    return "\n".join(lines) if any_edge else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="metadata_output")
    args = ap.parse_args()

    reports, datasets, usage = collect(args.out)
    combined = build_combined_graph(reports, datasets, usage)
    model = build_model_graph(reports, datasets, usage)
    columns = build_column_graph(reports, usage)

    with open(os.path.join(args.out, "graph.mmd"), "w") as f:
        f.write(combined + "\n")

    md = ["# Power BI lineage graph", "",
          "## End-to-end lineage (source → model → report)", "",
          "Source systems feed semantic models; reports read the tables "
          "highlighted in green. Edge labels on report arrows are the number of "
          "fields used.",
          "", "```mermaid", combined, "```", "",
          "## Reports → Semantic Models → Tables", "",
          "Reports connect to the tables they actually use (green); edge labels "
          "are the number of fields used. Inside each model, solid arrows are "
          "active relationships, dashed are inactive, labels are the join column.",
          "", "```mermaid", model, "```", ""]
    if columns:
        md += ["## Reports → Columns used", "",
               "The exact `TABLE.COLUMN` fields each report references "
               "(rounded nodes are measures).",
               "", "```mermaid", columns, "```", ""]
    with open(os.path.join(args.out, "graph.md"), "w") as f:
        f.write("\n".join(md))

    n_tables = sum(len(d["tables"]) for d in datasets.values())
    print(f"Wrote graph for {len(reports)} report(s), {len(datasets)} dataset(s), "
          f"{n_tables} table(s); column graph: {'yes' if columns else 'no usage data'}.")
    print(f"  - {os.path.join(args.out, 'graph.md')}  (open the Markdown preview)")


if __name__ == "__main__":
    main()
