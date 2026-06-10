# -*- coding: utf-8 -*-
"""
Unit tests for the uncertainty-based HITL trigger — Feature 1.

Verifies that cross_repo_hitl_check fires when uncertainty_score >= threshold
even when impact_score and breaking_consumer count are below their own thresholds.
"""
import pytest
from unittest.mock import AsyncMock, patch
from pipeline.hitl import cross_repo_hitl_check
from pipeline.confidence import UNCERTAINTY_THRESHOLD


def _base_state(**overrides) -> dict:
    state = {
        "run_id":             "test-run-001",
        "pr_number":          42,
        "repo_name":          "org/service-a",
        "impact_score":       0.10,   # well below IMPACT_HITL_THRESHOLD (0.60)
        "compliance_results": [],     # no breaking consumers
        "uncertainty_score":  0.0,
        "uncertainty_verdict": "LOW",
        "affected_services":  [],
        "breaking_changes":   [],
        "impact_radius":      0,
        "_mcp_clients":       {},
    }
    state.update(overrides)
    return state


class TestHITLUncertaintyTrigger:

    @pytest.mark.asyncio
    async def test_no_hitl_when_all_scores_low(self):
        state = _base_state(uncertainty_score=0.10)
        result = await cross_repo_hitl_check(state)
        assert result["hitl_required"] is False

    @pytest.mark.asyncio
    async def test_hitl_triggered_by_uncertainty_alone(self):
        state = _base_state(
            uncertainty_score=UNCERTAINTY_THRESHOLD + 0.01,
            uncertainty_verdict="HIGH",
        )
        result = await cross_repo_hitl_check(state)
        assert result["hitl_required"] is True
        assert "uncertainty" in result["summary"]["hitl_reason"].lower()

    @pytest.mark.asyncio
    async def test_hitl_triggered_by_impact_score(self):
        state = _base_state(impact_score=0.70, uncertainty_score=0.0)
        result = await cross_repo_hitl_check(state)
        assert result["hitl_required"] is True
        assert "impact score" in result["summary"]["hitl_reason"].lower()

    @pytest.mark.asyncio
    async def test_hitl_triggered_by_breaking_consumers(self):
        state = _base_state(
            compliance_results=[
                {"verdict": "BREAKING", "consumer": "svc-b"},
                {"verdict": "BREAKING", "consumer": "svc-c"},
            ],
            uncertainty_score=0.0,
        )
        result = await cross_repo_hitl_check(state)
        assert result["hitl_required"] is True

    @pytest.mark.asyncio
    async def test_uncertainty_at_exact_threshold_triggers(self):
        state = _base_state(uncertainty_score=UNCERTAINTY_THRESHOLD)
        result = await cross_repo_hitl_check(state)
        assert result["hitl_required"] is True

    @pytest.mark.asyncio
    async def test_uncertainty_just_below_threshold_does_not_trigger(self):
        state = _base_state(uncertainty_score=UNCERTAINTY_THRESHOLD - 0.001)
        result = await cross_repo_hitl_check(state)
        assert result["hitl_required"] is False

    @pytest.mark.asyncio
    async def test_missing_uncertainty_score_does_not_crash(self):
        state = _base_state()
        del state["uncertainty_score"]
        result = await cross_repo_hitl_check(state)
        assert "hitl_required" in result
