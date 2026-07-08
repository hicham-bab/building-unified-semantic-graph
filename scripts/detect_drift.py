"""Cross-platform consistency / drift detection over the unified graph.

Matches the *same* semantic concept across platforms (dbt vs Snowflake vs
Databricks) and emits:
  - EQUIVALENT_TO edges for matched concepts, and
  - DRIFT edges (with a `subtype`) for inconsistencies.

Matching is by `normalized_name`, optionally re-keyed through a crosswalk file
for genuine renames. Drift subtypes:
  - missing_on_platform : concept defined on some in-play platforms but not others
  - expression_mismatch : matched concept computed with different SQL
  - filter_mismatch     : dbt uses an explicit metric/measure filter; the matched
                          concept elsewhere inlines it (no separate filter)
  - grain_mismatch      : matched time dimension declared at different granularities
"""

from __future__ import annotations

import json
import os
from itertools import combinations
from typing import Any

from graph_model import (
    Graph, Node, METRIC, MEASURE, DIMENSION, EQUIVALENT_TO, DRIFT, PLATFORMS,
    normalize_name, normalize_expr,
)

COMPARABLE = (METRIC, MEASURE)


def load_crosswalk(path: str | None, warn: list[str]) -> dict[tuple[str, str], str]:
    """Load an optional crosswalk mapping (platform, normalized_name) -> canonical.

    File format (JSON):
      {"concepts": [
         {"canonical": "recognised_revenue",
          "dbt": "total_recognised_revenue",
          "snowflake": "total_net_revenue",
          "databricks": "total_revenue"}
      ]}
    """
    if not path:
        return {}
    if not os.path.exists(path):
        warn.append(f"[drift] crosswalk file not found: {path}")
        return {}
    with open(path) as f:
        data = json.load(f)
    alias: dict[tuple[str, str], str] = {}
    for concept in data.get("concepts", []):
        canonical = normalize_name(concept.get("canonical"))
        for platform in PLATFORMS:
            names = concept.get(platform)
            if not names:
                continue
            if isinstance(names, str):
                names = [names]
            for n in names:
                alias[(platform, normalize_name(n))] = canonical
    return alias


def _concept_key(node: Node, alias: dict[tuple[str, str], str]) -> str:
    return alias.get((node.platform, node.normalized_name), node.normalized_name)


def _rep_sort(node: Node) -> tuple[int, str]:
    # prefer Metric over Measure as the platform representative
    return (0 if node.type == METRIC else 1, node.id)


