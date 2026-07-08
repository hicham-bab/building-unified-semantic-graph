"""Optional live enrichment from a running dbt Fusion language server.

Fusion's socket transport is **reverse**: `dbt lsp --socket <PORT>` does not
listen — it *connects out* to an LSP client that is already listening on <PORT>
(the editor model). So this module listens on the port, (optionally) spawns the
server pointed at it, accepts the connection, performs the LSP
`initialize`/`initialized` handshake, records the server's capabilities and any
published diagnostics into the graph, and attempts a best-effort column-lineage
request.

This path is *optional* — the graph is fully built from static artifacts without
it. Anything that fails here is recorded as a warning and never aborts the build.

Note: the column-lineage request method (`CLL_METHODS`) is a Fusion server
extension, not part of base LSP. We probe a few candidate names; if none are
supported the base graph is unaffected.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from typing import Any

from graph_model import Graph, COLUMN, DERIVED_FROM

# Extension points: candidate method names for column-level lineage.
CLL_METHODS = (
    "dbt/columnLineage", "dbt/lineage", "textDocument/columnLineage",
    "$/dbt/columnLineage",
)


class LspError(Exception):
    pass


class JsonRpcTransport:
    """LSP JSON-RPC framing over an already-connected socket."""

    def __init__(self, sock: socket.socket, timeout: float = 20.0) -> None:
        self.sock = sock
        self.sock.settimeout(timeout)
        self._buf = b""
        self._id = 0
        self.last_diagnostics: list[dict] = []

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.sock.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)

    def request(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self._await(rid)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _read_message(self) -> dict:
        while b"\r\n\r\n" not in self._buf:
            self._recv_more()
        header, _, rest = self._buf.partition(b"\r\n\r\n")
        length = 0
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        self._buf = rest
        while len(self._buf) < length:
            self._recv_more()
        body, self._buf = self._buf[:length], self._buf[length:]
        return json.loads(body.decode("utf-8"))

    def _recv_more(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise LspError("connection closed by server")
        self._buf += chunk

    def _await(self, rid: int, deadline: float = 30.0) -> Any:
        end = time.time() + deadline
        while time.time() < end:
            msg = self._read_message()
            if msg.get("id") == rid:
                if "error" in msg:
                    raise LspError(str(msg["error"]))
                return msg.get("result")
            if msg.get("method") == "textDocument/publishDiagnostics":
                self.last_diagnostics.append(msg.get("params", {}))
        raise LspError(f"timed out awaiting response to request {rid}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def enrich_from_lsp(
    graph: Graph,
    port: int,
    project_dir: str,
    warn: list[str],
    host: str = "127.0.0.1",
    spawn: bool = True,
    executable: str = "dbt",
    profiles_dir: str | None = None,
) -> None:
    """Listen on `port`, (optionally) spawn `dbt lsp --socket port`, and enrich.

    If `spawn` is False, an external `dbt lsp --socket port` is expected to connect.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        listener.listen(1)
        listener.settimeout(45.0)
    except OSError as exc:
        warn.append(f"[lsp] could not listen on {host}:{port}: {exc}")
        return

    proc: subprocess.Popen | None = None
    if spawn:
        cmd = [executable, "lsp", "--socket", str(port), "--project-dir", project_dir]
        if profiles_dir:
            cmd += ["--profiles-dir", profiles_dir]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except OSError as exc:
            warn.append(f"[lsp] could not spawn `{' '.join(cmd)}`: {exc}")
            listener.close()
            return

    rpc: JsonRpcTransport | None = None
    try:
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            warn.append("[lsp] timed out waiting for the language server to connect")
            return
        rpc = JsonRpcTransport(conn)

        caps = rpc.request("initialize", {
            "processId": os.getpid(),
            "rootUri": f"file://{project_dir}",
            "capabilities": {},
            "clientInfo": {"name": "building-unified-semantic-graph"},
        })
        rpc.notify("initialized", {})
        graph.metadata["lsp"] = {
            "connected": True,
            "server": (caps or {}).get("serverInfo"),
            "capabilities": sorted((caps or {}).get("capabilities", {}).keys()),
        }

        _try_column_lineage(rpc, graph, warn)

        if rpc.last_diagnostics:
            graph.metadata["lsp"]["diagnostics_documents"] = len(rpc.last_diagnostics)

        try:
            rpc.request("shutdown")
            rpc.notify("exit")
        except LspError:
            pass
    except LspError as exc:
        warn.append(f"[lsp] enrichment failed: {exc}")
        graph.metadata.setdefault("lsp", {}).update({"connected": True, "error": str(exc)})
    finally:
        if rpc:
            rpc.close()
        listener.close()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _try_column_lineage(rpc: JsonRpcTransport, graph: Graph, warn: list[str]) -> None:
    for method in CLL_METHODS:
        try:
            result = rpc.request(method, {})
        except LspError:
            continue
        added = 0
        for src, tgt in _iter_lineage_edges(result):
            s = graph.node(COLUMN, "dbt", str(src).lower(), str(src))
            t = graph.node(COLUMN, "dbt", str(tgt).lower(), str(tgt))
            graph.add_edge(DERIVED_FROM, t.id, s.id, source="lsp")
            added += 1
        graph.metadata.setdefault("lsp", {})["cll_method"] = method
        graph.metadata["lsp"]["cll_edges"] = added
        return
    warn.append(
        f"[lsp] no column-lineage method among {CLL_METHODS} was supported; "
        "base graph is unaffected"
    )


def _iter_lineage_edges(result: Any):
    if not result:
        return
    rows = result if isinstance(result, list) else result.get("edges", [])
    for row in rows or []:
        if isinstance(row, dict):
            src = row.get("source") or row.get("from") or row.get("upstream")
            tgt = row.get("target") or row.get("to") or row.get("downstream")
            if src and tgt:
                yield src, tgt
