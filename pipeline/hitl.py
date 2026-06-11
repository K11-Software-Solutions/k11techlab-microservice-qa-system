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
pipeline/hitl.py
─────────────────
Cross-Repository HITL Gate.

Two independent triggers:
  1. impact_score >= IMPACT_HITL_THRESHOLD (default 0.60)
  2. breaking_consumers >= BREAKING_CONSUMER_HITL_COUNT (default 2)

Pauses execution via interrupt() — resumes via graph.update_state().
"""
from __future__ import annotations

import logging
import os

from langgraph.types import interrupt

from analyzer.impact_scorer import BREAKING_CONSUMER_HITL_COUNT, IMPACT_HITL_THRESHOLD
from pipeline.confidence import UNCERTAINTY_THRESHOLD
from pipeline.state import MicroservicePipelineState

logger = logging.getLogger(__name__)


async def _get_effective_uncertainty_threshold() -> tuple[float, str]:
    """
    Return (threshold, description) for the uncertainty gate.

    Uses the conformal threshold (Feature 8) when sufficient calibration data
    exists; falls back to the static UNCERTAINTY_THRESHOLD env-var otherwise.
    """
    if os.getenv("CALIBRATION_ENABLED", "true").lower() == "false":
        return UNCERTAINTY_THRESHOLD, f"static threshold {UNCERTAINTY_THRESHOLD}"
    try:
        from calibration.conformal import ConformalHITLThreshold
        from calibration.store import CalibrationStore, CALIBRATION_DB
        async with CalibrationStore(CALIBRATION_DB) as _store:
            ct = await ConformalHITLThreshold.from_store(_store)
        if ct is not None:
            desc = (
                f"conformal threshold {ct.threshold:.3f} "
                f"(α={ct.alpha:.2f}, FNR≤{ct.fnr_upper_bound:.3f}, "
                f"n_wrong={ct.n_wrong})"
            )
            return ct.threshold, desc
    except Exception as exc:
        logger.debug("Conformal threshold unavailable (cold start or error): %s", exc)
    return UNCERTAINTY_THRESHOLD, f"static threshold {UNCERTAINTY_THRESHOLD}"


async def cross_repo_hitl_check(state: MicroservicePipelineState) -> dict:
    """
    Evaluate whether cross-repo impact requires human review.
    Sets hitl_required and sends a Slack notification if so.
    """
    impact_score = state.get("impact_score", 0.0) or 0.0
    # Prefer adjusted verdicts (post-downgrade) when available
    results = state.get("adjusted_compliance_results") or state.get("compliance_results", [])
    breaking_consumers = sum(1 for r in results if r.get("verdict") == "BREAKING")

    uncertainty_score = state.get("uncertainty_score", 0.0) or 0.0

    hitl_required = False
    hitl_reason   = ""

    if impact_score >= IMPACT_HITL_THRESHOLD:
        hitl_required = True
        hitl_reason   = (
            f"Impact score {impact_score:.2f} >= threshold {IMPACT_HITL_THRESHOLD}"
        )
    elif breaking_consumers >= BREAKING_CONSUMER_HITL_COUNT:
        hitl_required = True
        hitl_reason   = (
            f"{breaking_consumers} breaking consumer(s) >= "
            f"threshold {BREAKING_CONSUMER_HITL_COUNT}"
        )
    else:
        unc_threshold, unc_desc = await _get_effective_uncertainty_threshold()
        if uncertainty_score >= unc_threshold:
            hitl_required = True
            uncertainty_verdict = state.get("uncertainty_verdict", "HIGH")
            hitl_reason = (
                f"Low agent confidence detected — uncertainty_score={uncertainty_score:.3f} "
                f">= {unc_desc} (verdict={uncertainty_verdict}). "
                f"Human review required even though impact score is below threshold."
            )

    if hitl_required:
        logger.warning(
            "Cross-repo HITL required for PR #%d (%s): %s",
            state.get("pr_number", 0), state.get("repo_name", ""), hitl_reason,
        )
        # Notify via Slack if client available
        mcp = state.get("_mcp_clients", {}) or {}
        slack = mcp.get("slack")
        if slack:
            try:
                affected = state.get("affected_services", [])
                await slack.call_tool("post_message", {
                    "channel": "#cross-repo-impact",
                    "text": (
                        f":rotating_light: *Cross-Repo HITL Required*\n"
                        f"Provider: `{state['repo_name']}` | PR: #{state['pr_number']}\n"
                        f"Reason: {hitl_reason}\n"
                        f"Affected services: {', '.join(affected) or 'none detected'}\n"
                        f"Run ID: `{state['run_id']}`\n"
                        f"Approve: `/cross-repo-approve {state['run_id']}`\n"
                        f"Reject:  `/cross-repo-reject {state['run_id']} <reason>`"
                    ),
                    "mrkdwn": True,
                })
            except Exception as exc:
                logger.error("Slack HITL notification failed: %s", exc)

        # Store Phase 3 summary now — Phase 4 will overwrite after approval.
        # Without this, ainvoke() returns state["summary"]=None when interrupted,
        # and the eval (and /runs API) cannot determine the BREAKING verdict.
        compliance = state.get("adjusted_compliance_results") or state.get("compliance_results", [])
        uncertain_count  = sum(1 for r in compliance if r.get("verdict") == "UNCERTAIN")
        compatible_count = sum(1 for r in compliance if r.get("verdict") == "COMPATIBLE")
        preliminary_summary = {
            "overall_verdict":    "BREAKING",
            "hitl_required":      True,
            "hitl_reason":        hitl_reason,
            "impact_score":       impact_score,
            "impact_radius":      state.get("impact_radius", 0),
            "breaking_consumers": breaking_consumers,
            "uncertain_consumers": uncertain_count,
            "compatible_consumers": compatible_count,
            "breaking_changes":   len(state.get("breaking_changes", [])),
        }
        return {"hitl_required": True, "summary": preliminary_summary}

    return {"hitl_required": False}


async def cross_repo_human_review(state: MicroservicePipelineState) -> dict:
    """
    Pause execution pending human decision.
    Resumes when graph.update_state() is called with the decision.
    """
    drift = state.get("drift_report") or {}
    if drift.get("drift_level") in ("HIGH", "CRITICAL"):
        logger.warning(
            "High contract drift for %s: %d changes in %d days (%.1f/week)",
            state.get("repo_name", ""), drift.get("change_count", 0),
            drift.get("window_days", 30), drift.get("change_velocity", 0.0),
        )

    logger.info("Awaiting cross-repo HITL decision — run_id=%s", state.get("run_id"))
    decision = interrupt({
        "type":               "cross_repo_human_review",
        "run_id":             state.get("run_id"),
        "impact_score":       state.get("impact_score"),
        "impact_radius":      state.get("impact_radius"),
        "uncertainty_score":  state.get("uncertainty_score"),
        "uncertainty_verdict": state.get("uncertainty_verdict"),
        "drift_report":       drift or None,
        "affected_services":  state.get("affected_services", []),
        "breaking_changes":   state.get("breaking_changes", []),
        "repo_name":          state.get("repo_name"),
        "pr_number":          state.get("pr_number"),
        "message": (
            "Cross-repo impact detected. Review breaking changes and decide "
            "whether to approve (provider may merge; consumers must adapt) "
            "or reject (provider must maintain backwards compatibility)."
        ),
    })
    hitl_decision = decision.get("decision", "")
    reviewer      = decision.get("reviewer", "unknown")
    comment       = decision.get("comment", "")

    logger.info(
        "Cross-repo HITL decision: decision=%s reviewer=%s",
        hitl_decision, reviewer,
    )

    # Record reviewer decision as calibration labels (Feature 6)
    await _record_hitl_labels(state, hitl_decision, reviewer, comment)

    return {
        "hitl_decision": hitl_decision,
        "hitl_reviewer": reviewer,
        "hitl_comment":  comment,
    }


def route_after_hitl(state: MicroservicePipelineState) -> str:
    """Route to phase4 (report) after HITL decision, or terminate on rejection."""
    if not state.get("hitl_required"):
        return "proceed"
    decision = state.get("hitl_decision")
    return "proceed" if decision == "approve" else "reject"


async def _record_hitl_labels(
    state: MicroservicePipelineState,
    hitl_decision: str,
    reviewer: str,
    comment: str,
) -> None:
    """
    Persist reviewer decision as per-consumer calibration labels (Feature 6).

    Label mapping:
      approve/reject → correct for consumers with BREAKING verdict
      override       → incorrect (false positive) for BREAKING verdicts
    """
    import os
    calibration_enabled = os.getenv("CALIBRATION_ENABLED", "true").lower() != "false"
    if not calibration_enabled:
        return

    run_id = state.get("run_id", "")
    if not run_id:
        return

    results = (
        state.get("adjusted_compliance_results")
        or state.get("compliance_results", [])
    )
    if not results:
        return

    try:
        from calibration.store import CalibrationStore, CALIBRATION_DB
        async with CalibrationStore(CALIBRATION_DB) as store:
            for result in results:
                consumer = result.get("consumer", "")
                if not consumer:
                    continue
                await store.record_hitl_outcome(
                    run_id=run_id,
                    consumer=consumer,
                    hitl_decision=hitl_decision,
                    reviewer=reviewer,
                    comment=comment,
                )
        logger.info(
            "HITL labels recorded for run %s: %d consumers decision=%s",
            run_id, len(results), hitl_decision,
        )
    except Exception as exc:
        logger.warning("Failed to record HITL calibration labels: %s", exc)


async def pipeline_rejected(state: MicroservicePipelineState) -> dict:
    """Terminal node for cross-repo rejected pipelines."""
    from datetime import datetime, timezone
    reviewer = state.get("hitl_reviewer", "unknown")
    reason   = state.get("hitl_comment") or "No reason provided."
    report = (
        f"# Cross-Repo Pipeline Rejected\n\n"
        f"**Provider:** {state.get('repo_name')} | **PR:** #{state.get('pr_number')}\n\n"
        f"**Rejected by:** {reviewer}\n\n"
        f"**Reason:** {reason}\n\n"
        f"The provider PR must be revised to maintain API backwards compatibility "
        f"before it can be merged.\n\n"
        f"**Run ID:** `{state.get('run_id')}`"
    )
    logger.info("Cross-repo pipeline rejected by %s: %s", reviewer, reason)

    mcp = state.get("_mcp_clients", {}) or {}
    slack = mcp.get("slack")
    if slack:
        try:
            await slack.call_tool("post_message", {
                "channel": "#cross-repo-impact",
                "text": (
                    f":no_entry: *Cross-repo PR rejected by {reviewer}*\n"
                    f"PR #{state.get('pr_number')} in `{state.get('repo_name')}`\n"
                    f"Reason: {reason}"
                ),
            })
        except Exception:
            pass

    return {
        "final_report": report,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
