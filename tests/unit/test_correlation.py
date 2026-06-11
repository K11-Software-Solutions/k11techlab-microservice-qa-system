# -*- coding: utf-8 -*-
"""
Unit tests for Feature 7 — Correlation-Aware Uncertainty Aggregation.

Covers:
  - AgentCorrelationMatrix.effective_n(): independent, correlated, partial
  - AgentCorrelationMatrix.precision_weights(): sum-to-1, non-negative,
    uniform when independent, down-weights correlated pair
  - AgentCorrelationMatrix.adjusted_mean(): arithmetic identity, shifts toward
    independent agent, blends unknown agents
  - AgentCorrelationMatrix.from_store(): cold start, incomplete runs, built correctly
  - AgentCorrelationMatrix.to_dict(): keys, no numpy types
  - aggregate_agent_confidence() with corr_matrix: passthrough and active paths
"""
import numpy as np
import pytest
import pytest_asyncio

from calibration.correlation import AgentCorrelationMatrix, MIN_RUNS, REGULARIZATION
from calibration.store import CalibrationStore
from pipeline.confidence import aggregate_agent_confidence


# ── Helpers ───────────────────────────────────────────────────────────────────

def _identity_matrix(n: int) -> AgentCorrelationMatrix:
    """N perfectly independent agents — correlation matrix = identity."""
    agents = [f"agent-{i}" for i in range(n)]
    corr   = np.eye(n)
    cov    = np.eye(n) * 0.01    # small, equal variances
    return AgentCorrelationMatrix(agents=agents, corr=corr, cov=cov, n_runs=50)


def _perfectly_correlated_matrix(n: int) -> AgentCorrelationMatrix:
    """N agents perfectly correlated — all eigenvalues concentrate on one."""
    agents = [f"agent-{i}" for i in range(n)]
    corr   = np.ones((n, n))
    # For test: cov = σ²·J where J = all-ones. Inversion is ill-conditioned
    # (intended) — precision_weights must handle via regularisation.
    cov    = np.ones((n, n)) * 0.04
    return AgentCorrelationMatrix(agents=agents, corr=corr, cov=cov, n_runs=50)


def _two_correlated_one_independent() -> AgentCorrelationMatrix:
    """payment+order highly correlated (r=0.9), notification independent."""
    agents = ["agent:notification", "agent:order", "agent:payment"]
    corr   = np.array([
        [1.00, 0.13, 0.15],
        [0.13, 1.00, 0.91],
        [0.15, 0.91, 1.00],
    ])
    # Variances: notification=0.01, order=0.04, payment=0.04
    std = np.array([0.1, 0.2, 0.2])
    cov = corr * np.outer(std, std)
    return AgentCorrelationMatrix(agents=agents, corr=corr, cov=cov, n_runs=50)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = str(tmp_path / "corr.db")
    async with CalibrationStore(db) as s:
        yield s


def _compliance(consumer, verdict, confidence):
    return {"consumer": consumer, "verdict": verdict, "confidence": confidence}


async def _seed_resolved_run(store, run_id, agent_confs: dict[str, float]):
    """Insert one run with multiple agents, resolve all ground truth immediately."""
    results = [_compliance(c, "BREAKING", conf) for c, conf in agent_confs.items()]
    scores  = {f"contract_compliance_agent:{c}": conf for c, conf in agent_confs.items()}
    await store.record_run(
        run_id=run_id, repo_name="org/p", pr_number=1,
        compliance_results=results,
        agent_confidence_scores=scores,
    )
    for consumer in agent_confs:
        await store.resolve_ground_truth(run_id, consumer, "BREAKING", "controlled")


# ── effective_n ───────────────────────────────────────────────────────────────

