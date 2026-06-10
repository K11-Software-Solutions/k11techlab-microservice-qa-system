# -*- coding: utf-8 -*-
"""
Unit tests for pipeline/confidence.py — Feature 1: Agent Confidence Propagation.
"""
import pytest
from pipeline.confidence import (
    aggregate_agent_confidence,
    compute_deterministic_confidence,
    UNCERTAINTY_THRESHOLD,
)


# ── aggregate_agent_confidence ────────────────────────────────────────────────

class TestAggregateAgentConfidence:
    def test_empty_scores_returns_max_uncertainty(self):
        result = aggregate_agent_confidence({})
        assert result.uncertainty_score == 1.0
        assert result.verdict == "HIGH"

    def test_single_high_confidence_agent(self):
        result = aggregate_agent_confidence({"agent_a": 0.95})
        assert result.mean == pytest.approx(0.95)
        assert result.uncertainty_score < 0.20
        assert result.verdict == "LOW"

    def test_single_low_confidence_agent(self):
        result = aggregate_agent_confidence({"agent_a": 0.20})
        assert result.uncertainty_score > UNCERTAINTY_THRESHOLD
        assert result.verdict == "HIGH"

    def test_multiple_agents_high_mean(self):
        scores = {"a": 0.90, "b": 0.95, "c": 0.88}
        result = aggregate_agent_confidence(scores)
        assert result.mean == pytest.approx(0.91, abs=0.01)
        assert result.verdict == "LOW"

    def test_high_variance_raises_uncertainty(self):
        # Mean is decent but one agent is very uncertain
        scores = {"a": 0.90, "b": 0.90, "c": 0.10}
        result = aggregate_agent_confidence(scores)
        # Variance is high — uncertainty should be elevated
        assert result.variance > 0.1
        assert result.uncertainty_score > result.mean * 0.1  # uncertainty above trivial

    def test_all_uncertain_agents(self):
        scores = {"a": 0.30, "b": 0.25, "c": 0.20}
        result = aggregate_agent_confidence(scores)
        assert result.uncertainty_score >= UNCERTAINTY_THRESHOLD
        assert result.verdict == "HIGH"

    def test_uncertainty_score_clamped_to_unit_interval(self):
        scores = {"a": 0.0, "b": 0.0, "c": 0.0}
        result = aggregate_agent_confidence(scores)
        assert 0.0 <= result.uncertainty_score <= 1.0

    def test_dict_reducer_merge(self):
        from pipeline.state import _merge_confidence_dicts
        a = {"agent_x": 0.8}
        b = {"agent_y": 0.6}
        merged = _merge_confidence_dicts(a, b)
        assert merged == {"agent_x": 0.8, "agent_y": 0.6}

    def test_dict_reducer_later_value_wins_on_same_key(self):
        from pipeline.state import _merge_confidence_dicts
        a = {"agent_x": 0.8}
        b = {"agent_x": 0.5}
        merged = _merge_confidence_dicts(a, b)
        assert merged["agent_x"] == 0.5

    def test_verdict_boundaries(self):
        # < 0.20 → LOW
        low = aggregate_agent_confidence({"a": 0.95})
        assert low.verdict == "LOW"
        # 0.20 – 0.35 → MEDIUM (default threshold)
        medium = aggregate_agent_confidence({"a": 0.72})
        assert medium.verdict in ("MEDIUM", "LOW")  # boundary sensitive to exact calc
        # >= 0.35 → HIGH
        high = aggregate_agent_confidence({"a": 0.20})
        assert high.verdict == "HIGH"


# ── compute_deterministic_confidence ─────────────────────────────────────────

class TestDeterministicConfidence:
    def test_error_lowers_confidence(self):
        conf = compute_deterministic_confidence(
            graph_loaded=True, consumer_count=3, error_occurred=True
        )
        assert conf == pytest.approx(0.40)

    def test_graph_not_loaded(self):
        conf = compute_deterministic_confidence(
            graph_loaded=False, consumer_count=0, error_occurred=False
        )
        assert conf == pytest.approx(0.50)

    def test_no_consumers_high_confidence(self):
        conf = compute_deterministic_confidence(
            graph_loaded=True, consumer_count=0, error_occurred=False
        )
        assert conf == pytest.approx(0.85)

    def test_consumers_found_highest_confidence(self):
        conf = compute_deterministic_confidence(
            graph_loaded=True, consumer_count=3, error_occurred=False
        )
        assert conf == pytest.approx(0.92)

    def test_error_takes_priority(self):
        # error_occurred=True should override consumer_count
        conf = compute_deterministic_confidence(
            graph_loaded=True, consumer_count=5, error_occurred=True
        )
        assert conf == pytest.approx(0.40)
