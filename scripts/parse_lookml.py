"""Parse LookML (`.view.lkml` / `.explore.lkml` / `.lkml`) into the unified graph.

LookML is Looker's modeling DSL, not JSON/YAML. This is a tolerant recursive
parser for the subset we need:

    view: orders {
      sql_table_name: analytics.orders ;;
      dimension: order_id { primary_key: yes  type: number  sql: ${TABLE}.order_id ;; }
      dimension_group: created { type: time  timeframes: [date, week, month]
                                 sql: ${TABLE}.created_at ;; }
      measure: total_revenue { type: sum  sql: ${TABLE}.amount ;; }
      measure: order_count   { type: count }
    }
    explore: orders {
      join: users { sql_on: ${orders.user_id} = ${users.id} ;;  relationship: many_to_one }
    }

Keys whose name starts with `sql` (or `html`) take a `;;`-terminated raw value;
everything else is a line/token scalar, a `[list]`, or a `{ block }`.
"""

from __future__ import annotations

import re

from graph_model import (
    Graph, PHYSICAL_TABLE, SEMANTIC_MODEL, ENTITY, DIMENSION, MEASURE,
    JOINS, HAS_DIMENSION, HAS_MEASURE, HAS_ENTITY, BOUND_TO,
)

PLATFORM = "lookml"
_AGG_TYPES = {"sum", "average", "avg", "count", "count_distinct", "min", "max",
              "median", "percentile"}
