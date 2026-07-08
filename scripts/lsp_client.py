"""Optional live enrichment from a running dbt Fusion language server.

Start the server in the project first:

    dbt lsp --socket <PORT> --static-analysis strict

This module speaks LSP JSON-RPC over a TCP socket: it performs the
`initialize`/`initialized` handshake, records the server's capabilities and any
published diagnostics into the graph, and attempts a best-effort column-level
lineage request.

This path is *optional* — the graph is fully built from static artifacts without
it. Anything that fails here is recorded as a warning and never aborts the build.

Note: the column-lineage request method (`CLL_METHOD`) is a Fusion server
extension, not part of base LSP. If your Fusion build names it differently,
adjust `CLL_METHOD`; the base graph is unaffected either way.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any

from graph_model import Graph, COLUMN, DERIVED_FROM

# Extension point: adjust if your Fusion build exposes CLL under another method.
CLL_METHOD = "dbt/columnLineage"


class LspError(Exception):
    pass


class JsonRpcTransport:
    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._id = 0

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.sock.sendall(header + body)

    def request(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        return self._await_response(rid)

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

    def _await_response(self, rid: int, deadline: float = 15.0) -> Any:
        end = time.time() + deadline
        diagnostics: list[dict] = []
        while time.time() < end:
            msg = self._read_message()
            if msg.get("id") == rid:
                if "error" in msg:
                    raise LspError(str(msg["error"]))
                self.last_diagnostics = diagnostics
                return msg.get("result")
            if msg.get("method") == "textDocument/publishDiagnostics":
                diagnostics.append(msg.get("params", {}))
        raise LspError(f"timed out awaiting response to request {rid}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def enrich_from_lsp(
    graph: Graph, port: int, project_dir: str, warn: list[str], host: str = "127.0.0.1"
) -> None:
    try:
        rpc = JsonRpcTransport(host, port)
    except OSError as exc:
        warn.append(f"[lsp] could not connect to {host}:{port}: {exc}")
        return

    rpc.last_diagnostics = []  # type: ignore[attr-defined]
    try:
        root_uri = f"file://{project_dir}"
        caps = rpc.request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {},
            "clientInfo": {"name": "building-unified-semantic-graph"},
        })
        rpc.notify("initialized", {})
        graph.metadata["lsp"] = {
            "connected": True,
            "server": (caps or {}).get("serverInfo"),
        }

        _try_column_lineage(rpc, graph, warn)

        diagnostics = getattr(rpc, "last_diagnostics", [])
        if diagnostics:
            graph.metadata["lsp"]["diagnostics_documents"] = len(diagnostics)

        try:
            rpc.request("shutdown")
            rpc.notify("exit")
        except LspError:
            pass
    except LspError as exc:
        warn.append(f"[lsp] enrichment failed: {exc}")
        graph.metadata.setdefault("lsp", {})["connected"] = True
        graph.metadata["lsp"]["error"] = str(exc)
    finally:
        rpc.close()


def _try_column_lineage(rpc: JsonRpcTransport, graph: Graph, warn: list[str]) -> None:
    try:
        result = rpc.request(CLL_METHOD, {})
    except LspError as exc:
        warn.append(
            f"[lsp] column-lineage request '{CLL_METHOD}' unavailable "
            f"({exc}); base graph is unaffected"
        )
        return
    added = 0
    for edge in _iter_lineage_edges(result):
        src, tgt = edge
        s = graph.node(COLUMN, "dbt", str(src).lower(), str(src))
        t = graph.node(COLUMN, "dbt", str(tgt).lower(), str(tgt))
        graph.add_edge(DERIVED_FROM, t.id, s.id, source="lsp")
        added += 1
    if added:
        graph.metadata.setdefault("lsp", {})["cll_edges"] = added


def _iter_lineage_edges(result: Any):
    """Yield (source, target) pairs from a lineage payload, defensively."""
    if not result:
        return
    rows = result if isinstance(result, list) else result.get("edges", [])
    for row in rows or []:
        if isinstance(row, dict):
            src = row.get("source") or row.get("from") or row.get("upstream")
            tgt = row.get("target") or row.get("to") or row.get("downstream")
            if src and tgt:
                yield src, tgt
