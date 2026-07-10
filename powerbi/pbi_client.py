#!/usr/bin/env python3
"""
Shared Power BI REST client + helpers for the lineage tooling.

Auth: borrows a delegated token from your existing `az login`.
Deps: Python stdlib only.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PBI_RESOURCE = "https://analysis.windows.net/powerbi/api"
BASE = "https://api.powerbi.com/v1.0/myorg"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"

# INFO.VIEW.* is the friendly, name-resolved view and works with a plain
# delegated user token. These cover everything lineage needs at table/column
# level.
DAX_INFO_QUERIES = {
    "tables": "EVALUATE INFO.VIEW.TABLES()",
    "columns": "EVALUATE INFO.VIEW.COLUMNS()",
    "measures": "EVALUATE INFO.VIEW.MEASURES()",
    "relationships": "EVALUATE INFO.VIEW.RELATIONSHIPS()",
}

# Best-effort: the raw INFO.* tables expose the exact Power Query "M" source
# query (table -> Snowflake object) but are blocked for non-admin tokens. They
# are recorded with their error so a privileged (Fabric admin / XMLA) re-run
# fills them in without code changes.
DAX_INFO_QUERIES_BEST_EFFORT = {
    "partitions": "EVALUATE INFO.PARTITIONS()",
    "expressions": "EVALUATE INFO.EXPRESSIONS()",
    "datasources": "EVALUATE INFO.DATASOURCES()",
}


# Power BI auto-creates a hidden date table per Date column; these are lineage
# noise (named LocalDateTable_<guid> or DateTableTemplate_<guid>).
AUTO_TABLE_PREFIXES = ("LocalDateTable", "DateTableTemplate")


def is_auto_table(name):
    return bool(name) and name.startswith(AUTO_TABLE_PREFIXES)


def err_message(payload):
    """Pull a short human-readable message out of a PBI error payload."""
    if isinstance(payload, dict):
        if "error" in payload:
            err = payload["error"]
            if isinstance(err, dict):
                return err.get("message") or err.get("code") or json.dumps(err)[:300]
            return str(err)
        if "error_text" in payload:
            return payload["error_text"][:300]
    return str(payload)[:300]


def write_json(out_dir, name, obj):
    """Write `obj` as pretty JSON to out_dir/name, creating dirs as needed."""
    path = os.path.join(out_dir, name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    return path


class PBIClient:
    """Thin Power BI REST client over a delegated az token."""

    def __init__(self, token=None):
        self.token = token or self._token_from_az()

    @staticmethod
    def _token_from_az():
        try:
            out = subprocess.run(
                ["az", "account", "get-access-token", "--resource", PBI_RESOURCE,
                 "--query", "accessToken", "-o", "tsv"],
                capture_output=True, text=True, check=True,
            )
        except FileNotFoundError:
            sys.exit("ERROR: az CLI not found. Install it or run `az login` first.")
        except subprocess.CalledProcessError as e:
            sys.exit(f"ERROR: could not get a Power BI token via az.\n{e.stderr}")
        token = out.stdout.strip()
        if not token:
            sys.exit("ERROR: empty token from az. Run `az login` and retry.")
        return token

    def request(self, method, url, body=None):
        """Make a REST call. Returns (status, parsed_json_or_error_dict)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"error_text": raw}
        except urllib.error.URLError as e:
            return None, {"error_text": str(e)}

    def get_value(self, path_or_url, warn=True):
        """GET a list endpoint, returning the `value` array (or [] on error)."""
        url = path_or_url if path_or_url.startswith("http") else f"{BASE}{path_or_url}"
        status, payload = self.request("GET", url)
        if status == 200:
            return payload.get("value", [])
        if warn:
            print(f"  ! GET {url} -> {status}: {err_message(payload)}")
        return []

    def run_dax(self, execute_queries_url, dax):
        """Run a single DAX query against a dataset's executeQueries URL."""
        body = {"queries": [{"query": dax}],
                "serializerSettings": {"includeNulls": True}}
        status, payload = self.request("POST", execute_queries_url, body)
        if status == 200:
            try:
                return {"rows": payload["results"][0]["tables"][0]["rows"]}
            except (KeyError, IndexError):
                return {"rows": [], "raw": payload}
        return {"error": err_message(payload), "status": status}

    def get_report_definition(self, workspace_id, report_id, poll_interval=3, max_polls=40):
        """
        Fetch a report's PBIR definition via the Fabric getDefinition LRO.
        Returns {path: decoded_text_or_json} for each definition part, or
        {"error": ...} on failure.
        """
        url = f"{FABRIC_BASE}/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
        status, headers, body = self._raw("POST", url, b"")
        if status == 200:
            return self._decode_parts(json.loads(body))
        if status != 202:
            return {"error": f"getDefinition -> {status}: {body[:300]}"}

        op = headers.get("Location")
        for _ in range(max_polls):
            time.sleep(poll_interval)
            _, _, b = self._raw("GET", op)
            state = json.loads(b).get("status") if b else None
            if state == "Succeeded":
                _, _, rb = self._raw("GET", op.rstrip("/") + "/result")
                return self._decode_parts(json.loads(rb))
            if state == "Failed":
                return {"error": f"getDefinition operation failed: {b[:300]}"}
        return {"error": "getDefinition operation timed out"}

    def _raw(self, method, url, data=None):
        """Like request() but returns (status, headers, raw_text); used for LRO."""
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, resp.headers, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode(errors="replace")
        except urllib.error.URLError as e:
            return None, {}, str(e)

    @staticmethod
    def _decode_parts(payload):
        """Decode base64 definition parts; parse JSON parts into objects."""
        out = {}
        for part in payload.get("definition", {}).get("parts", []):
            path = part["path"]
            try:
                text = base64.b64decode(part["payload"]).decode("utf-8")
            except Exception:
                out[path] = {"_undecoded": True}
                continue
            if path.endswith(".json") or path.endswith(".pbir") or path.endswith(".platform"):
                try:
                    out[path] = json.loads(text)
                    continue
                except json.JSONDecodeError:
                    pass
            out[path] = text
        return out

    def resolve_dataset_base(self, dataset_id, workspace_id):
        """
        Return the API base URL for a dataset, handling personal ("My Workspace")
        datasets which reject the /groups/{id}/ path. Returns (base_url, status, dataset_obj).
        """
        group_base = f"{BASE}/groups/{workspace_id}/datasets/{dataset_id}"
        personal_base = f"{BASE}/datasets/{dataset_id}"
        status, obj = self.request("GET", group_base)
        if status == 200:
            return group_base, status, obj
        status, obj = self.request("GET", personal_base)
        if status == 200:
            return personal_base, status, obj
        # neither worked; report against the group path we tried first
        return group_base, status, obj

    def pull_dataset_detail(self, dataset_id, workspace_id, dataset_obj=None,
                            referenced_by_report=None, verbose=True):
        """
        Pull a dataset's full detail: object, datasources, refresh history, and
        model internals (tables/columns/measures/relationships, plus best-effort
        partitions/expressions/datasources). Workspace-location agnostic.
        """
        ds_base, status, resolved = self.resolve_dataset_base(dataset_id, workspace_id)
        obj = dataset_obj or (resolved if status == 200 else {"error": err_message(resolved)})
        name = obj.get("name", dataset_id) if isinstance(obj, dict) else dataset_id
        is_personal = ds_base.endswith(f"/datasets/{dataset_id}") and "/groups/" not in ds_base
        if verbose:
            via = "personal" if is_personal else "group"
            print(f"  dataset: {name}  (via {via} endpoint)")

        detail = {
            "dataset_id": dataset_id,
            "home_workspace": workspace_id,
            "dataset_api_base": ds_base,
            "is_personal_workspace": is_personal,
            "referenced_by_report": referenced_by_report,
            "dataset": obj,
        }
        detail["datasources"] = self.get_value(f"{ds_base}/datasources")
        detail["refresh_history"] = self.get_value(f"{ds_base}/refreshes?$top=5", warn=False)

        detail["model"] = {}
        eq_url = f"{ds_base}/executeQueries"
        for label, dax in {**DAX_INFO_QUERIES, **DAX_INFO_QUERIES_BEST_EFFORT}.items():
            result = self.run_dax(eq_url, dax)
            detail["model"][label] = result
            if verbose:
                n = len(result.get("rows", [])) if "rows" in result else "ERR"
                note = f" ({result['error']})" if "error" in result else ""
                print(f"    {label}: {n}{note}")
        return detail