_IDENT = re.compile(r"[A-Za-z0-9_.$]")


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0
        self.n = len(text)

    # -- low level --
    def _skip_ws(self) -> None:
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "#":  # line comment
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def _read_key(self) -> str | None:
        self._skip_ws()
        start = self.i
        while self.i < self.n and _IDENT.match(self.s[self.i]):
            self.i += 1
        if self.i == start:
            return None
        return self.s[start:self.i]

    def _read_string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        start = self.i
        while self.i < self.n and self.s[self.i] != quote:
            self.i += 1
        val = self.s[start:self.i]
        self.i += 1  # closing quote
        return val

    def _read_token(self) -> str:
        self._skip_ws()
        if self.i < self.n and self.s[self.i] in "\"'":
            return self._read_string()
        start = self.i
        while self.i < self.n and _IDENT.match(self.s[self.i]):
            self.i += 1
        return self.s[start:self.i]

    def _read_sql(self) -> str:
        # raw value terminated by ';;'
        end = self.s.find(";;", self.i)
        if end == -1:
            end = self.n
        val = self.s[self.i:end].strip()
        self.i = end + 2
        return val

    def _read_list(self) -> list[str]:
        assert self.s[self.i] == "["
        depth = 0
        start = self.i
        while self.i < self.n:
            c = self.s[self.i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    break
            self.i += 1
        inner = self.s[start + 1:self.i - 1]
        return [p.strip().strip("\"'") for p in inner.split(",") if p.strip()]

    # -- structure --
    def parse_block(self) -> dict:
        """Parse a `{ ... }` block into {scalars, lists, '__blocks__': [(kind,name,body)]}."""
        self._skip_ws()
        assert self.s[self.i] == "{"
        self.i += 1
        body: dict = {"__blocks__": []}
        while True:
            self._skip_ws()
            if self.i >= self.n:
                break
            if self.s[self.i] == "}":
                self.i += 1
                break
            key = self._read_key()
            if key is None:
                self.i += 1
                continue
            self._skip_ws()
            if self.i < self.n and self.s[self.i] == ":":
                self.i += 1
            self._skip_ws()
            self._parse_value(key, body)
        return body

    def _parse_value(self, key: str, body: dict) -> None:
        self._skip_ws()
        if key.startswith("sql") or key == "html":
            body[key] = self._read_sql()
            return
        if self.i < self.n and self.s[self.i] == "{":
            body[key] = self.parse_block()
            return
        if self.i < self.n and self.s[self.i] == "[":
            body[key] = self._read_list()
            return
        tok = self._read_token()
        self._skip_ws()
        if self.i < self.n and self.s[self.i] == "{":
            block = self.parse_block()
            body["__blocks__"].append((key, tok, block))
        else:
            body[key] = tok

    def parse_top(self) -> list[tuple[str, str, dict]]:
        decls: list[tuple[str, str, dict]] = []
        while True:
            key = self._read_key()
            if key is None:
                break
            self._skip_ws()
            if self.i < self.n and self.s[self.i] == ":":
                self.i += 1
            name = self._read_token()
            self._skip_ws()
            if self.i < self.n and self.s[self.i] == "{":
                decls.append((key, name, self.parse_block()))
            else:
                self.i += 1  # skip stray
        return decls


def parse_lookml(text: str, graph: Graph, warn: list[str], origin: str = "") -> None:
    try:
        decls = _Parser(text).parse_top()
    except Exception as exc:  # noqa: BLE001
        warn.append(f"[lookml] parse error in {origin}: {exc}")
        return
    for kind, name, body in decls:
        if kind == "view":
            _parse_view(name, body, graph, origin)
        elif kind == "explore":
            _parse_explore(name, body, graph, warn)


def _parse_view(name: str, body: dict, graph: Graph, origin: str) -> None:
    sm = graph.node(
        SEMANTIC_MODEL, PLATFORM, name, name,
        origin=origin, kind="lookml_view",
        label=body.get("label"),
    )
    source = (body.get("sql_table_name") or "").strip().rstrip(";").strip().lower()
    if source:
        table = graph.node(
            PHYSICAL_TABLE, PLATFORM, source, source, relation_name=source
        )
        graph.add_edge(BOUND_TO, sm.id, table.id)

    for kind, item_name, item in body.get("__blocks__", []):
        if kind == "dimension":
            node = graph.node(
                DIMENSION, PLATFORM, f"{name}.{item_name}", item_name,
                dimension_type=item.get("type"),
                expr=item.get("sql"), display_name=item.get("label"),
                description=item.get("description"),
            )
            graph.add_edge(HAS_DIMENSION, sm.id, node.id)
            if str(item.get("primary_key", "")).lower() == "yes":
                e = graph.node(
                    ENTITY, PLATFORM, f"{name}.{item_name}", item_name,
                    entity_type="primary", expr=item.get("sql"),
                )
                graph.add_edge(HAS_ENTITY, sm.id, e.id)
        elif kind == "dimension_group":
            node = graph.node(
                DIMENSION, PLATFORM, f"{name}.{item_name}", item_name,
                dimension_type="time",
                time_granularity=_first_timeframe(item.get("timeframes")),
                timeframes=item.get("timeframes"),
                expr=item.get("sql"), display_name=item.get("label"),
            )
            graph.add_edge(HAS_DIMENSION, sm.id, node.id)
        elif kind == "measure":
            node = graph.node(
                MEASURE, PLATFORM, f"{name}.{item_name}", item_name,
                agg=item.get("type"),
                expr=item.get("sql"),
                canonical_expr=_measure_expr(item),
                display_name=item.get("label"),
                description=item.get("description"),
                has_filter=("filters" in item or any(
                    k == "filters" for k, _, _ in item.get("__blocks__", [])
                )),
            )
            graph.add_edge(HAS_MEASURE, sm.id, node.id)


def _parse_explore(name: str, body: dict, graph: Graph, warn: list[str]) -> None:
    base = f"{PLATFORM}:{SEMANTIC_MODEL}:{body.get('from', name)}"
    for kind, join_name, join in body.get("__blocks__", []):
        if kind != "join":
            continue
        graph.add_edge(
            JOINS, base, f"{PLATFORM}:{SEMANTIC_MODEL}:{join_name}",
            cardinality=join.get("relationship", "many_to_one"),
            join_type=join.get("type"),
            sql_on=join.get("sql_on"),
            inferred=False,
        )


def _first_timeframe(timeframes) -> str | None:
    if isinstance(timeframes, list) and timeframes:
        return timeframes[0]
    return None


def _measure_expr(item: dict) -> str | None:
    agg = (item.get("type") or "").lower()
    sql = item.get("sql")
    if agg == "count" and not sql:
        return "count(*)"
    if agg in _AGG_TYPES and sql:
        return f"{agg}({sql})"
    return sql
