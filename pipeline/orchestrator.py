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
pipeline/orchestrator.py
──────────────────────────
Main StateGraph orchestrator for the K11tech Microservice QA Pipeline.

Flow:
  START → phase1 → phase2 → [hitl_check] → [human_review] → phase3 → phase4 → END
                                                              ↓
                                                     pipeline_rejected → END

The pipeline short-circuits after Phase 1 if no contract files changed.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from pipeline.confidence import aggregate_agent_confidence, apply_drift_floor, downgrade_compatible_verdicts
from pipeline.hitl import (
    cross_repo_hitl_check,
    cross_repo_human_review,
    pipeline_rejected,
    route_after_hitl,
)
from pipeline.phase1 import phase1_app
from pipeline.phase2 import phase2_app
from pipeline.phase3 import phase3_app
from pipeline.phase4 import phase4_app
from pipeline.state import MicroservicePipelineState

logger = logging.getLogger(__name__)


async def aggregate_confidence_node(state: MicroservicePipelineState) -> dict:
    """Aggregate per-agent confidence scores and apply verdict downgrade when uncertainty is HIGH."""
    scores  = state.get("agent_confidence_scores") or {}
    summary = aggregate_agent_confidence(scores)

    # Raise uncertainty floor if contract drift is HIGH/CRITICAL (Feature 4)
    adjusted_uncertainty = apply_drift_floor(
        summary.uncertainty_score,
        state.get("drift_report"),
    )
    if adjusted_uncertainty != summary.uncertainty_score:
        drift = state.get("drift_report", {})
        logger.warning(
            "Drift floor applied — service=%s drift=%s floor=%.2f "
            "uncertainty %.3f → %.3f",
            drift.get("service", "?"), drift.get("drift_level", "?"),
            drift.get("uncertainty_floor", 0.0),
            summary.uncertainty_score, adjusted_uncertainty,
        )
        # Recompute verdict with floored score
        from pipeline.confidence import UNCERTAINTY_THRESHOLD
        if adjusted_uncertainty < 0.20:
            summary = summary.__class__(
                mean=summary.mean, minimum=summary.minimum,
                variance=summary.variance,
                uncertainty_score=adjusted_uncertainty, verdict="LOW",
            )
        elif adjusted_uncertainty < UNCERTAINTY_THRESHOLD:
            summary = summary.__class__(
                mean=summary.mean, minimum=summary.minimum,
                variance=summary.variance,
                uncertainty_score=adjusted_uncertainty, verdict="MEDIUM",
            )
        else:
            summary = summary.__class__(
                mean=summary.mean, minimum=summary.minimum,
                variance=summary.variance,
                uncertainty_score=adjusted_uncertainty, verdict="HIGH",
            )

    logger.info(
        "Confidence aggregation — agents=%d mean=%.3f uncertainty=%.3f verdict=%s",
        len(scores), summary.mean, summary.uncertainty_score, summary.verdict,
    )

    adjusted, downgrade_count = downgrade_compatible_verdicts(
        compliance_results=state.get("compliance_results", []),
        agent_confidence_scores=scores,
        uncertainty_verdict=summary.verdict,
    )
    if downgrade_count:
        logger.warning(
            "Verdict downgrade: %d COMPATIBLE → UNCERTAIN (uncertainty_verdict=%s)",
            downgrade_count, summary.verdict,
        )

    return {
        "uncertainty_score":           summary.uncertainty_score,
        "uncertainty_verdict":         summary.verdict,
        "adjusted_compliance_results": adjusted if downgrade_count else None,
    }


def route_after_phase1(state: MicroservicePipelineState) -> str:
    """Short-circuit if no contract changes were detected."""
    if not state.get("changed_endpoints"):
        return "no_changes"
    return "phase2"


def build_orchestrator() -> StateGraph:
    """
    Assemble the full microservice QA pipeline.
    Returns an uncompiled builder — compile at call-site to inject checkpointer.
    """
    builder = StateGraph(MicroservicePipelineState)

    # ── Phase subgraph nodes ──────────────────────────────────────────────
    builder.add_node("phase1", phase1_app)
    builder.add_node("phase2", phase2_app)
    builder.add_node("phase3", phase3_app)
    builder.add_node("phase4", phase4_app)

    # ── Confidence aggregation node ───────────────────────────────────────
    builder.add_node("aggregate_confidence", aggregate_confidence_node)

    # ── HITL gate nodes ───────────────────────────────────────────────────
    builder.add_node("hitl_check",         cross_repo_hitl_check)
    builder.add_node("human_review_gate",  cross_repo_human_review)
    builder.add_node("pipeline_rejected",  pipeline_rejected)

    # ── Edges ─────────────────────────────────────────────────────────────
    builder.add_edge(START, "phase1")

    # Phase 1 → phase2 (contract changes detected) or END (no changes)
    builder.add_conditional_edges(
        "phase1",
        route_after_phase1,
        {"phase2": "phase2", "no_changes": END},
    )

    # Phase 2 → Phase 3 (run compliance checks first so HITL has full consumer data)
    builder.add_edge("phase2", "phase3")

    # Phase 3 → confidence aggregation → HITL check
    builder.add_edge("phase3", "aggregate_confidence")
    builder.add_edge("aggregate_confidence", "hitl_check")

    # HITL check → human review (if required) or directly to phase4
    builder.add_conditional_edges(
        "hitl_check",
        lambda s: "review" if s.get("hitl_required") else "proceed",
        {"review": "human_review_gate", "proceed": "phase4"},
    )

    # Human review → phase4 (approved) or rejected
    builder.add_conditional_edges(
        "human_review_gate",
        route_after_hitl,
        {"proceed": "phase4", "reject": "pipeline_rejected"},
    )

    builder.add_edge("phase4", END)
    builder.add_edge("pipeline_rejected", END)

    return builder


# Module-level builder — compile at runtime with a checkpointer for HITL support
microservice_builder = build_orchestrator()
