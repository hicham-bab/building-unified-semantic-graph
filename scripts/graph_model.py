"""Unified semantic knowledge-graph model.

A tiny, dependency-free property graph: a bag of typed nodes and typed edges that
each carry a `platform` provenance tag and arbitrary `props`. Every semantic-layer
parser (dbt / Snowflake / Databricks) emits into one of these graphs; the graphs are
then merged and serialized to a single JSON file.

Node ids are stable and human-readable: "<platform>:<type>:<qualifier>".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# ---- Vocabulary -------------------------------------------------------------

# Node types
PHYSICAL_TABLE = "PhysicalTable"
COLUMN = "Column"
SEMANTIC_MODEL = "SemanticModel"
ENTITY = "Entity"
DIMENSION = "Dimension"
MEASURE = "Measure"
METRIC = "Metric"
SAVED_QUERY = "SavedQuery"

# Edge types
DEPENDS_ON = "DEPENDS_ON"        # model -> upstream model / source
DERIVED_FROM = "DERIVED_FROM"    # column -> upstream column (column-level lineage)
JOINS = "JOINS"                  # semantic model -> semantic model (with cardinality)
HAS_COLUMN = "HAS_COLUMN"        # physical table -> column
HAS_DIMENSION = "HAS_DIMENSION"  # semantic model -> dimension
HAS_MEASURE = "HAS_MEASURE"      # semantic model -> measure
HAS_ENTITY = "HAS_ENTITY"        # semantic model -> entity
BOUND_TO = "BOUND_TO"            # semantic model -> physical table
MEASURE_OF = "MEASURE_OF"        # metric -> input measure
COMPOSED_OF = "COMPOSED_OF"      # metric -> input metric (ratio/derived)
GROUPED_BY = "GROUPED_BY"        # saved query -> metric / dimension
EQUIVALENT_TO = "EQUIVALENT_TO"  # cross-platform concept match
DRIFT = "DRIFT"                  # cross-platform inconsistency (props.subtype)

PLATFORMS = ("dbt", "snowflake", "databricks", "osi", "lookml", "powerbi")


# ---- Normalization ----------------------------------------------------------

def normalize_name(name: str | None) -> str:
    """Lowercase, collapse non-alphanumerics to single underscores, strip edges.

    This is the key used to match the *same* concept across platforms when the
    names already agree (e.g. dbt `total_gross_revenue` == Snowflake
    `total_gross_revenue`). Genuine renames (dbt `total_recognised_revenue` vs
    Databricks `total_revenue`) are reconciled via an explicit crosswalk file,
    not here.
    """
    if not name:
        return ""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower())
    return s.strip("_")


def normalize_expr(expr: str | None) -> str:
    """Canonicalize a SQL expression for comparison: lowercase, single-spaced."""
    if not expr:
        return ""
    return re.sub(r"\s+", " ", str(expr).strip().lower())


# ---- Nodes & edges ----------------------------------------------------------

@dataclass
class Node:
    id: str
    type: str
    name: str
    platform: str
    normalized_name: str = ""
    props: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.normalized_name:
            self.normalized_name = normalize_name(self.name)


@dataclass
class Edge:
    type: str
    source: str
    target: str
    props: dict[str, Any] = field(default_factory=dict)


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.metadata: dict[str, Any] = {}

    # -- construction --
    def add_node(self, node: Node) -> Node:
        """Insert or shallow-merge a node. Later props win, existing keys kept."""
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        merged = {**existing.props, **node.props}
        existing.props = merged
        return existing

    def node(
        self,
        type: str,
        platform: str,
        qualifier: str,
        name: str,
        **props: Any,
    ) -> Node:
        node_id = f"{platform}:{type}:{qualifier}"
        return self.add_node(
            Node(id=node_id, type=type, name=name, platform=platform, props=props)
        )

    def add_edge(self, type: str, source: str, target: str, **props: Any) -> Edge:
        edge = Edge(type=type, source=source, target=target, props=props)
        self.edges.append(edge)
        return edge

    # -- queries --
    def by_type(self, *types: str) -> list[Node]:
        wanted = set(types)
        return [n for n in self.nodes.values() if n.type in wanted]

    def platforms_present(self) -> list[str]:
        seen = {n.platform for n in self.nodes.values() if n.platform in PLATFORMS}
        return [p for p in PLATFORMS if p in seen]

    def merge(self, other: "Graph") -> None:
        for n in other.nodes.values():
            self.add_node(n)
        self.edges.extend(other.edges)

    # -- serialization --
    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                **self.metadata,
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "platforms": self.platforms_present(),
            },
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges],
        }

    def to_json(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


def dedupe_edges(graph: Graph) -> None:
    """Drop exact-duplicate edges (same type/source/target/props)."""
    seen: set[str] = set()
    unique: list[Edge] = []
    for e in graph.edges:
        key = json.dumps(
            [e.type, e.source, e.target, e.props], sort_keys=True, default=str
        )
        if key not in seen:
            seen.add(key)
            unique.append(e)
    graph.edges = unique
