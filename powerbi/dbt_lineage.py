#!/usr/bin/env python3
"""
Link a dbt project's lineage to the extracted Power BI lineage.

The Power BI tooling (pull_powerbi_metadata.py + mermaid_graph.py) leaves the
trail at the warehouse: each Power BI table resolves to a Snowflake relation
(database + table). This CLI fetches the dbt `manifest.json`, matches dbt
models to those Power BI tables, and emits the joined source->dbt->Power BI
lineage.

The manifest source is configurable at launch:
  * pull from dbt platform (default), choosing project / environment / job /
    run, or
  * read a local manifest.json (e.g. `dbt parse` output).

Matching defaults to strict fully-qualified relation (database + table,
case-insensitive). `--match name` falls back to model name only (tolerating
version suffixes like _v1/_v2). Zero matches is a valid outcome and is
reported as such.

Usage:
    # Pull the manifest from the production env of a project, then link
    python3 dbt_lineage.py pull --project-id 70437463662253
    python3 dbt_lineage.py link

    # One shot: pull (latest prod run) + link, name-based matching
    python3 dbt_lineage.py run --project-id <id> --match name

    # Use a manifest you already have
    python3 dbt_lineage.py link --manifest ../target/manifest.json --match name

Run `python3 dbt_lineage.py <command> -h` for all options.
"""

import argparse
import glob
import json
import os
import re
import sys
import time

from dbt_client import (DbtCloudClient, DbtCloudError, model_nodes,
                        relation_parts, upstream_closure)
from mermaid_graph import node_id
from pbi_client import is_auto_table

DEFAULT_OUT = "metadata_output"
MANIFEST_FILE = "dbt_manifest.json"          # the dbt manifest (not the PBI one)
LINKS_FILE = "dbt_powerbi_links.json"
GRAPH_FILE = "lineage_with_dbt.md"

_VER_SUFFIX = re.compile(r"_v\d+$", re.IGNORECASE)


