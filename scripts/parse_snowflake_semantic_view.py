"""Parse Snowflake `CREATE SEMANTIC VIEW` DDL into the unified graph.

Snowflake Semantic Views are SQL DDL (not JSON/YAML), so this is a tolerant,
depth-aware parser for the observed grammar:

    CREATE [OR REPLACE] SEMANTIC VIEW <fqname>
      [COMMENT = '...']
      TABLES (
        <FQ_TABLE> AS <alias> (
          PRIMARY KEY (...) [UNIQUE KEY (...)]
          DIMENSIONS ( name [LABEL='..'] [COMMENT='..'], ... )
          FACTS      ( name [LABEL='..'] [COMMENT='..'], ... )
          MEASURES   ( name [LABEL='..'] [COMMENT='..'] AS <expr>, ... )
        ), ...
      )
      RELATIONSHIPS ( a (col) MANY TO ONE b (col), ... );

Unparseable clauses are recorded as warnings, never fatal. Distinctive traits vs
dbt: multi-table within one view, explicit FACT/MEASURE split, and explicit join
cardinality via RELATIONSHIPS.
"""

from __future__ import annotations

import re

from graph_model import (
    Graph, PHYSICAL_TABLE, SEMANTIC_MODEL, ENTITY, DIMENSION, MEASURE, COLUMN,
    JOINS, HAS_DIMENSION, HAS_MEASURE, HAS_ENTITY, BOUND_TO,
)

PLATFORM = "snowflake"


def strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _paren_block(text: str, start: int) -> tuple[str, int]:
    """Given index `start` at an opening '(', return (inner_text, index_after_close)."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    raise ValueError("unbalanced parentheses")


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` only at paren depth 0 and outside single quotes."""
    parts, buf, depth, in_str = [], [], 0, False
    for c in text:
        if c == "'":
            in_str = not in_str
        if not in_str:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == sep and depth == 0:
                parts.append("".join(buf))
                buf = []
                continue
        buf.append(c)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _find_section(body: str, keyword: str) -> str | None:
    """Return the inner text of `KEYWORD ( ... )` within a table body, else None."""
    m = re.search(rf"\b{keyword}\b\s*\(", body, re.IGNORECASE)
    if not m:
        return None
    try:
        inner, _ = _paren_block(body, m.end() - 1)
        return inner
    except ValueError:
        return None


def _extract(item: str, keyword: str) -> str | None:
    m = re.search(rf"\b{keyword}\b\s*=\s*'([^']*)'", item, re.IGNORECASE)
    return m.group(1) if m else None


def _column_item(item: str) -> dict:
    """Parse a DIMENSIONS/FACTS entry: leading name plus optional LABEL/COMMENT."""
    name = re.split(r"\s", item.strip(), 1)[0]
    return {
        "name": name,
        "label": _extract(item, "LABEL"),
        "comment": _extract(item, "COMMENT"),
    }


def _measure_item(item: str) -> dict:
    """Parse a MEASURES entry: `name [LABEL=..] [COMMENT=..] AS <expr>`."""
    # split on top-level ' AS '
    m = re.search(r"\bAS\b", item, re.IGNORECASE)
    left, expr = item, None
    if m:
        # find the AS that is at depth 0
        depth, idx = 0, None
        for mm in re.finditer(r"\bAS\b", item, re.IGNORECASE):
            pre = item[: mm.start()]
            depth = pre.count("(") - pre.count(")")
            if depth == 0:
                idx = mm
                break
        if idx:
            left = item[: idx.start()]
            expr = item[idx.end():].strip()
    d = _column_item(left)
    d["expr"] = expr.strip() if expr else None
    return d