def detect_drift(
    graph: Graph, alias: dict[tuple[str, str], str], warn: list[str]
) -> dict[str, Any]:
    in_play = set(graph.platforms_present())
    findings: dict[str, list[dict]] = {
        "missing_on_platform": [],
        "expression_mismatch": [],
        "filter_mismatch": [],
        "grain_mismatch": [],
    }
    matched = 0

    # ---- measures / metrics ----
    concepts: dict[str, dict[str, list[Node]]] = {}
    for node in graph.by_type(*COMPARABLE):
        key = _concept_key(node, alias)
        concepts.setdefault(key, {}).setdefault(node.platform, []).append(node)

    # denormalized, easy-to-consume concept catalog (one row per concept)
    consumable: list[dict] = []

    for key, by_platform in concepts.items():
        reps = {
            p: sorted(nodes, key=_rep_sort)[0] for p, nodes in by_platform.items()
        }
        present = set(reps)
        concept_drift: list[str] = []

        # missing on in-play platforms
        for missing in sorted(in_play - present):
            for existing_p in sorted(present):
                rep = reps[existing_p]
                graph.add_edge(
                    DRIFT, rep.id, rep.id,
                    subtype="missing_on_platform", missing_platform=missing,
                    concept=key,
                )
            findings["missing_on_platform"].append(
                {"concept": key, "defined_on": sorted(present), "missing_on": missing}
            )
            concept_drift.append(f"missing_on:{missing}")

        # pairwise comparisons among present platforms
        for pa, pb in combinations(sorted(present), 2):
            na, nb = reps[pa], reps[pb]
            matched += 1
            graph.add_edge(
                EQUIVALENT_TO, na.id, nb.id,
                concept=key,
                match=("crosswalk" if key != na.normalized_name
                       or key != nb.normalized_name else "name"),
            )
            ea = normalize_expr(na.props.get("canonical_expr"))
            eb = normalize_expr(nb.props.get("canonical_expr"))
            if ea and eb and ea != eb:
                graph.add_edge(
                    DRIFT, na.id, nb.id, subtype="expression_mismatch", concept=key
                )
                findings["expression_mismatch"].append({
                    "concept": key,
                    pa: na.props.get("canonical_expr"),
                    pb: nb.props.get("canonical_expr"),
                })
                concept_drift.append(f"expression_mismatch:{pa}~{pb}")
            # filter mismatch: dbt explicit filter vs inlined elsewhere
            fa, fb = na.props.get("has_filter"), nb.props.get("has_filter")
            if fa != fb:
                dbt_side = na if na.platform == "dbt" else (nb if nb.platform == "dbt" else None)
                if dbt_side and dbt_side.props.get("has_filter"):
                    other = nb if dbt_side is na else na
                    graph.add_edge(
                        DRIFT, dbt_side.id, other.id,
                        subtype="filter_mismatch", concept=key,
                    )
                    findings["filter_mismatch"].append({
                        "concept": key,
                        "dbt_filter": True,
                        f"{other.platform}_inlined": True,
                    })
                    concept_drift.append(f"filter_mismatch:{other.platform}")

        # one denormalized row per concept, easy for an agent/BI to consume
        consumable.append({
            "concept": key,
            "defined_on": sorted(present),
            "consistent": not concept_drift,
            "drift": sorted(set(concept_drift)),
            "definitions": {
                p: {
                    "node_id": reps[p].id,
                    "type": reps[p].type,
                    "name": reps[p].name,
                    "expression": reps[p].props.get("canonical_expr"),
                    "display_name": reps[p].props.get("display_name"),
                    "synonyms": reps[p].props.get("synonyms") or [],
                }
                for p in sorted(present)
            },
        })

    # ---- dimensions: grain ----
    dim_concepts: dict[str, dict[str, Node]] = {}
    for node in graph.by_type(DIMENSION):
        key = _concept_key(node, alias)
        dim_concepts.setdefault(key, {}).setdefault(node.platform, node)
    for key, by_platform in dim_concepts.items():
        for pa, pb in combinations(sorted(by_platform), 2):
            ga = by_platform[pa].props.get("time_granularity")
            gb = by_platform[pb].props.get("time_granularity")
            if ga and gb and ga != gb:
                graph.add_edge(
                    DRIFT, by_platform[pa].id, by_platform[pb].id,
                    subtype="grain_mismatch", concept=key,
                )
                findings["grain_mismatch"].append(
                    {"concept": key, pa: ga, pb: gb}
                )

    return {
        "in_play_platforms": sorted(in_play),
        "concepts_compared": len(concepts),
        "equivalences": matched,
        "findings": findings,
        "concepts": sorted(consumable, key=lambda c: (c["consistent"], c["concept"])),
    }


def render_report(summary: dict[str, Any]) -> str:
    f = summary["findings"]
    lines = ["# Cross-Platform Semantic Drift Report", ""]
    lines.append(f"Platforms in play: {', '.join(summary['in_play_platforms']) or 'none'}")
    lines.append(f"Concepts compared: {summary['concepts_compared']}  ·  "
                 f"Equivalences: {summary['equivalences']}")
    total = sum(len(v) for v in f.values())
    lines.append(f"Total drift findings: {total}")
    lines.append("")

    if not total:
        lines.append("No drift detected across the matched concepts.")
        return "\n".join(lines) + "\n"

    if f["missing_on_platform"]:
        lines += ["## Missing on platform", "",
                  "| Concept | Defined on | Missing on |", "|---|---|---|"]
        for item in f["missing_on_platform"]:
            lines.append(
                f"| `{item['concept']}` | {', '.join(item['defined_on'])} | "
                f"**{item['missing_on']}** |"
            )
        lines.append("")

    if f["expression_mismatch"]:
        lines += ["## Expression mismatch", "",
                  "Same concept, different aggregation SQL:", ""]
        for item in f["expression_mismatch"]:
            lines.append(f"- `{item['concept']}`")
            for k, v in item.items():
                if k != "concept":
                    lines.append(f"    - {k}: `{v}`")
        lines.append("")

    if f["filter_mismatch"]:
        lines += ["## Filter mismatch", "",
                  "dbt applies an explicit metric/measure filter; the matched "
                  "concept elsewhere inlines it (e.g. `CASE WHEN`):", ""]
        for item in f["filter_mismatch"]:
            lines.append(f"- `{item['concept']}`: {json.dumps(item)}")
        lines.append("")

    if f["grain_mismatch"]:
        lines += ["## Grain mismatch", "",
                  "| Concept | Grains |", "|---|---|"]
        for item in f["grain_mismatch"]:
            grains = ", ".join(f"{k}={v}" for k, v in item.items() if k != "concept")
            lines.append(f"| `{item['concept']}` | {grains} |")
        lines.append("")

    return "\n".join(lines) + "\n"