def load(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


# --------------------------------------------------------------------------- #
# pull
# --------------------------------------------------------------------------- #
def resolve_run(client, args):
    """Turn the user's source flags into a concrete (run, provenance)."""
    if args.run_id:
        return args.run_id, {"resolved_from": "run_id"}
    if args.job_id:
        run = client.latest_successful_run(job_id=args.job_id)
        return run["id"], {"resolved_from": "job_id", "job_id": args.job_id}
    if args.environment_id:
        run = client.latest_successful_run(environment_id=args.environment_id)
        return run["id"], {"resolved_from": "environment_id",
                           "environment_id": args.environment_id}
    if args.project_id:
        env = client.production_environment(args.project_id)
        run = client.latest_successful_run(environment_id=env["id"])
        return run["id"], {"resolved_from": "project_id production env",
                           "project_id": args.project_id,
                           "environment_id": env["id"],
                           "environment_name": env.get("name")}
    raise DbtCloudError(
        "Specify a manifest source: --run-id / --job-id / --environment-id / "
        "--project-id, or --manifest <path>. List projects with "
        "`dbt_lineage.py projects`.")


def cmd_projects(args):
    client = DbtCloudClient(args.host, args.account_id, args.token, args.env)
    print(f"Account {client.resolve_account_id()} on {client.host}\n")
    for p in client.list_projects():
        print(f"  project {p['id']}  {p['name']}")
        try:
            for e in client.list_environments(p["id"]):
                tag = e.get("deployment_type") or e.get("type")
                print(f"      env {e['id']}  [{tag}]  {e['name']}")
        except DbtCloudError as e:
            print(f"      (environments unavailable: {e})")
    return 0


def cmd_pull(args):
    out = args.out
    if args.manifest:                       # local file -> just copy in
        manifest = load(args.manifest)
        prov = {"source": "local", "path": os.path.abspath(args.manifest)}
    else:
        client = DbtCloudClient(args.host, args.account_id, args.token, args.env)
        run_id, prov = resolve_run(client, args)
        print(f"Downloading manifest from run {run_id} ...")
        manifest = client.download_manifest(run_id)
        prov.update({"source": "dbt_platform", "host": client.host,
                     "account_id": client.resolve_account_id(), "run_id": run_id})
    prov["pulled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    md = manifest.get("metadata", {})
    n_models = len(model_nodes(manifest))
    write_json(os.path.join(out, MANIFEST_FILE), manifest)
    write_json(os.path.join(out, "dbt_manifest_provenance.json"), prov)
    print(f"dbt project: {md.get('project_name')}  "
          f"(schema {md.get('dbt_schema_version','?').split('/')[-1]})")
    print(f"  {n_models} models, {len(manifest.get('sources',{}))} sources")
    print(f"  wrote {os.path.join(out, MANIFEST_FILE)}")
    return 0


# --------------------------------------------------------------------------- #
# link
# --------------------------------------------------------------------------- #
def pbi_tables(out_dir):
    """Yield Power BI tables as dicts with the warehouse relation we can see.

    Power BI exposes database + table via the datasource connection string
    (`server;database`), but not the schema, so the relation is (database,
    table). Returns one entry per (dataset, table)."""
    reports = load(os.path.join(out_dir, "reports.json")) \
        if os.path.exists(os.path.join(out_dir, "reports.json")) else []
    rep_by_ds = {}
    for r in reports:
        rep_by_ds.setdefault(r.get("datasetId"), []).append(r)

    tables = []
    for path in sorted(glob.glob(os.path.join(out_dir, "datasets", "*.json"))):
        d = load(path)
        ds_id = d.get("dataset_id") or d.get("dataset", {}).get("id")
        ds_name = d.get("dataset", {}).get("name", ds_id)
        # warehouse server/database from the Snowflake datasource (best effort)
        server = database = None
        for s in (d.get("datasources") or []):
            cd = s.get("connectionDetails", {}) if isinstance(s, dict) else {}
            if (cd.get("kind") or "").lower() == "snowflake" and cd.get("path"):
                parts = cd["path"].split(";")
                server = parts[0]
                database = parts[1] if len(parts) > 1 else None
                break
        for t in d.get("model", {}).get("tables", {}).get("rows", []):
            name = t["[Name]"]
            if is_auto_table(name) or t.get("[IsHidden]"):
                continue
            tables.append({
                "dataset_id": ds_id, "dataset_name": ds_name,
                "table": name, "server": server, "database": database,
                "reports": [{"id": r["id"], "name": r["name"]}
                            for r in rep_by_ds.get(ds_id, [])],
            })
    return tables


def base_name(name):
    return _VER_SUFFIX.sub("", name.lower())


def build_indexes(manifest, mode):
    """Map a matchable key -> list of model unique_ids."""
    idx = {}
    for uid, n in model_nodes(manifest).items():
        db, _sch, ident = relation_parts(n)
        if mode == "fqn":
            # schema is absent from Power BI, so the comparable FQN is
            # database + identifier (case-insensitive).
            key = (db, ident)
        else:                                # name
            key = base_name(ident)
        idx.setdefault(key, []).append(uid)
    return idx


def match_table(t, idx, mode):
    if mode == "fqn":
        if not t["database"]:
            return []
        return idx.get((t["database"].lower(), t["table"].lower()), [])
    return idx.get(base_name(t["table"]), [])


def cmd_link(args):
    out = args.out
    manifest_path = args.manifest or os.path.join(out, MANIFEST_FILE)
    if not os.path.exists(manifest_path):
        print(f"No manifest at {manifest_path}. Run `dbt_lineage.py pull` first "
              f"or pass --manifest.", file=sys.stderr)
        return 2
    manifest = load(manifest_path)
    models = model_nodes(manifest)
    sources = manifest.get("sources", {})
    idx = build_indexes(manifest, args.match)
    tables = pbi_tables(out)

    links, matched_uids = [], set()
    for t in tables:
        uids = match_table(t, idx, args.match)
        for uid in uids:
            matched_uids.add(uid)
        links.append({
            "power_bi": {"dataset": t["dataset_name"], "table": t["table"],
                         "server": t["server"], "database": t["database"],
                         "reports": t["reports"]},
            "matched": bool(uids),
            "dbt_models": [{
                "unique_id": uid,
                "name": models[uid]["name"],
                "relation": ".".join(p for p in relation_parts(models[uid])),
                "version": models[uid].get("version"),
            } for uid in uids],
        })

    report = {
        "match_mode": args.match,
        "dbt_project": manifest.get("metadata", {}).get("project_name"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "power_bi_tables": len(tables),
            "matched": sum(1 for l in links if l["matched"]),
            "unmatched": sum(1 for l in links if not l["matched"]),
            "dbt_models_in_manifest": len(models),
        },
        "links": links,
    }
    write_json(os.path.join(out, LINKS_FILE), report)

    write_graph(out, manifest, models, sources, tables, links, matched_uids,
                args.match)

    s = report["summary"]
    print(f"Match mode: {args.match}  |  dbt project: {report['dbt_project']}")
    print(f"Power BI tables: {s['power_bi_tables']}  "
          f"matched: {s['matched']}  unmatched: {s['unmatched']}")
    for l in links:
        mark = "OK " if l["matched"] else "-- "
        rel = ", ".join(m["relation"] for m in l["dbt_models"]) or "(no dbt match)"
        db = l["power_bi"]["database"] or "?"
        print(f"  {mark}{db}.{l['power_bi']['table']:24} -> {rel}")
    if s["matched"] == 0:
        print("\nNo matches. With --match fqn this usually means the dbt "
              "manifest targets a different database/account than Power BI. "
              "Re-run with `--match name`, or pull the manifest of the dbt "
              "project that actually builds these tables.")
    print(f"\nWrote {os.path.join(out, LINKS_FILE)}")
    print(f"Wrote {os.path.join(out, GRAPH_FILE)}  (open the Markdown preview)")
    return 0


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #
def write_graph(out, manifest, models, sources, tables, links, matched_uids, mode):
    """End-to-end Mermaid: dbt sources -> dbt models -> Power BI tables ->
    reports. Only the dbt ancestry of matched models is drawn (keeps it
    legible); if nothing matched, the dbt subgraph is omitted."""
    lines = ["flowchart LR"]
    nodes = manifest.get("nodes", {})

    closure = upstream_closure(manifest, matched_uids) if matched_uids else set()
    drawn_models = [uid for uid in closure if uid in models]
    drawn_sources = [uid for uid in closure if uid in sources]

    if matched_uids:
        proj = manifest.get("metadata", {}).get("project_name", "dbt")
        lines.append(f'  subgraph DBT["&#129516; dbt: {proj}"]')
        lines.append("    direction LR")
        for uid in drawn_sources:
            s = sources[uid]
            lbl = f'{s.get("source_name","")}.{s.get("name","")}'
            lines.append(f'    {node_id("S", uid)}[("&#128451; {lbl}")]')
        for uid in drawn_models:
            star = " &#11088;" if uid in matched_uids else ""
            lines.append(f'    {node_id("DM", uid)}["{models[uid]["name"]}{star}"]')
        for uid in drawn_models:                       # internal dbt edges
            for dep in nodes.get(uid, {}).get("depends_on", {}).get("nodes", []):
                if dep in closure:
                    pref = "DM" if dep in models else "S"
                    lines.append(f'    {node_id(pref, dep)} --> {node_id("DM", uid)}')
        lines.append("  end")

    # Power BI side: dataset subgraphs with their tables, then reports.
    by_ds = {}
    for t in tables:
        by_ds.setdefault((t["dataset_id"], t["dataset_name"]), []).append(t)
    link_by_table = {(l["power_bi"]["dataset"], l["power_bi"]["table"]): l
                     for l in links}
    used_tables = []
    for (ds_id, ds_name), ts in by_ds.items():
        lines.append(f'  subgraph {node_id("DS", ds_id)}["&#129513; {ds_name}"]')
        lines.append("    direction TB")
        for t in ts:
            tid = node_id("T", f'{ds_id}_{t["table"]}')
            lines.append(f'    {tid}["{t["table"]}"]')
        lines.append("  end")

    # cross edges: matched dbt model -> PBI table, then table -> report
    rep_nodes = {}
    for t in tables:
        tid = node_id("T", f'{t["dataset_id"]}_{t["table"]}')
        l = link_by_table.get((t["dataset_name"], t["table"]), {})
        for m in l.get("dbt_models", []):
            lines.append(f'  {node_id("DM", m["unique_id"])} ==>|{mode}| {tid}')
            used_tables.append(tid)
        for r in t["reports"]:
            rid = node_id("R", r["id"])
            rep_nodes[rid] = r["name"]
            lines.append(f'  {tid} --> {rid}')
    for rid, rname in rep_nodes.items():
        lines.append(f'  {rid}["&#128202; {rname}"]')

    lines.append("  classDef report fill:#FFF3CD,stroke:#B8860B,color:#000;")
    lines.append("  classDef used fill:#D4EDDA,stroke:#28A745,color:#000;")
    lines.append("  classDef dbt fill:#FFE5D0,stroke:#FF694A,color:#000;")
    if rep_nodes:
        lines.append("  class " + " ".join(rep_nodes) + " report;")
    if used_tables:
        lines.append("  class " + " ".join(sorted(set(used_tables))) + " used;")
    if matched_uids:
        dbt_ids = [node_id("DM", u) for u in drawn_models] + \
                  [node_id("S", u) for u in drawn_sources]
        lines.append("  class " + " ".join(dbt_ids) + " dbt;")
    graph = "\n".join(lines)

    matched = sum(1 for l in links if l["matched"])
    md = [f"# Power BI ↔ dbt lineage", "",
          f"Match mode: **{mode}** &nbsp;|&nbsp; "
          f"dbt project: **{manifest.get('metadata',{}).get('project_name')}** "
          f"&nbsp;|&nbsp; matched **{matched}/{len(tables)}** tables.", ""]
    if matched == 0:
        md += ["> No dbt models matched the Power BI tables under this mode. "
               "The dbt subgraph is omitted. See `dbt_powerbi_links.json` and "
               "try `--match name`.", ""]
    md += ["End-to-end: dbt sources → dbt models (⭐ = linked) → "
           "Power BI tables (green = has a dbt match) → reports.", "",
           "```mermaid", graph, "```", ""]
    with open(os.path.join(out, GRAPH_FILE), "w") as f:
        f.write("\n".join(md))


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def add_source_args(p):
    p.add_argument("--host", help="dbt platform host (e.g. tr995.us1.dbt.com)")
    p.add_argument("--account-id", help="dbt account id (auto-detected if omitted)")
    p.add_argument("--token", help="dbt token (else .env dbt_token / dbt_cloud.yml)")
    p.add_argument("--env", default=".env", help="path to .env (default ./.env)")
    p.add_argument("--manifest", help="use a local manifest.json instead of the API")
    p.add_argument("--project-id", help="pull latest prod run of this project")
    p.add_argument("--environment-id", help="pull latest successful run of this env")
    p.add_argument("--job-id", help="pull latest successful run of this job")
    p.add_argument("--run-id", help="pull this specific run's manifest")
    p.add_argument("--out", default=DEFAULT_OUT, help="output dir")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("projects", help="list projects/environments you can see")
    pp.add_argument("--host")
    pp.add_argument("--account-id")
    pp.add_argument("--token")
    pp.add_argument("--env", default=".env")
    pp.set_defaults(func=cmd_projects)

    pl = sub.add_parser("pull", help="fetch dbt manifest.json (platform or local)")
    add_source_args(pl)
    pl.set_defaults(func=cmd_pull)

    lk = sub.add_parser("link", help="link a manifest to the Power BI lineage")
    lk.add_argument("--manifest", help="manifest.json path (default: pulled one)")
    lk.add_argument("--match", choices=["fqn", "name"], default="fqn",
                    help="strict relation match (default) or model-name match")
    lk.add_argument("--out", default=DEFAULT_OUT)
    lk.set_defaults(func=cmd_link)

    rn = sub.add_parser("run", help="pull then link in one go")
    add_source_args(rn)
    rn.add_argument("--match", choices=["fqn", "name"], default="fqn")
    rn.set_defaults(func=lambda a: cmd_pull(a) or cmd_link(a))

    args = ap.parse_args()
    try:
        return args.func(args)
    except DbtCloudError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
