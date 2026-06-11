# -*- coding: utf-8 -*-
"""
Unit tests for the calibration study infrastructure.

Covers:
  - CalibrationStore: record_run, resolve_ground_truth, get_resolved_rows,
    get_pending_runs, stats
  - compute_calibration: overall, per-agent, per-hop-depth, ECE, accuracy
  - _is_correct: verdict/ground-truth matching logic
"""
import pytest
import pytest_asyncio

from calibration.store import CalibrationStore
from calibration.curves import compute_calibration, _is_correct, CalibrationResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def store(tmp_path):
    """In-memory (tmp) CalibrationStore for each test."""
    db = str(tmp_path / "test_cal.db")
    async with CalibrationStore(db) as s:
        yield s


def _compliance(consumer, verdict, confidence=0.8):
    return {"consumer": consumer, "verdict": verdict, "confidence": confidence}


# ── CalibrationStore ──────────────────────────────────────────────────────────

class TestCalibrationStore:

    @pytest.mark.asyncio
    async def test_record_run_inserts_rows(self, store):
        results = [
            _compliance("svc-a", "COMPATIBLE", 0.9),
            _compliance("svc-b", "BREAKING",   0.2),
        ]
        n = await store.record_run(
            run_id="run-1", repo_name="org/provider", pr_number=42,
            compliance_results=results,
            agent_confidence_scores={
                "contract_compliance_agent:svc-a": 0.9,
                "contract_compliance_agent:svc-b": 0.2,
            },
        )
        assert n == 2

    @pytest.mark.asyncio
    async def test_initial_ground_truth_is_unknown(self, store):
        await store.record_run(
            run_id="run-1", repo_name="org/provider", pr_number=1,
            compliance_results=[_compliance("svc-a", "COMPATIBLE", 0.9)],
            agent_confidence_scores={"contract_compliance_agent:svc-a": 0.9},
        )
        rows = await store.get_resolved_rows()
        # No resolved rows yet
        assert rows == []

    @pytest.mark.asyncio
    async def test_resolve_ground_truth_single(self, store):
        await store.record_run(
            run_id="run-1", repo_name="org/p", pr_number=1,
            compliance_results=[_compliance("svc-a", "COMPATIBLE", 0.8)],
            agent_confidence_scores={},
        )
        await store.resolve_ground_truth("run-1", "svc-a", "COMPATIBLE", "manual")
        rows = await store.get_resolved_rows()
        assert len(rows) == 1
        assert rows[0]["ground_truth"] == "COMPATIBLE"
        assert rows[0]["gt_source"] == "manual"

    @pytest.mark.asyncio
    async def test_resolve_run_ground_truth_bulk(self, store):
        await store.record_run(
            run_id="run-1", repo_name="org/p", pr_number=1,
            compliance_results=[
                _compliance("svc-a", "COMPATIBLE", 0.9),
                _compliance("svc-b", "BREAKING",   0.1),
            ],
            agent_confidence_scores={},
        )
        await store.resolve_run_ground_truth(
            "run-1",
            {"svc-a": "COMPATIBLE", "svc-b": "BREAKING"},
            gt_source="ci_failure",
        )
        rows = await store.get_resolved_rows()
        assert len(rows) == 2
        by_consumer = {r["consumer"]: r for r in rows}
        assert by_consumer["svc-a"]["ground_truth"] == "COMPATIBLE"
        assert by_consumer["svc-b"]["ground_truth"] == "BREAKING"

    @pytest.mark.asyncio
    async def test_stats_counts_correctly(self, store):
        await store.record_run(
            run_id="run-1", repo_name="org/p", pr_number=1,
            compliance_results=[
                _compliance("svc-a", "COMPATIBLE", 0.9),
                _compliance("svc-b", "BREAKING",   0.1),
            ],
            agent_confidence_scores={},
        )
        await store.resolve_ground_truth("run-1", "svc-a", "COMPATIBLE", "manual")
        stats = await store.stats()
        assert stats["total"] == 2
        assert stats["resolved"] == 1
        assert stats["pending"] == 1

    @pytest.mark.asyncio
    async def test_hop_depth_recorded(self, store):
        await store.record_run(
            run_id="run-1", repo_name="org/p", pr_number=1,
            compliance_results=[_compliance("svc-c", "COMPATIBLE", 0.7)],
            agent_confidence_scores={},
            hop_depths={"svc-c": 2},
        )
        await store.resolve_ground_truth("run-1", "svc-c", "COMPATIBLE", "manual")
        rows = await store.get_resolved_rows()
        assert rows[0]["hop_depth"] == 2

    @pytest.mark.asyncio
    async def test_empty_results_records_zero_rows(self, store):
        n = await store.record_run(
            run_id="run-1", repo_name="org/p", pr_number=1,
            compliance_results=[],
            agent_confidence_scores={},
        )
        assert n == 0


