"""Parse Open Semantic Interchange (OSI) core-spec files into the unified graph.

Spec: https://github.com/open-semantic-interchange/OSI/blob/main/core-spec/spec.md

OSI is a vendor-neutral interchange format (YAML or JSON). Its shape maps very
cleanly onto the unified graph:

    semantic_model:
      - name: ...
        datasets:                       # table bindings
          - name: ...
            source: <physical table>
            primary_key: [col]
            fields:                      # dimensions / row-level attributes
              - name: ...
                expression: {dialects: [{dialect, expression}]}
                dimension: {is_time: bool}
        relationships:                   # joins (from = many side, to = one side)
          - {name, from, to, from_columns, to_columns}
        metrics:                         # measures (aggregate expressions)
          - name: ...
            expression: {dialects: [{dialect, expression}]}

Expressions are multi-dialect; we prefer ANSI_SQL, else the first dialect given.
"""

from __future__ import annotations

import json

import yaml

from graph_model import (
    Graph, PHYSICAL_TABLE, SEMANTIC_MODEL, ENTITY, DIMENSION, METRIC,
    JOINS, HAS_DIMENSION, HAS_MEASURE, HAS_ENTITY, BOUND_TO,
)

PLATFORM = "osi"
_DIALECT_PREFERENCE = ("ANSI_SQL", "SNOWFLAKE", "DATABRICKS", "TABLEAU", "MDX", "MAQL")


def _load(text: str):
    text = text.lstrip()
    if text.startswith("{"):
        return json.loads(text)
    return yaml.safe_load(text)


def _expression(obj: dict | None) -> str | None:
    """Pull a scalar SQL expression out of an OSI `expression.dialects` block."""
    if not obj:
        return None
    dialects = obj.get("dialects") or []
    by_name = {d.get("dialect"): d.get("expression") for d in dialects if isinstance(d, dict)}
    for pref in _DIALECT_PREFERENCE:
        if by_name.get(pref):
            return by_name[pref]
    return next((v for v in by_name.values() if v), None)


def parse_osi(text: str, graph: Graph, warn: list[str], origin: str = "") -> None:
    try:
        doc = _load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        warn.append(f"[osi] parse error in {origin}: {exc}")
        return
    if not isinstance(doc, dict):
        warn.append(f"[osi] unexpected top-level structure in {origin}")
        return

    models = doc.get("semantic_model") or doc.get("semantic_models") or []
    if isinstance(models, dict):
        models = [models]
    for model in models:
        _parse_model(model, graph, warn, origin)


def _parse_model(model: dict, graph: Graph, warn: list[str], origin: str) -> None:
    model_name = model.get("name", "osi_model")
    sm = graph.node(
        SEMANTIC_MODEL, PLATFORM, model_name, model_name,
        description=model.get("description"), origin=origin, kind="osi_semantic_model",
    )

    # datasets -> physical tables + fields (dimensions); track pk entities
    dataset_source: dict[str, str] = {}
    for ds in model.get("datasets", []) or []:
        ds_name = ds.get("name")
        source = (ds.get("source") or "").lower()
        dataset_source[ds_name] = source
        if source:
            table = graph.node(
                PHYSICAL_TABLE, PLATFORM, source, source, relation_name=source
            )
            graph.add_edge(BOUND_TO, sm.id, table.id)

        pk = ds.get("primary_key")
        if pk:
            pk_col = pk[0] if isinstance(pk, list) else pk
            e = graph.node(
                ENTITY, PLATFORM, f"{ds_name}.{pk_col}", str(pk_col),
                entity_type="primary", expr=str(pk_col),
            )
            graph.add_edge(HAS_ENTITY, sm.id, e.id)

        for field in ds.get("fields", []) or []:
            fname = field.get("name")
            dim = field.get("dimension") or {}
            node = graph.node(
                DIMENSION, PLATFORM, f"{ds_name}.{fname}", fname,
                dimension_type=("time" if dim.get("is_time") else "categorical"),
                expr=_expression(field.get("expression")),
                description=field.get("description"),
                source_table=source,
            )
            graph.add_edge(HAS_DIMENSION, sm.id, node.id)

    # relationships -> joins (from = many side, to = one side)
    for rel in model.get("relationships", []) or []:
        graph.add_edge(
            JOINS,
            f"{PLATFORM}:{PHYSICAL_TABLE}:{dataset_source.get(rel.get('from'), '')}",
            f"{PLATFORM}:{PHYSICAL_TABLE}:{dataset_source.get(rel.get('to'), '')}",
            cardinality="many_to_one", inferred=False,
            name=rel.get("name"),
            left_columns=rel.get("from_columns"),
            right_columns=rel.get("to_columns"),
        )

    # metrics -> measures (OSI metrics are aggregate expressions)
    for metric in model.get("metrics", []) or []:
        mname = metric.get("name")
        expr = _expression(metric.get("expression"))
        node = graph.node(
            METRIC, PLATFORM, mname, mname,
            metric_type="expression", canonical_expr=expr, expr=expr,
            description=metric.get("description"), has_filter=False,
        )
        graph.add_edge(HAS_MEASURE, sm.id, node.id)
