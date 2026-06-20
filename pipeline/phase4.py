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
pipeline/phase4.py
───────────────────
Phase 4 subgraph — Impact Reporting.

Runs ImpactReportAgent to aggregate compliance results, produce the
cross-repo impact report, file GitHub issues, and send Slack notifications.
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agents.impact_report_agent import ImpactReportAgent
from pipeline.state import MicroservicePipelineState

logger = logging.getLogger(__name__)


async def generate_report_node(state: MicroservicePipelineState) -> dict:
    """Run ImpactReportAgent to produce the final report and notifications."""
    agent = ImpactReportAgent(mcp_clients=state.get("_mcp_clients"))
    return await agent.run(state)


def build_phase4() -> StateGraph:
    builder = StateGraph(MicroservicePipelineState)
    builder.add_node("generate_report", generate_report_node)
    builder.add_edge(START, "generate_report")
    builder.add_edge("generate_report", END)
    return builder


phase4_app = build_phase4().compile()