class TestEffectiveN:

    def test_fully_independent_returns_n(self):
        matrix = _identity_matrix(4)
        assert matrix.effective_n() == pytest.approx(4.0, abs=0.01)

    def test_perfectly_correlated_returns_one(self):
        # All eigenvalues of all-ones matrix: one = N, rest = 0 → n_eff = 1
        matrix = _perfectly_correlated_matrix(3)
        assert matrix.effective_n() == pytest.approx(1.0, abs=0.01)

    def test_partial_correlation_between_one_and_n(self):
        matrix = _two_correlated_one_independent()
        n_eff  = matrix.effective_n()
        assert 1.0 < n_eff < 3.0

    def test_partial_correlation_meaningfully_above_one(self):
        # One correlated pair (r=0.9) among 3 agents: n_eff ≈ 1.9
        # — clearly above 1 (not fully correlated) but below 3 (not fully independent)
        matrix = _two_correlated_one_independent()
        assert matrix.effective_n() > 1.5

    def test_single_agent_returns_one(self):
        matrix = _identity_matrix(1)
        assert matrix.effective_n() == pytest.approx(1.0, abs=0.01)


# ── precision_weights ─────────────────────────────────────────────────────────

class TestPrecisionWeights:

    def test_weights_sum_to_one(self):
        matrix  = _two_correlated_one_independent()
        weights = matrix.precision_weights(matrix.agents)
        assert weights.sum() == pytest.approx(1.0, abs=1e-6)

    def test_weights_all_non_negative(self):
        matrix  = _two_correlated_one_independent()
        weights = matrix.precision_weights(matrix.agents)
        assert all(w >= 0.0 for w in weights)

    def test_independent_agents_get_uniform_weights(self):
        matrix  = _identity_matrix(3)
        weights = matrix.precision_weights(matrix.agents)
        # With Σ = σ²I, precision = (1/σ²)I, raw_w = [1/σ², 1/σ², 1/σ²] → uniform
        assert weights == pytest.approx([1/3, 1/3, 1/3], abs=0.02)

    def test_correlated_pair_gets_lower_weight(self):
        matrix = _two_correlated_one_independent()
        # agents: [notification, order, payment]; order+payment are correlated
        weights = matrix.precision_weights(matrix.agents)
        agent_idx = {a: i for i, a in enumerate(matrix.agents)}
        w_notif   = weights[agent_idx["agent:notification"]]
        w_order   = weights[agent_idx["agent:order"]]
        w_payment = weights[agent_idx["agent:payment"]]
        # notification should get more weight than either correlated agent
        assert w_notif > w_order
        assert w_notif > w_payment

    def test_single_agent_returns_weight_one(self):
        matrix  = _identity_matrix(3)
        weights = matrix.precision_weights(["agent-0"])
        assert weights[0] == pytest.approx(1.0)

    def test_fallback_to_uniform_on_singular(self):
        # perfectly correlated → almost singular → precision_weights falls back to uniform
        matrix  = _perfectly_correlated_matrix(3)
        weights = matrix.precision_weights(matrix.agents)
        assert weights.sum() == pytest.approx(1.0, abs=1e-6)
        assert all(w >= 0.0 for w in weights)

    def test_weights_cached_on_second_call(self):
        matrix = _two_correlated_one_independent()
        w1 = matrix.precision_weights(matrix.agents)
        w2 = matrix.precision_weights(matrix.agents)
        assert np.array_equal(w1, w2)
        # And the cache entry exists
        assert tuple(sorted(matrix.agents)) in matrix._weights_cache


# ── adjusted_mean ─────────────────────────────────────────────────────────────

