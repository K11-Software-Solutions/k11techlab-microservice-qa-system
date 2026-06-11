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
pipeline/phase3.py
───────────────────
Phase 3 subgraph — Parallel Consumer Validation.

Uses LangGraph Send API to dispatch one ContractComplianceAgent
per downstream consumer in parallel. Results accumulate via
the list-append reducer on compliance_results.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

import os

from agents.contract_compliance_agent import ContractComplianceAgent
from pipeline.state import MicroservicePipelineState

logger = logging.getLogger(__name__)

# Feature 5 — Transitive Confidence Decay
# Each additional hop introduces propagation uncertainty:
#   depth 1 (direct):  multiplier = 1.00  (unchanged)
#   depth 2:           multiplier = DEPTH_DECAY^1
#   depth 3:           multiplier = DEPTH_DECAY^2
# Default 1.0 = no decay (backwards-compatible with Evals 1–5).
DEPTH_DECAY: float = float(os.getenv("DEPTH_DECAY", "1.0"))


# ── Fan-out dispatcher ────────────────────────────────────────────────────────

def dispatch_consumers(state: MicroservicePipelineState) -> list[Send] | str:
    """
    Fan-out: dispatch a validate_consumer node for each downstream consumer.
    If there are no consumers, skip directly to END.
    """
    consumers = state.get("downstream_consumers", [])
    if not consumers:
        logger.info("Phase 3: no downstream consumers — skipping validation")
        return END

    contract_diff  = state.get("contract_diff", {})
    breaking       = state.get("breaking_changes", [])
    mcp_clients    = state.get("_mcp_clients")

    sends: list[Send] = []
    for consumer_ctx in consumers:
        sends.append(Send(
            "validate_consumer",
            {
                "consumer_ctx":   consumer_ctx,
                "contract_diff":  contract_diff,
                "breaking_changes": breaking,
                "_mcp_clients":   mcp_clients,
            },
        ))
    logger.info("Phase 3: dispatching %d parallel consumer validations", len(sends))
    return sends


# ── Worker node ───────────────────────────────────────────────────────────────

async def validate_consumer_node(worker_state: dict) -> dict:
    """
    Validate one consumer. Returns partial state update with compliance_results.
    """
    consumer_ctx  = worker_state["consumer_ctx"]
    contract_diff = worker_state.get("contract_diff", {})
    breaking      = worker_state.get("breaking_changes", [])
    mcp_clients   = worker_state.get("_mcp_clients")

    agent = ContractComplianceAgent(mcp_clients=mcp_clients)
    hop_depth = consumer_ctx.get("hop_depth", 1)
    result = await agent.run(
        consumer=consumer_ctx["consumer"],
        contract_diff={"breaking_changes": breaking, "diff": contract_diff},
        usage_patterns={
            "repo":                consumer_ctx.get("repo", ""),
            "endpoints_called":    [
                {"endpoint": ep,
                 "methods": consumer_ctx.get("edge_methods", ["GET"]),
                 "criticality": consumer_ctx.get("edge_criticality", "medium")}
                for ep in consumer_ctx.get("consuming_endpoints", [])
            ],
            "edge_criticality":    consumer_ctx.get("edge_criticality", "medium"),
            "is_direct_consumer":  consumer_ctx.get("is_direct", True),
            "hop_depth":           hop_depth,
        },
        hop_depth=hop_depth,
    )

    # Violations bubble up to top-level state via list-append reducer
    violations = [
        {**v, "consumer": consumer_ctx["consumer"]}
        for v in result.violations
    ]

    # Apply transitive decay: each intermediate hop reduces effective confidence.
    # The original confidence is preserved in compliance_results for reviewers;
    # the decayed value enters uncertainty aggregation.
    decay_multiplier = DEPTH_DECAY ** max(0, hop_depth - 1)
    decayed_confidence = result.confidence * decay_multiplier

    if decay_multiplier < 1.0:
        logger.info(
            "Consumer %s: verdict=%s violations=%d confidence=%.2f "
            "(depth=%d decay=%.2f → effective=%.2f)",
            result.consumer, result.verdict, len(violations),
            result.confidence, hop_depth, decay_multiplier, decayed_confidence,
        )
    else:
        logger.info(
            "Consumer %s: verdict=%s violations=%d confidence=%.2f",
            result.consumer, result.verdict, len(violations), result.confidence,
        )

    return {
        "compliance_results": [{
            "consumer":          result.consumer,
            "verdict":           result.verdict,
            "violations":        result.violations,
            "reasoning":         result.reasoning,
            "confidence":        result.confidence,        # original — for display/calibration
            "confidence_decayed": decayed_confidence,      # decay-adjusted — informational
            "hop_depth":         hop_depth,
            "error":             result.error,
        }],
        "violations": violations,
        "errors": [result.error] if result.error else [],
        "agent_confidence_scores": {
            # Decayed confidence feeds uncertainty aggregation (Feature 5)
            f"contract_compliance_agent:{result.consumer}": decayed_confidence
        },
    }


# ── Subgraph assembly ─────────────────────────────────────────────────────────

def build_phase3() -> StateGraph:
    builder = StateGraph(MicroservicePipelineState)
    builder.add_node("validate_consumer", validate_consumer_node)
    builder.add_conditional_edges(START, dispatch_consumers, ["validate_consumer", END])
    builder.add_edge("validate_consumer", END)
    return builder


phase3_app = build_phase3().compile()