# ── compute_calibration ───────────────────────────────────────────────────────

def _resolved_row(consumer, confidence, verdict, ground_truth,
                  agent=None, hop_depth=1, gt_source="manual"):
    return {
        "consumer":     consumer,
        "confidence":   confidence,
        "verdict":      verdict,
        "ground_truth": ground_truth,
        "agent":        agent or f"contract_compliance_agent:{consumer}",
        "hop_depth":    hop_depth,
        "gt_source":    gt_source,
    }


class TestComputeCalibration:

    def test_empty_rows_returns_empty_result(self):
        result = compute_calibration([])
        assert result.total_samples == 0

    def test_unknown_rows_excluded(self):
        rows = [_resolved_row("svc-a", 0.9, "COMPATIBLE", "UNKNOWN")]
        result = compute_calibration(rows)
        assert result.total_samples == 0

    def test_perfect_calibration_all_correct(self):
        rows = [
            _resolved_row("svc-a", 0.95, "COMPATIBLE", "COMPATIBLE"),
            _resolved_row("svc-b", 0.90, "COMPATIBLE", "COMPATIBLE"),
        ]
        result = compute_calibration(rows)
        assert result.total_samples == 2
        assert result.accuracy == pytest.approx(1.0)

    def test_all_wrong_zero_accuracy(self):
        rows = [
            _resolved_row("svc-a", 0.9, "COMPATIBLE", "BREAKING"),
            _resolved_row("svc-b", 0.8, "COMPATIBLE", "BREAKING"),
        ]
        result = compute_calibration(rows)
        assert result.accuracy == pytest.approx(0.0)

    def test_ece_perfect_calibration_is_low(self):
        # When predicted confidence matches accuracy, ECE should be near 0
        rows = [_resolved_row(f"svc-{i}", 0.95, "COMPATIBLE", "COMPATIBLE") for i in range(20)]
        result = compute_calibration(rows)
        assert result.ece < 0.15   # should be close to 0

    def test_per_agent_breakdown(self):
        rows = [
            _resolved_row("svc-a", 0.9, "COMPATIBLE", "COMPATIBLE", agent="agent:svc-a"),
            _resolved_row("svc-b", 0.2, "BREAKING",   "BREAKING",   agent="agent:svc-b"),
        ]
        result = compute_calibration(rows)
        assert "agent:svc-a" in result.per_agent
        assert "agent:svc-b" in result.per_agent

    def test_per_hop_depth_breakdown(self):
        rows = [
            _resolved_row("svc-a", 0.9, "COMPATIBLE", "COMPATIBLE", hop_depth=1),
            _resolved_row("svc-b", 0.7, "COMPATIBLE", "COMPATIBLE", hop_depth=2),
        ]
        result = compute_calibration(rows)
        assert 1 in result.per_hop_depth
        assert 2 in result.per_hop_depth

    def test_bin_counts_sum_to_total(self):
        rows = [_resolved_row(f"svc-{i}", i / 20, "COMPATIBLE", "COMPATIBLE") for i in range(20)]
        result = compute_calibration(rows, n_bins=5)
        assert sum(result.bin_counts) == result.total_samples


# ── _is_correct ───────────────────────────────────────────────────────────────

class TestIsCorrect:

    def test_compatible_matches_compatible(self):
        assert _is_correct("COMPATIBLE", "COMPATIBLE") is True

    def test_breaking_matches_breaking(self):
        assert _is_correct("BREAKING", "BREAKING") is True

    def test_compatible_vs_breaking_is_wrong(self):
        assert _is_correct("COMPATIBLE", "BREAKING") is False

    def test_breaking_vs_compatible_is_wrong(self):
        assert _is_correct("BREAKING", "COMPATIBLE") is False

    def test_uncertain_vs_breaking_is_correct(self):
        # UNCERTAIN when ground truth is BREAKING = appropriate caution
        assert _is_correct("UNCERTAIN", "BREAKING") is True

    def test_uncertain_vs_compatible_is_wrong(self):
        # UNCERTAIN when ground truth is COMPATIBLE = unnecessary noise
        assert _is_correct("UNCERTAIN", "COMPATIBLE") is False
