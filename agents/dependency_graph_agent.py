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
agents/dependency_graph_agent.py
──────────────────────────────────
Phase 2 agent: Loads the dependency graph, finds all downstream consumers
of the changed service, scores impact, and decides HITL requirement.
"""
from __future__ import annotations

import json
import logging

from agents.base import BaseMicroserviceAgent
from analyzer.impact_scorer import score_impact
from contracts.diff import ContractDiff
from contracts.models import ContractChange, BreakingChangeType, NonBreakingChangeType
from graph.consumer_finder import find_affected_consumers, serialise_consumers
from graph.dependency_graph import DependencyGraph

logger = logging.getLogger(__name__)


class DependencyGraphAgent(BaseMicroserviceAgent):
    """
    Traverses the dependency graph to identify affected downstream consumers.

    Reads:   state["repo_name"], state["contract_diff"], state["breaking_changes"]
    Writes:  state["dependency_graph"], state["downstream_consumers"],
             state["impact_radius"], state["impact_score"],
             state["affected_services"], state["hitl_required"]
    """

    NAME = "dependency_graph_agent"

    async def execute(self, state: dict) -> dict:
        repo_name = state["repo_name"]
        provider  = repo_name.split("/")[-1]

        # ── Load dependency graph ──────────────────────────────────────────
        graph: DependencyGraph
        try:
            graph_data = await self.call_mcp(
                "graph_store", "export_graph", {}
            )
            graph = DependencyGraph.from_dict(graph_data)
        except Exception as mcp_exc:
            self._log.warning("MCP graph load failed (%s) — trying SQLite", mcp_exc)
            try:
                import os
                from graph.graph_store import GraphStore
                db = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
                async with GraphStore(db) as store:
                    graph = await store.load_graph()
            except Exception as exc:
                self._log.warning("SQLite graph load also failed (%s) — using empty graph", exc)
                graph = DependencyGraph()

        # ── Identify changed endpoints (structured — preserves HTTP method) ──
        changed_endpoints = [
            {
                "pattern": ep.get("endpoint", ""),
                "methods": [ep["method"].upper()] if ep.get("method") else [],
            }
            for ep in state.get("changed_endpoints", [])
        ]
        if not changed_endpoints:
            # No contract changes — nothing to traverse
            return {
                "dependency_graph":     graph.to_dict(),
                "downstream_consumers": [],
                "impact_radius":        0,
                "impact_score":         0.0,
                "affected_services":    [],
                "hitl_required":        False,
            }

        # ── Rebuild a minimal ContractDiff for impact scoring ──────────────
        breaking_raw = state.get("breaking_changes", [])
        diff = _rebuild_diff(provider, state.get("contract_diff", {}), breaking_raw)

        # ── Score impact ───────────────────────────────────────────────────
        assessment = score_impact(diff, graph, provider)

        # ── Find enriched consumer context ─────────────────────────────────
        consumers = find_affected_consumers(graph, provider, changed_endpoints)

        self._log.info(
            "Graph traversal — provider=%s radius=%d score=%.2f hitl=%s",
            provider, assessment.impact_radius,
            assessment.impact_score, assessment.hitl_required,
        )

        return {
            "dependency_graph":     graph.to_dict(),
            "downstream_consumers": serialise_consumers(consumers),
            "impact_radius":        assessment.impact_radius,
            "impact_score":         assessment.impact_score,
            "affected_services":    assessment.affected_services,
            "hitl_required":        assessment.hitl_required,
        }


def _rebuild_diff(provider: str, diff_dict: dict, breaking_raw: list[dict]) -> "ContractDiff":
    """Reconstruct a minimal ContractDiff from serialised pipeline state."""
    changes: list[ContractChange] = []
    for c in breaking_raw:
        changes.append(ContractChange(
            change_type=BreakingChangeType.INCOMPATIBLE_CHANGE,
            is_breaking=True,
            endpoint=c.get("endpoint", ""),
            field=c.get("field", ""),
            before=c.get("before", ""),
            after=c.get("after", ""),
            severity=c.get("severity", "medium"),
            description=c.get("description", ""),
        ))
    # Add non-breaking from diff dict if available
    for c in diff_dict.get("changes", []):
        if not c.get("is_breaking"):
            changes.append(ContractChange(
                change_type=NonBreakingChangeType.ENDPOINT_ADDED,
                is_breaking=False,
                endpoint=c.get("endpoint", ""),
                severity=c.get("severity", "low"),
                description=c.get("description", ""),
            ))
    return ContractDiff(
        service=provider,
        base_version=diff_dict.get("base_version", ""),
        head_version=diff_dict.get("head_version", ""),
        changes=changes,
    )