def parse_semantic_view_sql(sql_text: str, graph: Graph, warn: list[str], origin: str = "") -> None:
    sql = strip_sql_comments(sql_text)
    for m in re.finditer(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?SEMANTIC\s+VIEW\s+([A-Za-z0-9_.\"]+)",
        sql, re.IGNORECASE,
    ):
        try:
            _parse_one_view(sql, m, graph, warn, origin)
        except Exception as exc:  # noqa: BLE001
            warn.append(f"[snowflake] failed to parse semantic view in {origin}: {exc}")


def _parse_one_view(sql: str, m: re.Match, graph: Graph, warn: list[str], origin: str) -> None:
    view_name = m.group(1).replace('"', "").lower()
    tail = sql[m.end():]

    view_comment = _extract(tail.split("TABLES", 1)[0], "COMMENT")
    view = graph.node(
        SEMANTIC_MODEL, PLATFORM, view_name, view_name,
        description=view_comment, origin=origin, kind="semantic_view",
    )

    tm = re.search(r"\bTABLES\b\s*\(", tail, re.IGNORECASE)
    if not tm:
        warn.append(f"[snowflake] no TABLES clause in view {view_name}")
        return
    tables_inner, after = _paren_block(tail, tm.end() - 1)

    alias_to_table: dict[str, str] = {}
    for entry in _split_top_level(tables_inner):
        _parse_table_entry(entry, view, graph, alias_to_table, warn)

    # RELATIONSHIPS -> JOINS edges
    rel_search = tail[after:]
    rm = re.search(r"\bRELATIONSHIPS\b\s*\(", rel_search, re.IGNORECASE)
    if rm:
        rel_inner, _ = _paren_block(rel_search, rm.end() - 1)
        _parse_relationships(rel_inner, alias_to_table, graph, warn)


def _parse_table_entry(entry: str, view, graph: Graph, alias_to_table: dict, warn: list[str]) -> None:
    # "<FQ_TABLE> AS <alias> ( body )"
    head_m = re.match(
        r"([A-Za-z0-9_.\"]+)\s+AS\s+([A-Za-z0-9_]+)\s*\(", entry, re.IGNORECASE
    )
    if not head_m:
        warn.append(f"[snowflake] could not parse table entry: {entry[:60]}...")
        return
    fq_table = head_m.group(1).replace('"', "").lower()
    alias = head_m.group(2).lower()
    body, _ = _paren_block(entry, head_m.end() - 1)

    table = graph.node(
        PHYSICAL_TABLE, PLATFORM, fq_table, fq_table, relation_name=fq_table, alias=alias
    )
    graph.add_edge(BOUND_TO, view.id, table.id)
    alias_to_table[alias] = table.id

    # primary key -> entity
    pk = _find_section(body, "PRIMARY KEY")
    if pk:
        key = pk.strip().strip("()").split(",")[0].strip()
        e = graph.node(
            ENTITY, PLATFORM, f"{alias}.{key}", key,
            entity_type="primary", expr=key,
        )
        graph.add_edge(HAS_ENTITY, view.id, e.id)

    scope = f"{view.name}.{alias}"

    dims = _find_section(body, "DIMENSIONS")
    if dims:
        for item in _split_top_level(dims):
            d = _column_item(item)
            node = graph.node(
                DIMENSION, PLATFORM, f"{scope}.{d['name']}", d["name"],
                display_name=d["label"], description=d["comment"],
                source_table=fq_table,
            )
            graph.add_edge(HAS_DIMENSION, view.id, node.id)

    facts = _find_section(body, "FACTS")
    if facts:
        for item in _split_top_level(facts):
            d = _column_item(item)
            graph.node(
                COLUMN, PLATFORM, f"{fq_table}.{d['name'].lower()}", d["name"],
                table=fq_table, is_fact=True,
                display_name=d["label"], description=d["comment"],
            )

    measures = _find_section(body, "MEASURES")
    if measures:
        for item in _split_top_level(measures):
            d = _measure_item(item)
            node = graph.node(
                MEASURE, PLATFORM, f"{scope}.{d['name']}", d["name"],
                canonical_expr=d["expr"], expr=d["expr"],
                display_name=d["label"], description=d["comment"],
                source_table=fq_table, has_filter=False,
            )
            graph.add_edge(HAS_MEASURE, view.id, node.id)


def _parse_relationships(rel_inner: str, alias_to_table: dict, graph: Graph, warn: list[str]) -> None:
    for rel in _split_top_level(rel_inner):
        rm = re.match(
            r"([A-Za-z0-9_]+)\s*\(([^)]*)\)\s+MANY\s+TO\s+ONE\s+"
            r"([A-Za-z0-9_]+)\s*\(([^)]*)\)",
            rel.strip(), re.IGNORECASE,
        )
        if not rm:
            warn.append(f"[snowflake] could not parse relationship: {rel[:60]}")
            continue
        left_alias, left_col, right_alias, right_col = (
            rm.group(1).lower(), rm.group(2).strip(),
            rm.group(3).lower(), rm.group(4).strip(),
        )
        left = alias_to_table.get(left_alias)
        right = alias_to_table.get(right_alias)
        if left and right:
            graph.add_edge(
                JOINS, left, right,
                cardinality="many_to_one",
                left_column=left_col, right_column=right_col,
                inferred=False,
            )