class TestAdjustedMean:

    def test_independent_agents_equals_arithmetic_mean(self):
        matrix = _identity_matrix(3)
        scores = {"agent-0": 0.6, "agent-1": 0.7, "agent-2": 0.8}
        adj    = matrix.adjusted_mean(scores)
        # Uniform weights → same as arithmetic mean
        assert adj == pytest.approx(0.7, abs=0.02)

    def test_correlated_pair_shifts_toward_independent(self):
        """
        notification is independent, order+payment are correlated (r=0.9).
        When notification = 0.3 (low confidence) and order+payment = 0.9 (high),
        the adjusted mean should be pulled toward notification more than arithmetic.
        """
        matrix = _two_correlated_one_independent()
        scores = {
            "agent:notification": 0.3,
            "agent:order":        0.9,
            "agent:payment":      0.9,
        }
        arith = (0.3 + 0.9 + 0.9) / 3          # 0.700
        adj   = matrix.adjusted_mean(scores)
        # notification is up-weighted → adjusted mean < arithmetic mean
        assert adj < arith

    def test_unknown_agents_blended_uniformly(self):
        matrix = _two_correlated_one_independent()
        scores = {
            "agent:notification": 0.5,
            "agent:order":        0.5,
            "agent:payment":      0.5,
            "agent:new-service":  0.5,   # not in the matrix
        }
        adj = matrix.adjusted_mean(scores)
        # All same scores → result should still be 0.5 regardless of weights
        assert adj == pytest.approx(0.5, abs=0.02)

    def test_empty_scores_returns_zero(self):
        matrix = _identity_matrix(3)
        assert matrix.adjusted_mean({}) == 0.0

    def test_single_score_returns_that_score(self):
        matrix = _identity_matrix(3)
        assert matrix.adjusted_mean({"agent-0": 0.77}) == pytest.approx(0.77, abs=0.01)

    def test_result_within_unit_interval(self):
        matrix = _two_correlated_one_independent()
        scores = {"agent:notification": 0.95, "agent:order": 0.02, "agent:payment": 0.98}
        adj = matrix.adjusted_mean(scores)
        assert 0.0 <= adj <= 1.0


# ── from_store ────────────────────────────────────────────────────────────────

class TestFromStore:

    @pytest.mark.asyncio
    async def test_returns_none_below_min_runs(self, store):
        # Seed fewer than MIN_RUNS complete runs
        for i in range(MIN_RUNS - 1):
            await _seed_resolved_run(store, f"run-{i}", {"svc-a": 0.8, "svc-b": 0.6})
        matrix = await AgentCorrelationMatrix.from_store(store)
        assert matrix is None

    @pytest.mark.asyncio
    async def test_returns_none_for_incomplete_runs(self, store):
        # MIN_RUNS rows but some runs are missing one agent → no complete runs
        for i in range(MIN_RUNS):
            consumer = "svc-a" if i % 2 == 0 else "svc-b"
            await _seed_resolved_run(store, f"run-{i}", {consumer: 0.8})
        matrix = await AgentCorrelationMatrix.from_store(store)
        assert matrix is None

    @pytest.mark.asyncio
    async def test_builds_matrix_when_sufficient(self, store):
        for i in range(MIN_RUNS):
            await _seed_resolved_run(store, f"run-{i}", {
                "svc-a": 0.7 + 0.02 * i,
                "svc-b": 0.6 + 0.02 * i,
            })
        matrix = await AgentCorrelationMatrix.from_store(store)
        assert matrix is not None
        assert matrix.n_runs == MIN_RUNS
        assert len(matrix.agents) == 2

    @pytest.mark.asyncio
    async def test_unresolved_runs_excluded(self, store):
        # Resolved runs are below MIN but total (including unresolved) would exceed it
        for i in range(MIN_RUNS - 1):
            await _seed_resolved_run(store, f"run-r{i}", {"svc-a": 0.8, "svc-b": 0.6})
        # Add more unresolved runs (no resolve_ground_truth call)
        from calibration.store import CalibrationStore
        for i in range(5):
            await store.record_run(
                run_id=f"run-u{i}", repo_name="org/p", pr_number=i,
                compliance_results=[
                    _compliance("svc-a", "BREAKING", 0.8),
                    _compliance("svc-b", "BREAKING", 0.6),
                ],
                agent_confidence_scores={
                    "contract_compliance_agent:svc-a": 0.8,
                    "contract_compliance_agent:svc-b": 0.6,
                },
            )
        matrix = await AgentCorrelationMatrix.from_store(store)
        assert matrix is None   # unresolved rows don't count


# ── to_dict ───────────────────────────────────────────────────────────────────

