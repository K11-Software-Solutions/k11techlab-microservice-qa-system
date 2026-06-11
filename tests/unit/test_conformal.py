# -*- coding: utf-8 -*-
"""
Unit tests for Feature 8 — Conformal HITL Threshold.

Covers:
  - ConformalHITLThreshold.fit(): quantile formula, zero wrong, single wrong
  - Guarantee tightness: empirical FNR ≤ alpha at calibration points
  - Monotonicity: higher alpha → lower threshold (more lenient gate)
  - ConformalHITLThreshold.from_store(): cold start, activates at min_wrong
  - to_dict(): JSON-safe, correct keys
  - pipeline integration: _get_effective_uncertainty_threshold() falls back
    to static threshold before conformal data available
"""
import math
import pytest
import pytest_asyncio

from calibration.conformal import (
    ConformalHITLThreshold,
    DEFAULT_ALPHA,
    MIN_WRONG_SAMPLES,
)
from calibration.store import CalibrationStore
from pipeline.confidence import UNCERTAINTY_THRESHOLD


# ── Fixtures & helpers ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def store(tmp_path):
    db = str(tmp_path / "conf.db")
    async with CalibrationStore(db) as s:
        yield s


def _pairs_uniform(n_correct: int, n_wrong: int, wrong_score: float = 0.6) -> list:
    """Correct verdicts have low uncertainty; wrong verdicts have fixed uncertainty."""
    pairs = [(0.1, False)] * n_correct + [(wrong_score, True)] * n_wrong
    return pairs


def _pairs_spread(n: int) -> list:
    """n wrong-verdict runs with linearly spread uncertainty scores 0.1..0.9."""
    return [(0.1 + 0.8 * i / (n - 1), True) for i in range(n)]


def _compliance(consumer, verdict, confidence):
    return {"consumer": consumer, "verdict": verdict, "confidence": confidence}


async def _seed_run(store, run_id: str, correct: bool):
    """One consumer run, resolved; correct=True → verdict matches ground truth."""
    verdict = "BREAKING"
    gt = "BREAKING" if correct else "COMPATIBLE"
    confidence = 0.2 if not correct else 0.9  # wrong verdicts have lower uncertainty
    await store.record_run(
        run_id=run_id, repo_name="org/p", pr_number=1,
        compliance_results=[_compliance("svc-a", verdict, confidence)],
        agent_confidence_scores={"contract_compliance_agent:svc-a": confidence},
    )
    await store.resolve_ground_truth(run_id, "svc-a", gt, "controlled")


# ── fit() — quantile formula ──────────────────────────────────────────────────

