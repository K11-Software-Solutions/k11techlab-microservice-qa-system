# -*- coding: utf-8 -*-
# Copyright 2026 Kavita Jadhav / K11 Software Solutions LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
graph/consumer_finder.py
──────────────────────────
Finds downstream consumers of a changed provider and enriches them with
context needed for Phase 3 (parallel contract compliance checks).

Feature 5: transitive consumers (depth > 1) are now resolved via
shortest-path traversal so their methods and criticality reflect the
actual dependency chain rather than defaulting to GET/low.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Union

from graph.dependency_graph import ConsumptionEdge, DependencyGraph

logger = logging.getLogger(__name__)

TRANSITIVE_VALIDATION_ENABLED = os.getenv("TRANSITIVE_VALIDATION_ENABLED", "true").lower() != "false"
MAX_TRANSITIVE_DEPTH = int(os.getenv("MAX_TRANSITIVE_DEPTH", "3"))


def _normalise_specs(changed_endpoints: list) -> list[dict]:
    """
    Accept either flat strings or structured dicts and return a normalised list.

    Accepted input forms:
      - "GET /api/v2/users/{id}"   → {"pattern": "/api/v2/users/{id}", "methods": {"GET"}}
      - "/api/v2/users/{id}"       → {"pattern": "/api/v2/users/{id}", "methods": set()}
      - {"pattern": "...", "methods": ["GET"]}  → as-is with set conversion
      - {"endpoint": "...", "method": "GET"}    → legacy Phase 1 dict form

    An empty methods set means "match all methods" (backward-compatible behaviour).
    """
    specs = []
    for ep in changed_endpoints:
        if isinstance(ep, str):
            parts = ep.split(" ", 1)
            if len(parts) == 2 and parts[0].isupper() and "/" in parts[1]:
                specs.append({"pattern": parts[1], "methods": {parts[0]}})
            else:
                specs.append({"pattern": ep, "methods": set()})
        else:
            pattern = ep.get("pattern") or ep.get("endpoint", "")
            methods_raw = ep.get("methods") or (
                [ep["method"]] if ep.get("method") else []
            )
            specs.append({
                "pattern": pattern,
                "methods": {m.upper() for m in methods_raw},
            })
    return specs


@dataclass
class ConsumerContext:
    """Enriched consumer record for Phase 3 validation."""
    consumer:           str
    repo:               str
    team:               str
    slack_channel:      str
    consuming_endpoints: list[str]          = field(default_factory=list)
    edge_methods:       list[str]           = field(default_factory=lambda: ["GET"])
    edge_criticality:   str                 = "medium"
    is_direct:          bool                = True
    hop_depth:          int                 = 1   # 1 = direct, 2+ = transitive


