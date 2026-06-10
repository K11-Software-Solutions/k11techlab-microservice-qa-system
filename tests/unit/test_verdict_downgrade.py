# -*- coding: utf-8 -*-
"""
Unit tests for Feature 3 — Confidence-Aware Verdict Downgrade.
"""
import pytest
from pipeline.confidence import (
    downgrade_compatible_verdicts,
    CONFIDENCE_DOWNGRADE_THRESHOLD,
    UNCERTAINTY_THRESHOLD,
)


def _make_result(consumer: str, verdict: str, reasoning: str = "") -> dict:
    return {"consumer": consumer, "verdict": verdict, "reasoning": reasoning,
            "violations": [], "confidence": 0.5}


def _scores(*pairs) -> dict:
    """Build agent_confidence_scores dict from (consumer, score) pairs."""
    return {f"contract_compliance_agent:{c}": s for c, s in pairs}


# ── Core downgrade behaviour ──────────────────────────────────────────────────

class TestDowngradeCompatibleVerdicts:

    def test_no_downgrade_when_uncertainty_low(self):
        results = [_make_result("svc-a", "COMPATIBLE")]
        scores  = _scores(("svc-a", 0.20))
        adjusted, count = downgrade_compatible_verdicts(results, scores, "LOW")
        assert count == 0
        assert adjusted[0]["verdict"] == "COMPATIBLE"

    def test_no_downgrade_when_uncertainty_medium(self):
        results = [_make_result("svc-a", "COMPATIBLE")]
        scores  = _scores(("svc-a", 0.20))
        adjusted, count = downgrade_compatible_verdicts(results, scores, "MEDIUM")
        assert count == 0
        assert adjusted[0]["verdict"] == "COMPATIBLE"

    def test_compatible_low_confidence_downgraded_when_high(self):
        results = [_make_result("svc-a", "COMPATIBLE")]
        scores  = _scores(("svc-a", 0.20))   # below CONFIDENCE_DOWNGRADE_THRESHOLD (0.50)
        adjusted, count = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert count == 1
        assert adjusted[0]["verdict"] == "UNCERTAIN"

    def test_compatible_above_threshold_not_downgraded(self):
        results = [_make_result("svc-a", "COMPATIBLE")]
        scores  = _scores(("svc-a", 0.80))   # above threshold
        adjusted, count = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert count == 0
        assert adjusted[0]["verdict"] == "COMPATIBLE"

    def test_compatible_at_exact_threshold_not_downgraded(self):
        results = [_make_result("svc-a", "COMPATIBLE")]
        scores  = _scores(("svc-a", CONFIDENCE_DOWNGRADE_THRESHOLD))
        adjusted, count = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert count == 0

    def test_breaking_verdict_never_downgraded(self):
        results = [_make_result("svc-a", "BREAKING")]
        scores  = _scores(("svc-a", 0.10))
        adjusted, count = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert count == 0
        assert adjusted[0]["verdict"] == "BREAKING"

    def test_uncertain_verdict_not_double_downgraded(self):
        results = [_make_result("svc-a", "UNCERTAIN")]
        scores  = _scores(("svc-a", 0.10))
        adjusted, count = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert count == 0
        assert adjusted[0]["verdict"] == "UNCERTAIN"

    def test_reasoning_annotated_on_downgrade(self):
        results = [_make_result("svc-a", "COMPATIBLE", reasoning="Looks fine")]
        scores  = _scores(("svc-a", 0.20))
        adjusted, _ = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert "[Downgraded COMPATIBLE" in adjusted[0]["reasoning"]
        assert "Looks fine" in adjusted[0]["reasoning"]

    def test_zero_downgrades_returns_original_list_unchanged(self):
        results = [_make_result("svc-a", "COMPATIBLE")]
        scores  = _scores(("svc-a", 0.90))
        adjusted, count = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert count == 0
        assert adjusted is not results   # new list, but content same
        assert adjusted[0]["verdict"] == "COMPATIBLE"

    def test_missing_score_key_defaults_to_high_confidence(self):
        """Consumer with no score entry should NOT be downgraded (safe default)."""
        results = [_make_result("svc-unknown", "COMPATIBLE")]
        adjusted, count = downgrade_compatible_verdicts(results, {}, "HIGH")
        assert count == 0
        assert adjusted[0]["verdict"] == "COMPATIBLE"

    def test_mixed_results_partial_downgrade(self):
        results = [
            _make_result("svc-a", "COMPATIBLE"),   # low confidence → downgrade
            _make_result("svc-b", "COMPATIBLE"),   # high confidence → keep
            _make_result("svc-c", "BREAKING"),     # BREAKING → untouched
            _make_result("svc-d", "UNCERTAIN"),    # UNCERTAIN → untouched
        ]
        scores = _scores(("svc-a", 0.15), ("svc-b", 0.85), ("svc-c", 0.10), ("svc-d", 0.10))
        adjusted, count = downgrade_compatible_verdicts(results, scores, "HIGH")
        assert count == 1
        verdicts = {r["consumer"]: r["verdict"] for r in adjusted}
        assert verdicts["svc-a"] == "UNCERTAIN"
        assert verdicts["svc-b"] == "COMPATIBLE"
        assert verdicts["svc-c"] == "BREAKING"
        assert verdicts["svc-d"] == "UNCERTAIN"

    def test_empty_results_returns_empty(self):
        adjusted, count = downgrade_compatible_verdicts([], {}, "HIGH")
        assert adjusted == []
        assert count == 0

    def test_original_dict_not_mutated(self):
        original = _make_result("svc-a", "COMPATIBLE")
        results  = [original]
        scores   = _scores(("svc-a", 0.10))
        adjusted, _ = downgrade_compatible_verdicts(results, scores, "HIGH")
        # Original dict must not be mutated
        assert original["verdict"] == "COMPATIBLE"
        assert adjusted[0]["verdict"] == "UNCERTAIN"
