#!/usr/bin/env python3
"""
Pull all metadata from a Power BI workspace for lineage purposes.

Auth:   delegated token borrowed from your existing `az login`.
Deps:   none (Python stdlib only).
Output: one JSON file per artifact-type + per-dataset detail under ./metadata_output/

Usage:
    python3 pull_powerbi_metadata.py
    python3 pull_powerbi_metadata.py --workspace <group-id> --out metadata_output
"""

import argparse
import time

from pbi_client import BASE, PBIClient, err_message, write_json

DEFAULT_WORKSPACE = "89828a8a-8547-415e-9771-49a83274def4"


def resolve_workspaces(client, args):
    """Decide which workspace ids to scan, from the CLI args."""
    if args.all_workspaces:
        groups = client.get_value("/groups")
        ids = [g["id"] for g in groups]
        print(f"Discovered {len(ids)} accessible workspace(s).")
        return ids
    if args.workspaces:
        return [w.strip() for w in args.workspaces.split(",") if w.strip()]
    return [args.workspace]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="single workspace (group) id")
    ap.add_argument("--workspaces", help="comma-separated list of workspace ids")
    ap.add_argument("--all-workspaces", action="store_true",
                    help="scan every workspace the signed-in user can access")
    ap.add_argument("--out", default="metadata_output", help="output directory")
    args = ap.parse_args()
    out_dir = args.out

    client = PBIClient()
    workspaces = resolve_workspaces(client, args)
    print(f"Got Power BI token. Scanning {len(workspaces)} workspace(s).")

    manifest = {"workspaces": workspaces,
                "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "by_workspace": {}}

    # Merged, cross-workspace collections written once at the end.
    all_reports, all_datasets_meta, all_workspaces_info = [], [], []
    ds_seen = set()       # dataset ids already pulled (dedup across workspaces)
    ds_worklist = []      # (dataset_id, home_workspace, dataset_obj, via_report)

    for group in workspaces:
        status, ws = client.request("GET", f"{BASE}/groups/{group}")
        ws_name = ws.get("name") if status == 200 else "?"
        print(f"\n=== Workspace: {ws_name} ({group}) ===")
        if status == 200:
            all_workspaces_info.append(ws)

        inv = {}
        for kind in ("datasets", "reports", "dashboards", "dataflows"):
            inv[kind] = client.get_value(f"/groups/{group}/{kind}")
            print(f"  {kind}: {len(inv[kind])}")
        manifest["by_workspace"][group] = {"name": ws_name,
                                           "counts": {k: len(v) for k, v in inv.items()}}

        all_datasets_meta.extend(inv["datasets"])
        for rpt in inv["reports"]:
            rpt["containingWorkspaceId"] = group   # so usage tooling finds its home
            all_reports.append(rpt)

        # queue datasets in this workspace + any bound via its reports
        for ds in inv["datasets"]:
            if ds["id"] not in ds_seen:
                ds_seen.add(ds["id"])
                ds_worklist.append((ds["id"], group, ds, None))
        for rpt in inv["reports"]:
            ds_id = rpt.get("datasetId")
            if ds_id and ds_id not in ds_seen:
                ds_seen.add(ds_id)
                ds_worklist.append((ds_id, rpt.get("datasetWorkspaceId") or group,
                                    None, rpt.get("id")))

    # --- pull each unique dataset's deep metadata ----------------------------
    for ds_id, home, ds_obj, via_report in ds_worklist:
        label = ds_obj.get("name", ds_id) if ds_obj else ds_id
        origin = "" if ds_obj else " (external, via report)"
        print(f"\nDataset: {label}{origin}")
        detail = client.pull_dataset_detail(ds_id, home, dataset_obj=ds_obj,
                                            referenced_by_report=via_report)
        write_json(out_dir, f"datasets/{ds_id}.json", detail)

    # --- write merged collections + per-report files -------------------------
    for rpt in all_reports:
        write_json(out_dir, f"reports/{rpt['id']}.json", rpt)
    write_json(out_dir, "reports.json", all_reports)
    write_json(out_dir, "datasets.json", all_datasets_meta)
    write_json(out_dir, "workspaces.json", all_workspaces_info)
    if len(all_workspaces_info) == 1:
        write_json(out_dir, "workspace.json", all_workspaces_info[0])  # back-compat

    manifest["counts"] = {"workspaces": len(workspaces),
                          "datasets_pulled": len(ds_worklist),
                          "reports": len(all_reports)}
    write_json(out_dir, "manifest.json", manifest)
    print(f"\nDone. Metadata for {len(workspaces)} workspace(s), {len(ds_worklist)} dataset(s), "
          f"{len(all_reports)} report(s) written to ./{out_dir}/")


if __name__ == "__main__":
    main()