_CRIT_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _resolve_consumer_context(
    graph: DependencyGraph,
    consumer: str,
    provider: str,
    specs: list[dict],
    is_direct: bool,
) -> ConsumerContext | None:
    """
    Resolve edge metadata for one consumer.

    Direct consumers: match specs against the single consumer→provider edge
    (method + path filtering, same as Feature 2 logic).

    Transitive consumers: walk the shortest path through the graph,
    union methods and take max criticality across all hops.
    Returns None when the consumer should be skipped (exceeds depth, disabled,
    or no path/match found).
    """
    node_data = graph._g.nodes.get(consumer, {})

    if is_direct:
        consuming: list[str] = []
        all_methods: set[str] = set()
        max_criticality = "low"

        for _, target, edge_data in graph._g.edges(consumer, data=True):
            if target != provider:
                continue
            ep_pattern   = edge_data.get("endpoint_pattern", "")
            edge_methods = {m.upper() for m in edge_data.get("methods", ["GET"])}
            edge_crit    = edge_data.get("criticality", "medium")

            for spec in specs:
                changed_pattern = spec["pattern"]
                changed_methods = spec["methods"]

                if not (ep_pattern and (
                    ep_pattern in changed_pattern or changed_pattern in ep_pattern
                )):
                    continue
                if changed_methods and not edge_methods.intersection(changed_methods):
                    continue

                consuming.append(changed_pattern)
                all_methods.update(edge_methods)

            if _CRIT_RANK.get(edge_crit, 0) > _CRIT_RANK.get(max_criticality, 0):
                max_criticality = edge_crit

        if not consuming:
            logger.debug(
                "Consumer %s skipped — no method+path overlap with changed endpoints",
                consumer,
            )
            return None

        return ConsumerContext(
            consumer=consumer,
            repo=node_data.get("repo", ""),
            team=node_data.get("team", ""),
            slack_channel=node_data.get("slack_channel", ""),
            consuming_endpoints=consuming,
            edge_methods=sorted(all_methods) if all_methods else ["GET"],
            edge_criticality=max_criticality,
            is_direct=True,
            hop_depth=1,
        )

    else:
        if not TRANSITIVE_VALIDATION_ENABLED:
            return None

        path_edges = graph.shortest_path_edges(consumer, provider)
        hop_depth  = len(path_edges)

        if hop_depth == 0:
            return None

        if hop_depth > MAX_TRANSITIVE_DEPTH:
            logger.debug(
                "Skipping transitive consumer %s (depth %d > max %d)",
                consumer, hop_depth, MAX_TRANSITIVE_DEPTH,
            )
            return None

        all_methods: set[str] = set()
        max_criticality = "low"
        for edge_data in path_edges:
            for m in edge_data.get("methods", ["GET"]):
                all_methods.add(m.upper())
            edge_crit = edge_data.get("criticality", "medium")
            if _CRIT_RANK.get(edge_crit, 0) > _CRIT_RANK.get(max_criticality, 0):
                max_criticality = edge_crit

        consuming_endpoints = [spec["pattern"] for spec in specs] or []

        return ConsumerContext(
            consumer=consumer,
            repo=node_data.get("repo", ""),
            team=node_data.get("team", ""),
            slack_channel=node_data.get("slack_channel", ""),
            consuming_endpoints=consuming_endpoints,
            edge_methods=sorted(all_methods) if all_methods else ["GET"],
            edge_criticality=max_criticality,
            is_direct=False,
            hop_depth=hop_depth,
        )


def find_affected_consumers(
    graph: DependencyGraph,
    provider: str,
    changed_endpoints: list,
) -> list[ConsumerContext]:
    """
    Return all consumers affected by changes to `provider`'s `changed_endpoints`.

    `changed_endpoints` accepts:
      - flat strings: ["GET /api/v2/users/{id}", "/api/v1/orders/{id}"]
      - structured dicts: [{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}]
      - legacy Phase 1 dicts: [{"endpoint": "/api/v2/users/{id}", "method": "GET"}]

    Direct consumers: filtered by method+path overlap (Feature 2).
    Transitive consumers: included via shortest-path traversal up to
    MAX_TRANSITIVE_DEPTH hops (Feature 5).
    """
    direct   = set(graph.direct_consumers(provider))
    all_down = set(graph.downstream_consumers(provider))
    specs    = _normalise_specs(changed_endpoints)

    consumers: list[ConsumerContext] = []
    for consumer in all_down:
        ctx = _resolve_consumer_context(
            graph, consumer, provider, specs,
            is_direct=(consumer in direct),
        )
        if ctx is not None:
            consumers.append(ctx)

    # Sort: direct first, then by hop_depth ascending, then criticality descending
    crit_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    consumers.sort(key=lambda c: (
        0 if c.is_direct else c.hop_depth,
        crit_order.get(c.edge_criticality, 2),
    ))

    logger.info(
        "Found %d affected consumers for provider=%s (%d direct, %d transitive)",
        len(consumers), provider,
        sum(1 for c in consumers if c.is_direct),
        sum(1 for c in consumers if not c.is_direct),
    )
    return consumers


def serialise_consumers(consumers: list[ConsumerContext]) -> list[dict]:
    """Convert ConsumerContext list to pipeline state dicts."""
    return [
        {
            "consumer":             c.consumer,
            "repo":                 c.repo,
            "team":                 c.team,
            "slack_channel":        c.slack_channel,
            "consuming_endpoints":  c.consuming_endpoints,
            "edge_methods":         c.edge_methods,
            "edge_criticality":     c.edge_criticality,
            "is_direct":            c.is_direct,
            "hop_depth":            c.hop_depth,
        }
        for c in consumers
    ]