class TestToDict:

    def test_has_required_keys(self):
        matrix = _two_correlated_one_independent()
        d = matrix.to_dict()
        assert "n_agents" in d
        assert "n_runs" in d
        assert "effective_n" in d
        assert "agents" in d
        assert "correlation_matrix" in d
        assert "precision_weights" in d

    def test_no_numpy_types(self):
        """All values must be JSON-serialisable Python built-ins."""
        import json
        matrix = _two_correlated_one_independent()
        d = matrix.to_dict()
        json.dumps(d)   # raises if numpy scalars sneak through

    def test_n_agents_matches_agent_count(self):
        matrix = _two_correlated_one_independent()
        d = matrix.to_dict()
        assert d["n_agents"] == len(matrix.agents)

    def test_effective_n_present_and_correct_range(self):
        matrix = _two_correlated_one_independent()
        d = matrix.to_dict()
        assert 1.0 < d["effective_n"] < len(matrix.agents)

    def test_precision_weights_sum_to_one_in_dict(self):
        matrix = _two_correlated_one_independent()
        d = matrix.to_dict()
        total = sum(d["precision_weights"].values())
        assert total == pytest.approx(1.0, abs=1e-4)


# ── aggregate_agent_confidence with corr_matrix ───────────────────────────────

class TestAggregateWithCorrMatrix:

    def test_passthrough_when_no_matrix(self):
        scores = {"agent-a": 0.4, "agent-b": 0.4, "agent-c": 0.4}
        result = aggregate_agent_confidence(scores, corr_matrix=None)
        assert result.correlation_adjusted is False
        assert result.effective_n is None
        # uncertainty_score same as pre-F7 formula
        expected_score = round((1 - 0.4) * 0.7 + 0.0 * 0.3, 4)
        assert result.uncertainty_score == pytest.approx(expected_score, abs=1e-4)

    def test_corr_matrix_changes_uncertainty_score(self):
        """With a correlated matrix the adjusted mean differs from arithmetic mean."""
        matrix = _two_correlated_one_independent()
        # notification=0.3 (low), order+payment=0.9 (high, correlated)
        scores = {
            "agent:notification": 0.3,
            "agent:order":        0.9,
            "agent:payment":      0.9,
        }
        result_raw  = aggregate_agent_confidence(scores, corr_matrix=None)
        result_corr = aggregate_agent_confidence(scores, corr_matrix=matrix)

        # Precision weighting up-weights notification → adjusted_mean lower than raw
        assert result_corr.uncertainty_score > result_raw.uncertainty_score
        assert result_corr.correlation_adjusted is True

    def test_effective_n_none_without_matrix(self):
        result = aggregate_agent_confidence({"a": 0.8, "b": 0.7}, corr_matrix=None)
        assert result.effective_n is None

    def test_effective_n_populated_with_matrix(self):
        matrix = _two_correlated_one_independent()
        scores = {"agent:notification": 0.8, "agent:order": 0.7, "agent:payment": 0.75}
        result = aggregate_agent_confidence(scores, corr_matrix=matrix)
        assert result.effective_n is not None
        assert 1.0 < result.effective_n < 3.0

    def test_raw_mean_always_stored(self):
        """ConfidenceSummary.mean is always the arithmetic mean (for audit trail)."""
        matrix = _two_correlated_one_independent()
        scores = {"agent:notification": 0.3, "agent:order": 0.9, "agent:payment": 0.9}
        result = aggregate_agent_confidence(scores, corr_matrix=matrix)
        expected_raw = (0.3 + 0.9 + 0.9) / 3
        assert result.mean == pytest.approx(expected_raw, abs=0.01)

    def test_single_agent_no_adjustment(self):
        """Precision weighting requires >= 2 agents; single agent must be identity."""
        matrix = _two_correlated_one_independent()
        scores = {"agent:notification": 0.4}
        result = aggregate_agent_confidence(scores, corr_matrix=matrix)
        # Result should equal raw formula (no adjustment with single agent)
        expected = round((1 - 0.4) * 0.7 + 0.0 * 0.3, 4)
        assert result.uncertainty_score == pytest.approx(expected, abs=1e-4)
