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
pipeline/phase1.py
───────────────────
Phase 1 subgraph — Contract Extraction.

Nodes:
  extract_contract → store_contract → (route: has_changes | no_changes)
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agents.contract_extractor_agent import ContractExtractorAgent
from pipeline.state import MicroservicePipelineState

logger = logging.getLogger(__name__)


async def extract_contract_node(state: MicroservicePipelineState) -> dict:
    """Run ContractExtractorAgent to extract and diff contracts."""
    agent = ContractExtractorAgent(mcp_clients=state.get("_mcp_clients"))
    return await agent.run(state)


def route_phase1(state: MicroservicePipelineState) -> str:
    """If no contract changes detected, short-circuit the pipeline."""
    if not state.get("changed_endpoints"):
        logger.info(
            "Phase 1: no contract changes detected for PR #%d — exiting pipeline",
            state.get("pr_number", 0),
        )
        return "no_changes"
    return "has_changes"


def build_phase1() -> StateGraph:
    builder = StateGraph(MicroservicePipelineState)
    builder.add_node("extract_contract", extract_contract_node)
    builder.add_edge(START, "extract_contract")
    builder.add_conditional_edges(
        "extract_contract",
        route_phase1,
        {"has_changes": END, "no_changes": END},
    )
    return builder


phase1_app = build_phase1().compile()
