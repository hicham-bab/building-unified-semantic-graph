#!/usr/bin/env python3
"""
Pull each report's PBIR definition and extract which TABLE.COLUMN / TABLE.MEASURE
fields it actually uses (across visuals, filters, sorts).

Reads:  metadata_output/reports.json  (for report ids + their workspace)
Writes: metadata_output/report_definitions/<reportId>/...  (raw decoded parts)
        metadata_output/report_column_usage.json           (structured usage)
        metadata_output/report_usage.md                    (readable summary)

Usage:
    python3 pull_report_usage.py [--out metadata_output] [--workspace <id>]
"""

import argparse
import json
import os

from pbi_client import PBIClient, is_auto_table, write_json

DEFAULT_WORKSPACE = "89828a8a-8547-415e-9771-49a83274def4"


def build_alias_map(node, acc=None):
    """Collect alias->entity from all `From` clauses ({Name, Entity}) in a part."""
    if acc is None:
        acc = {}
    if isinstance(node, dict):
        if isinstance(node.get("From"), list):
            for f in node["From"]:
                if isinstance(f, dict) and f.get("Name") and f.get("Entity"):
                    acc[f["Name"]] = f["Entity"]
        for v in node.values():
            build_alias_map(v, acc)
    elif isinstance(node, list):
        for v in node:
            build_alias_map(v, acc)
    return acc


def find_entity(expr, alias_map):
    """
    Recursively resolve the table name under expr. A SourceRef carries either a
    full `Entity` name or a `Source` alias that maps to an entity via `From`.
    """
    if isinstance(expr, dict):
        ref = expr.get("SourceRef")
        if isinstance(ref, dict):
            if ref.get("Entity"):
                return ref["Entity"]
            if ref.get("Source"):
                return alias_map.get(ref["Source"])
        for v in expr.values():
            ent = find_entity(v, alias_map)
            if ent:
                return ent
    elif isinstance(expr, list):
        for v in expr:
            ent = find_entity(v, alias_map)
            if ent:
                return ent
    return None


def extract_field_refs(node, alias_map, parent_key=None, acc=None):
    """
    Walk an arbitrary PBIR JSON fragment and collect (entity, property, kind)
    field references. Handles Column / Measure / Aggregation(Column) /
    PropertyVariationSource (auto date hierarchies), which all share the
    `{Expression: {...SourceRef...}, Property: "..."}` shape. Auto date tables
    are dropped as lineage noise.
    """
    if acc is None:
        acc = set()
    if isinstance(node, dict):
        prop = node.get("Property")
        if isinstance(prop, str) and "Expression" in node:
            entity = find_entity(node["Expression"], alias_map)
            if entity and not is_auto_table(entity):
                kind = "measure" if parent_key == "Measure" else "column"
                acc.add((entity, prop, kind))
        for k, v in node.items():
            extract_field_refs(v, alias_map, k, acc)
    elif isinstance(node, list):
        for v in node:
            extract_field_refs(v, alias_map, parent_key, acc)
    return acc


def usage_for_report(parts):
    """
    Given decoded definition parts, return per-report usage:
    { "tables": {entity: {"columns": [..], "measures": [..]}},
      "by_page": {pageName: {visualCount, fields:[...]}} }
    """
    tables = {}
    pages = {}

    def record(entity, prop, kind):
        t = tables.setdefault(entity, {"columns": set(), "measures": set()})
        t["measures" if kind == "measure" else "columns"].add(prop)

    for path, obj in parts.items():
        if not (path.endswith("visual.json") or path.endswith("page.json")
                or path.endswith("report.json")):
            continue
        if not isinstance(obj, (dict, list)):
            continue
        alias_map = build_alias_map(obj)
        refs = extract_field_refs(obj, alias_map)
        for entity, prop, kind in refs:
            record(entity, prop, kind)
        # roll up per page for the visual parts
        if path.endswith("visual.json"):
            page = path.split("/pages/")[1].split("/")[0]
            p = pages.setdefault(page, {"visuals": 0, "fields": set()})
            p["visuals"] += 1
            for entity, prop, kind in refs:
                p["fields"].add(f"{entity}.{prop}")

    return {
        "tables": {e: {"columns": sorted(v["columns"]),
                       "measures": sorted(v["measures"])}
                   for e, v in sorted(tables.items())},
        "by_page": {pg: {"visuals": d["visuals"], "fields": sorted(d["fields"])}
                    for pg, d in pages.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="metadata_output")
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                    help="fallback workspace id for reports that don't record one")
    args = ap.parse_args()

    reports = json.load(open(os.path.join(args.out, "reports.json")))
    client = PBIClient()
    print(f"Pulling definitions for {len(reports)} report(s)...")

    all_usage = {}
    for rpt in reports:
        rid, rname = rpt["id"], rpt.get("name", rpt["id"])
        # Each report records its own containing workspace at pull time; fall
        # back to --workspace for older metadata. getDefinition needs the
        # report's real (non-personal) home workspace.
        ws = rpt.get("containingWorkspaceId") or args.workspace
        print(f"\nReport: {rname} ({rid})")
        parts = client.get_report_definition(ws, rid)
        if "error" in parts:
            print(f"  ! {parts['error']}")
            all_usage[rid] = {"name": rname, "error": parts["error"]}
            continue

        # persist decoded parts for inspection / re-parsing
        for path, obj in parts.items():
            safe = path.replace("/", "__")
            full = os.path.join(args.out, "report_definitions", rid, safe)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(json.dumps(obj, indent=2) if isinstance(obj, (dict, list)) else obj)

        usage = usage_for_report(parts)
        usage["name"] = rname
        usage["dataset_id"] = rpt.get("datasetId")
        all_usage[rid] = usage
        ncols = sum(len(t["columns"]) for t in usage["tables"].values())
        nmeas = sum(len(t["measures"]) for t in usage["tables"].values())
        print(f"  tables used: {len(usage['tables'])}, columns: {ncols}, measures: {nmeas}")

    write_json(args.out, "report_column_usage.json", all_usage)
    _write_markdown(args.out, all_usage)
    print(f"\nWrote {os.path.join(args.out, 'report_column_usage.json')} and report_usage.md")


def _write_markdown(out_dir, all_usage):
    lines = ["# Report column usage\n",
             "Which tables/columns each report actually references "
             "(parsed from the PBIR report definition).\n"]
    for rid, u in all_usage.items():
        lines.append(f"## {u.get('name', rid)}\n")
        if "error" in u:
            lines.append(f"_Could not read definition: {u['error']}_\n")
            continue
        lines.append(f"- report id: `{rid}`")
        lines.append(f"- bound dataset: `{u.get('dataset_id')}`\n")
        for entity, fields in u["tables"].items():
            cols = ", ".join(f"`{c}`" for c in fields["columns"]) or "_(none)_"
            lines.append(f"**{entity}** — columns: {cols}")
            if fields["measures"]:
                lines.append("  measures: " + ", ".join(f"`{m}`" for m in fields["measures"]))
        lines.append("")
    with open(os.path.join(out_dir, "report_usage.md"), "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
