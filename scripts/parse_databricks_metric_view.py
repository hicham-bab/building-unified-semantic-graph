"""Parse Databricks Metric View YAML (spec version 1.1) into the unified graph.

Databricks Metric Views are single-source YAML (no joins/entities). Their
distinctive payload is rich NL/presentation metadata: `display_name`, `format`,
and `synonyms` — normalized here onto dimension/measure nodes. Ratios like
`return_rate` are written inline in `expr` (no metric composition graph).
"""

from __future__ import annotations

import os

import yaml

from graph_model import (
    Graph, PHYSICAL_TABLE, SEMANTIC_MODEL, DIMENSION, MEASURE,
    HAS_DIMENSION, HAS_MEASURE, BOUND_TO,
)

PLATFORM = "databricks"


def parse_metric_view_yaml(text: str, graph: Graph, warn: list[str], origin: str = "") -> None:
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError as exc:
        warn.append(f"[databricks] YAML parse error in {origin}: {exc}")
        return

    view_stem = os.path.splitext(os.path.basename(origin))[0] if origin else "metric_view"
    for i, doc in enumerate(docs):
        if "measures" not in doc and "dimensions" not in doc:
            continue
        view_name = view_stem if len(docs) == 1 else f"{view_stem}_{i}"
        _parse_doc(doc, view_name, graph, origin)


def _parse_doc(doc: dict, view_name: str, graph: Graph, origin: str) -> None:
    source = (doc.get("source") or "").lower()
    view = graph.node(
        SEMANTIC_MODEL, PLATFORM, view_name, view_name,
        description=doc.get("comment"),
        spec_version=doc.get("version"),
        source=source, origin=origin, kind="metric_view",
    )
    if source:
        table = graph.node(
            PHYSICAL_TABLE, PLATFORM, source, source, relation_name=source
        )
        graph.add_edge(BOUND_TO, view.id, table.id)

    for dim in doc.get("dimensions", []) or []:
        name = dim.get("name")
        node = graph.node(
            DIMENSION, PLATFORM, f"{view_name}.{name}", name,
            expr=dim.get("expr"),
            display_name=dim.get("display_name"),
            description=dim.get("comment"),
            synonyms=dim.get("synonyms") or [],
            format=dim.get("format"),
        )
        graph.add_edge(HAS_DIMENSION, view.id, node.id)

    for mea in doc.get("measures", []) or []:
        name = mea.get("name")
        expr = mea.get("expr")
        node = graph.node(
            MEASURE, PLATFORM, f"{view_name}.{name}", name,
            expr=expr, canonical_expr=expr,
            display_name=mea.get("display_name"),
            description=mea.get("comment"),
            synonyms=mea.get("synonyms") or [],
            format=mea.get("format"),
            has_filter=False,
        )
        graph.add_edge(HAS_MEASURE, view.id, node.id)
