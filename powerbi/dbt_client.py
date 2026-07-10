#!/usr/bin/env python3
"""
Shared dbt platform (dbt Cloud) Administrative API client + manifest helpers
for linking dbt lineage to the extracted Power BI lineage.

Auth: a dbt platform token (service token or PAT). Resolved, in order, from
      an explicit argument, the `dbt_token` / `DBT_TOKEN` env or .env entry,
      then ~/.dbt/dbt_cloud.yml.
Host: cell-based host such as `tr995.us1.dbt.com`. Resolved from an explicit
      argument, `DBT_HOST`/.env, then the active-host in ~/.dbt/dbt_cloud.yml.
Deps: Python stdlib only.

Docs: https://docs.getdbt.com/dbt-cloud/api-v2  (Administrative API)
      Artifacts: GET /api/v2/accounts/{a}/runs/{r}/artifacts/manifest.json
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_HOST = "cloud.getdbt.com"
DBT_CLOUD_YML = os.path.expanduser("~/.dbt/dbt_cloud.yml")


# --------------------------------------------------------------------------- #
# Config resolution
# --------------------------------------------------------------------------- #
def load_dotenv(path=".env"):
    """Minimal .env reader (KEY=VALUE per line); never overwrites real env."""
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def _read_dbt_cloud_yml():
    """Best-effort scrape of ~/.dbt/dbt_cloud.yml without a YAML dependency.

    Returns {'host':..., 'account_id':..., 'token':...} from the active
    context / first project. Good enough for the simple file the CLI writes.
    """
    out = {}
    if not os.path.exists(DBT_CLOUD_YML):
        return out
    with open(DBT_CLOUD_YML) as f:
        for raw in f:
            line = raw.strip()
            for key, dest in (("active-host:", "host"), ("account-host:", "host"),
                              ("account-id:", "account_id"), ("token-value:", "token")):
                if line.startswith(key) and dest not in out:
                    out[dest] = line.split(":", 1)[1].strip().strip('"').strip("'")
    return out


def resolve_config(host=None, account_id=None, token=None, env_path=".env"):
    """Layer explicit args over .env over ~/.dbt/dbt_cloud.yml over defaults."""
    dotenv = load_dotenv(env_path)
    ycfg = _read_dbt_cloud_yml()

    token = (token or os.environ.get("DBT_TOKEN") or dotenv.get("dbt_token")
             or dotenv.get("DBT_TOKEN") or ycfg.get("token"))
    host = (host or os.environ.get("DBT_HOST") or dotenv.get("dbt_host")
            or dotenv.get("DBT_HOST") or ycfg.get("host") or DEFAULT_HOST)
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    account_id = (account_id or os.environ.get("DBT_ACCOUNT_ID")
                  or dotenv.get("dbt_account_id") or ycfg.get("account_id"))
    return host, account_id, token


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class DbtCloudError(RuntimeError):
    pass


class DbtCloudClient:
    def __init__(self, host=None, account_id=None, token=None, env_path=".env"):
        self.host, self.account_id, self.token = resolve_config(
            host, account_id, token, env_path)
        if not self.token:
            raise DbtCloudError(
                "No dbt token found (set dbt_token in .env, DBT_TOKEN, or "
                "~/.dbt/dbt_cloud.yml).")

    # -- low level --------------------------------------------------------- #
    def request(self, path, raw=False, timeout=120):
        """GET an absolute API path (starting with /). Returns parsed JSON,
        or raw bytes when raw=True (used for artifact downloads)."""
        url = f"https://{self.host}{path}"
        # Artifact downloads 406 on a strict json Accept; ask for anything.
        req = urllib.request.Request(url, headers={
            "Authorization": f"Token {self.token}",
            "Accept": "*/*" if raw else "application/json",
            "User-Agent": "powerbi-dbt-lineage/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise DbtCloudError(f"{e.code} {e.reason} for {path}\n{detail}")
        except urllib.error.URLError as e:
            raise DbtCloudError(f"Network error for {url}: {e.reason}")
        if raw:
            return body
        return json.loads(body)

    def _data(self, path, **kw):
        payload = self.request(path, **kw)
        if isinstance(payload, dict) and not payload.get("status", {}).get(
                "is_success", True):
            raise DbtCloudError(payload["status"].get("user_message", "API error"))
        return payload.get("data") if isinstance(payload, dict) else payload

    # -- discovery --------------------------------------------------------- #
    def list_accounts(self):
        return self._data("/api/v2/accounts/")

    def resolve_account_id(self):
        if self.account_id:
            return str(self.account_id)
        accounts = self.list_accounts()
        if not accounts:
            raise DbtCloudError("Token can see no accounts.")
        self.account_id = str(accounts[0]["id"])
        return self.account_id

    def list_projects(self):
        a = self.resolve_account_id()
        return self._data(f"/api/v3/accounts/{a}/projects/")

    def list_environments(self, project_id):
        a = self.resolve_account_id()
        return self._data(
            f"/api/v3/accounts/{a}/projects/{project_id}/environments/")

    def production_environment(self, project_id):
        envs = self.list_environments(project_id)
        prod = [e for e in envs if e.get("deployment_type") == "production"]
        if not prod:
            prod = [e for e in envs if e.get("type") == "deployment"]
        if not prod:
            raise DbtCloudError(
                f"No production/deployment environment in project {project_id}.")
        return prod[0]

    def list_jobs(self, project_id=None):
        a = self.resolve_account_id()
        q = f"?project_id={project_id}" if project_id else ""
        return self._data(f"/api/v2/accounts/{a}/jobs/{q}")

    def latest_successful_run(self, environment_id=None, job_id=None):
        """Most recent successful (status 10) run for an env or job."""
        a = self.resolve_account_id()
        q = ["order_by=-finished_at", "limit=1", "status=10"]
        if environment_id:
            q.append(f"environment_id={environment_id}")
        if job_id:
            q.append(f"job_definition_id={job_id}")
        runs = self._data(f"/api/v2/accounts/{a}/runs/?{'&'.join(q)}")
        if not runs:
            raise DbtCloudError(
                "No successful run found for the given environment/job.")
        return runs[0]

    def download_manifest(self, run_id):
        """Return the parsed manifest.json artifact for a run."""
        a = self.resolve_account_id()
        body = self.request(
            f"/api/v2/accounts/{a}/runs/{run_id}/artifacts/manifest.json",
            raw=True)
        return json.loads(body)


# --------------------------------------------------------------------------- #
# Manifest helpers
# --------------------------------------------------------------------------- #
def model_nodes(manifest):
    """unique_id -> node, for resource_type == 'model'."""
    return {uid: n for uid, n in manifest.get("nodes", {}).items()
            if n.get("resource_type") == "model"}


def relation_parts(node):
    """(database, schema, identifier) for a model/source node, lower-cased.
    Identifier is the alias (what actually lands in the warehouse)."""
    db = (node.get("database") or "").lower()
    sch = (node.get("schema") or "").lower()
    ident = (node.get("alias") or node.get("identifier") or node.get("name") or "").lower()
    return db, sch, ident


def upstream_closure(manifest, seed_uids):
    """All ancestor node/source unique_ids of the seed models (inclusive of
    intermediate models; sources collected separately by the caller)."""
    nodes = manifest.get("nodes", {})
    sources = manifest.get("sources", {})
    seen, stack = set(), list(seed_uids)
    while stack:
        uid = stack.pop()
        if uid in seen:
            continue
        seen.add(uid)
        obj = nodes.get(uid) or sources.get(uid) or {}
        for dep in obj.get("depends_on", {}).get("nodes", []):
            if dep not in seen:
                stack.append(dep)
    return seen
