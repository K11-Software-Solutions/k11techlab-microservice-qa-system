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
graph/dependency_graph.py
──────────────────────────
Directed dependency graph over microservices.
Edge A → B means "service A consumes an API provided by service B".
When B changes a contract, A is a downstream consumer that may break.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Iterator

try:
    import networkx as nx
except ImportError:
    nx = None   # graceful degradation for environments without networkx

logger = logging.getLogger(__name__)


@dataclass
class ServiceNode:
    name:       str
    repo:       str
    team:       str = ""
    slack_channel: str = ""


@dataclass
class ConsumptionEdge:
    consumer:         str    # service that calls the API
    provider:         str    # service that owns the API
    endpoint_pattern: str    # e.g. "/api/v2/users/*"
    methods:          list[str] = field(default_factory=list)
    criticality:      str = "medium"   # low | medium | high | critical


class DependencyGraph:
    """
    Directed graph: consumer → provider (arrows point TO the dependency).
    "Who calls whom" — traversal finds all services that will break if
    a provider changes.
    """

    def __init__(self) -> None:
        if nx is None:
            raise ImportError("networkx is required: pip install networkx")
        self._g: nx.DiGraph = nx.DiGraph()

    def add_service(self, node: ServiceNode) -> None:
        self._g.add_node(node.name, **{
            "repo": node.repo, "team": node.team,
            "slack_channel": node.slack_channel,
        })

    def add_consumption(self, edge: ConsumptionEdge) -> None:
        """Record that `consumer` depends on `provider`."""
        self._g.add_edge(
            edge.consumer, edge.provider,
            endpoint_pattern=edge.endpoint_pattern,
            methods=edge.methods,
            criticality=edge.criticality,
        )

    def downstream_consumers(self, provider: str) -> list[str]:
        """
        Return all services that directly or indirectly consume `provider`.
        Uses reverse traversal: find all nodes with a path TO `provider`.
        """
        if provider not in self._g:
            return []
        rev = self._g.reverse(copy=False)
        return [
            n for n in nx.descendants(rev, provider)
            if n != provider
        ]

    def direct_consumers(self, provider: str) -> list[str]:
        """Return only direct (one-hop) consumers of `provider`."""
        rev = self._g.reverse(copy=False)
        return list(rev.successors(provider))

    def impact_radius(self, provider: str) -> int:
        """Count of all affected downstream consumers."""
        return len(self.downstream_consumers(provider))

    def impact_score(self, provider: str, changed_endpoints: list[str]) -> float:
        """
        Weighted impact score [0.0, 1.0] considering:
        - Number of downstream consumers
        - Criticality of consuming edges that touch changed endpoints
        """
        consumers = self.downstream_consumers(provider)
        if not consumers:
            return 0.0

        criticality_weights = {"critical": 1.0, "high": 0.75, "medium": 0.5, "low": 0.25}
        total_weight = 0.0
        for consumer in consumers:
            edges = self._g.edges(consumer, data=True)
            for _, target, data in edges:
                if target == provider:
                    ep = data.get("endpoint_pattern", "")
                    if any(ep in changed or changed in ep
                           for changed in changed_endpoints):
                        total_weight += criticality_weights.get(
                            data.get("criticality", "medium"), 0.5)

        # Normalise: max realistic impact = 10 critical consumers
        return min(1.0, total_weight / 10.0)

    def to_dict(self) -> dict:
        return nx.node_link_data(self._g)

    @classmethod
    def from_dict(cls, data: dict) -> "DependencyGraph":
        g = cls()
        g._g = nx.node_link_graph(data)
        return g
