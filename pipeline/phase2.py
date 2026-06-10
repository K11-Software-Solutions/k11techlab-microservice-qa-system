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
pipeline/phase2.py
───────────────────
Phase 2 subgraph — Dependency Graph Traversal.

Runs DependencyGraphAgent which:
  - Loads the live dependency graph
  - Finds all downstream consumers of the changed service
  - Scores impact radius and sets hitl_required if thresholds are exceeded
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agents.dependency_graph_agent import DependencyGraphAgent
from pipeline.confidence import compute_deterministic_confidence
from pipeline.state import MicroservicePipelineState

logger = logging.getLogger(__name__)


async def traverse_graph_node(state: MicroservicePipelineState) -> dict:
    """Run DependencyGraphAgent and attach deterministic confidence score."""
    agent = DependencyGraphAgent(mcp_clients=state.get("_mcp_clients"))
    result = await agent.run(state)

    graph_loaded   = bool(result.get("dependency_graph"))
    consumer_count = len(result.get("downstream_consumers", []))
    error_occurred = bool(result.get("errors"))

    confidence = compute_deterministic_confidence(
        graph_loaded=graph_loaded,
        consumer_count=consumer_count,
        error_occurred=error_occurred,
    )
    result["agent_confidence_scores"] = {"dependency_graph_agent": confidence}
    return result


def build_phase2() -> StateGraph:
    builder = StateGraph(MicroservicePipelineState)
    builder.add_node("traverse_graph", traverse_graph_node)
    builder.add_edge(START, "traverse_graph")
    builder.add_edge("traverse_graph", END)
    return builder


phase2_app = build_phase2().compile()
