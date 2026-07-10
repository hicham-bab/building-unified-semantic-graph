#!/usr/bin/env python3
"""Render a human-readable lineage summary (LINEAGE.md) from the pulled JSON."""
import glob
import json
import os

OUT = "metadata_output"


def load(path):
    with open(path) as f:
        return json.load(f)


def main():
    reports = load(os.path.join(OUT, "reports.json"))
    ws = load(os.path.join(OUT, "workspace.json")) if os.path.exists(os.path.join(OUT, "workspace.json")) else {}
    lines = []
    lines.append(f"# Power BI lineage — {ws.get('name', 'workspace')}\n")
    lines.append(f"Workspace id: `{ws.get('id', '')}`\n")

    # reports -> dataset bindings
    lines.append("## Reports → datasets\n")
    for r in reports:
        lines.append(f"- **{r['name']}** (`{r['id']}`) → dataset `{r.get('datasetId')}` "
                     f"in workspace `{r.get('datasetWorkspaceId')}`")
    lines.append("")

    # datasets
    for path in sorted(glob.glob(os.path.join(OUT, "datasets", "*.json"))):
        d = load(path)
        ds = d.get("dataset", {})
        model = d.get("model", {})
        name = ds.get("name", d.get("dataset_id"))
        lines.append(f"## Dataset: {name}\n")
        lines.append(f"- id: `{d.get('dataset_id', ds.get('id'))}`")
        lines.append(f"- configured by: {ds.get('configuredBy', '?')}")
        lines.append(f"- storage mode: {ds.get('targetStorageMode', '?')}")

        # datasources
        dsr = d.get("datasources")
        if isinstance(dsr, list) and dsr:
            lines.append("\n### Upstream data sources")
            for s in dsr:
                cd = s.get("connectionDetails", {})
                lines.append(f"- {cd.get('kind', s.get('datasourceType'))}: `{cd.get('path', cd.get('server',''))}`"
                             + (f" / {cd.get('database')}" if cd.get('database') else ""))

        # tables (visible only)
        tabs = model.get("tables", {}).get("rows", [])
        vis = [t for t in tabs if not t.get("[IsHidden]")]
        lines.append(f"\n### Tables ({len(vis)} visible, {len(tabs)} total incl. date tables)")
        for t in vis:
            lines.append(f"- `{t['[Name]']}` ({t.get('[StorageMode]')})")

        # columns grouped by table (visible tables)
        cols = model.get("columns", {}).get("rows", [])
        if cols:
            lines.append(f"\n### Columns ({len(cols)} total)")
            by_tab = {}
            for c in cols:
                by_tab.setdefault(c.get("[Table]"), []).append(c)
            visnames = {t["[Name]"] for t in vis}
            for tab in sorted(n for n in by_tab if n in visnames):
                names = [f"{c['[Name]']}:{c.get('[DataType]','')}" for c in by_tab[tab]
                         if not c.get("[IsHidden]")]
                lines.append(f"- **{tab}**: " + ", ".join(names))

        # measures
        meas = model.get("measures", {}).get("rows", [])
        lines.append(f"\n### Measures ({len(meas)})")
        for m in meas:
            expr = (m.get("[Expression]") or "").replace("\n", " ")
            lines.append(f"- `{m.get('[Name]')}` = {expr}")

        # relationships
        rels = model.get("relationships", {}).get("rows", [])
        biz = [r for r in rels if not r["[ToTable]"].startswith(("LocalDateTable", "DateTableTemplate"))
               and not r["[FromTable]"].startswith(("LocalDateTable", "DateTableTemplate"))]
        lines.append(f"\n### Relationships ({len(biz)} business, {len(rels)} total incl. auto date)")
        for r in biz:
            star = "" if r["[IsActive]"] else "  _(inactive)_"
            lines.append(f"- `{r['[FromTable]']}[{r['[FromColumn]']}]` "
                         f"{r['[FromCardinality]']}→{r['[ToCardinality]']} "
                         f"`{r['[ToTable]']}[{r['[ToColumn]']}]`{star}")

        # note unavailable best-effort
        gaps = [k for k in ("partitions", "expressions") if "error" in model.get(k, {})]
        if gaps:
            lines.append(f"\n_Note: {', '.join(gaps)} (exact M source-query mapping) require a "
                         f"Fabric-admin token or XMLA endpoint; not available via the delegated user token._")
        lines.append("")

    md = "\n".join(lines)
    with open(os.path.join(OUT, "LINEAGE.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