class TestConformalFit:

    def test_threshold_is_alpha_quantile_of_wrong_scores(self):
        # 10 wrong-verdict runs with scores 0.1,0.2,...,1.0
        pairs = [(0.1 * (i + 1), True) for i in range(10)]
        ct = ConformalHITLThreshold.fit(pairs, alpha=0.10)
        # k = 10 - ceil(0.9 * 11) = 10 - 10 = 0 → τ = 0.1 (smallest)
        assert ct.threshold == pytest.approx(0.1, abs=1e-4)

    def test_higher_alpha_gives_higher_threshold(self):
        pairs = _pairs_spread(20)
        ct_05 = ConformalHITLThreshold.fit(pairs, alpha=0.05)
        ct_20 = ConformalHITLThreshold.fit(pairs, alpha=0.20)
        # Higher alpha (willing to miss more wrong verdicts) → higher threshold
        # (less aggressive flagging): α=0.05 flags everything above ~0.1,
        # α=0.20 only flags above ~0.22
        assert ct_20.threshold > ct_05.threshold

    def test_zero_wrong_returns_threshold_zero(self):
        pairs = [(0.3, False)] * 20
        ct = ConformalHITLThreshold.fit(pairs, alpha=0.10)
        assert ct.threshold == 0.0
        assert ct.n_wrong == 0

    def test_single_wrong_returns_that_score(self):
        pairs = [(0.5, False)] * 10 + [(0.72, True)]
        ct = ConformalHITLThreshold.fit(pairs, alpha=0.10)
        # Only one wrong score, index k = max(0, 1 - ceil(0.9*2)) = max(0,1-2)=0 → τ=0.72
        assert ct.threshold == pytest.approx(0.72, abs=1e-4)

    def test_n_calibration_counts_all_pairs(self):
        pairs = [(0.3, False)] * 15 + [(0.8, True)] * 5
        ct = ConformalHITLThreshold.fit(pairs, alpha=0.10)
        assert ct.n_calibration == 20
        assert ct.n_wrong == 5

    def test_fnr_upper_bound_formula(self):
        pairs = _pairs_uniform(10, 20)
        ct = ConformalHITLThreshold.fit(pairs, alpha=0.10)
        expected_ub = 0.10 + 1 / (20 + 1)
        assert ct.fnr_upper_bound == pytest.approx(expected_ub, abs=1e-4)

    def test_coverage_guarantee_complement_of_fnr_bound(self):
        ct = ConformalHITLThreshold.fit(_pairs_spread(15), alpha=0.10)
        assert ct.coverage_guarantee == pytest.approx(1 - ct.fnr_upper_bound, abs=1e-4)

    def test_coverage_guarantee_non_negative(self):
        # With very few wrong samples the guarantee may be loose but not negative
        pairs = [(0.5, True)] * 3 + [(0.2, False)] * 5
        ct = ConformalHITLThreshold.fit(pairs, alpha=0.10)
        assert ct.coverage_guarantee >= 0.0

    def test_threshold_within_unit_interval(self):
        for alpha in [0.05, 0.10, 0.20]:
            ct = ConformalHITLThreshold.fit(_pairs_spread(30), alpha=alpha)
            assert 0.0 <= ct.threshold <= 1.0

    def test_alpha_stored_on_instance(self):
        ct = ConformalHITLThreshold.fit(_pairs_uniform(5, 10), alpha=0.15)
        assert ct.alpha == pytest.approx(0.15)


# ── Guarantee: empirical FNR ≤ alpha on calibration set ──────────────────────

class TestCoverageGuarantee:

    def test_empirical_fnr_at_most_alpha_large_n(self):
        """With 50 wrong runs the empirical FNR should be ≤ alpha."""
        n = 50
        pairs = [(0.1 + 0.8 * i / (n - 1), True) for i in range(n)]
        for alpha in [0.05, 0.10, 0.20]:
            ct = ConformalHITLThreshold.fit(pairs, alpha=alpha)
            escaped = sum(1 for s, _ in pairs if s < ct.threshold)
            empirical_fnr = escaped / n
            assert empirical_fnr <= alpha + 1e-9, (
                f"α={alpha}: empirical FNR {empirical_fnr:.3f} > alpha"
            )

    def test_threshold_zero_means_all_wrong_flagged(self):
        """If n_wrong=0, τ=0 → all wrong runs flagged (FNR=0)."""
        pairs = [(0.5, False)] * 20
        ct = ConformalHITLThreshold.fit(pairs, alpha=0.10)
        assert ct.threshold == 0.0
        # No wrong runs to miss
        assert ct.n_wrong == 0

    def test_fnr_tighter_with_more_data(self):
        """FNR upper bound decreases as n_wrong increases."""
        ct_small = ConformalHITLThreshold.fit(_pairs_uniform(5, 10), alpha=0.10)
        ct_large = ConformalHITLThreshold.fit(_pairs_uniform(50, 100), alpha=0.10)
        assert ct_large.fnr_upper_bound < ct_small.fnr_upper_bound


# ── from_store ────────────────────────────────────────────────────────────────

class TestFromStore:

    @pytest.mark.asyncio
    async def test_returns_none_below_min_wrong(self, store):
        # Seed fewer wrong runs than MIN_WRONG_SAMPLES
        for i in range(MIN_WRONG_SAMPLES - 1):
            await _seed_run(store, f"run-w{i}", correct=False)
        for i in range(20):
            await _seed_run(store, f"run-c{i}", correct=True)

        ct = await ConformalHITLThreshold.from_store(store)
        assert ct is None

    @pytest.mark.asyncio
    async def test_returns_threshold_when_sufficient_wrong(self, store):
        for i in range(MIN_WRONG_SAMPLES):
            await _seed_run(store, f"run-w{i}", correct=False)
        for i in range(10):
            await _seed_run(store, f"run-c{i}", correct=True)

        ct = await ConformalHITLThreshold.from_store(store)
        assert ct is not None
        assert 0.0 <= ct.threshold <= 1.0

    @pytest.mark.asyncio
    async def test_alpha_param_respected(self, store):
        for i in range(MIN_WRONG_SAMPLES + 5):
            await _seed_run(store, f"run-w{i}", correct=False)

        ct_05 = await ConformalHITLThreshold.from_store(store, alpha=0.05)
        ct_20 = await ConformalHITLThreshold.from_store(store, alpha=0.20)
        assert ct_05 is not None
        assert ct_20 is not None
        assert ct_20.threshold <= ct_05.threshold

    @pytest.mark.asyncio
    async def test_custom_min_wrong(self, store):
        # Seed 3 wrong runs — below default MIN but above min_wrong=2
        for i in range(3):
            await _seed_run(store, f"run-w{i}", correct=False)

        ct_default = await ConformalHITLThreshold.from_store(store)
        assert ct_default is None  # 3 < MIN_WRONG_SAMPLES (default 10)

        ct_custom = await ConformalHITLThreshold.from_store(store, min_wrong=2)
        assert ct_custom is not None


# ── to_dict ───────────────────────────────────────────────────────────────────

class TestToDict:

    def test_has_required_keys(self):
        ct = ConformalHITLThreshold.fit(_pairs_uniform(10, 15), alpha=0.10)
        d = ct.to_dict()
        for key in ("alpha", "threshold", "n_calibration", "n_wrong",
                    "fnr_upper_bound", "coverage_guarantee"):
            assert key in d, f"Missing key: {key}"

    def test_json_serialisable(self):
        import json
        ct = ConformalHITLThreshold.fit(_pairs_spread(20), alpha=0.10)
        json.dumps(ct.to_dict())  # raises if non-serialisable types

    def test_values_match_instance_fields(self):
        ct = ConformalHITLThreshold.fit(_pairs_uniform(10, 12), alpha=0.12)
        d = ct.to_dict()
        assert d["alpha"] == ct.alpha
        assert d["threshold"] == ct.threshold
        assert d["n_wrong"] == ct.n_wrong
        assert d["fnr_upper_bound"] == ct.fnr_upper_bound


# ── pipeline fallback ─────────────────────────────────────────────────────────

class TestPipelineFallback:

    @pytest.mark.asyncio
    async def test_fallback_to_static_threshold_when_insufficient_data(
        self, store, monkeypatch, tmp_path
    ):
        """
        When n_wrong < MIN_WRONG_SAMPLES, _get_effective_uncertainty_threshold
        should return the static UNCERTAINTY_THRESHOLD.
        """
        db_path = str(tmp_path / "empty.db")
        monkeypatch.setenv("CALIBRATION_DB", db_path)
        monkeypatch.setenv("CALIBRATION_ENABLED", "true")

        from pipeline.hitl import _get_effective_uncertainty_threshold
        threshold, desc = await _get_effective_uncertainty_threshold()
        assert threshold == pytest.approx(UNCERTAINTY_THRESHOLD, abs=1e-6)
        assert "static" in desc

    @pytest.mark.asyncio
    async def test_fallback_when_calibration_disabled(self, monkeypatch):
        monkeypatch.setenv("CALIBRATION_ENABLED", "false")
        from pipeline.hitl import _get_effective_uncertainty_threshold
        threshold, desc = await _get_effective_uncertainty_threshold()
        assert threshold == pytest.approx(UNCERTAINTY_THRESHOLD, abs=1e-6)
        assert "static" in desc
