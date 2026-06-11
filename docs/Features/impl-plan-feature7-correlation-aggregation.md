# Implementation Plan: Feature 7 — Correlation-Aware Uncertainty Aggregation
**Branch:** `feature/correlation-aware-aggregation`
**Depends on:** Feature 1 (agent_confidence_scores, aggregate_agent_confidence), calibration study infrastructure (calibration/store.py, calibration_log table)

---

## Overview

The current aggregation formula (`uncertainty_score = (1 − mean) × 0.7 + variance × 0.3`)
assumes all consumer compliance agents are independent. They are not — every agent
sees the same PR diff, so a confusing change makes multiple agents uncertain at once.

Identical confidence vectors on a single run cannot distinguish "3 independent agents
agree it's uncertain" from "1 signal observed 3 correlated times." The first is strong
evidence; the second is redundant. Systematically conflating the two inflates the
information content of the uncertainty estimate (phantom precision).

**Fix:** Replace the arithmetic mean with the Gauss-Markov minimum-variance
precision-weighted mean, estimated from the historical inter-agent covariance matrix.

---

## Design Decisions

### Why precision weighting, not simple de-correlation?

Whitening the confidence vectors (multiplying by Σ⁻¹/²) would fully remove
correlation, but the resulting scores would no longer be confidence values in
[0,1] and would be uninterpretable to a human reviewer. Precision-weighted
*aggregation* keeps the output as a scalar mean in [0,1] while still giving
less weight to redundant agents — interpretable at every step.

### Why only the mean, not also the variance term?

The variance term captures *within-run* disagreement (one agent says 0.9,
another 0.3). This is orthogonal to the cross-run correlation structure and
should not be touched. Within-run disagreement is informative regardless of
whether agents are correlated across runs.

### Why Tikhonov (ridge) regularisation?

With N = 3 agents and 15 runs, the sample covariance matrix is low-rank and
its inverse is numerically unstable. Adding λI (default λ = 0.10) shrinks
precision weights toward uniform 1/N as the noise floor. The regularisation
amount is tunable via `CORR_REGULARIZATION`; tighten it (lower λ) as more
historical data accumulates.

### Cold-start safety

`AgentCorrelationMatrix.from_store()` returns `None` until `CORR_MIN_RUNS`
(default 10) complete runs are resolved. The orchestrator handles `None` by
calling `aggregate_agent_confidence(scores, corr_matrix=None)` which falls
through to the pre-Feature-7 formula. No configuration change needed; the
transition is automatic.

### Negative precision weights

The raw precision-weight vector (`Σ_reg⁻¹ 1`) can contain negative entries for
highly correlated sub-groups. Negative weights would mean "bet against that
agent" — theoretically valid for anti-correlated agents but unintuitive and
risky in a safety context. We clip to 0 and renormalise. This loses the BLUE
property but produces a valid convex combination that cannot produce an
aggregated mean outside the range of the input scores.

---

## Files to Change

| File | Change Type |
|------|-------------|
| `calibration/correlation.py` | **New file** — `AgentCorrelationMatrix` class |
| `calibration/store.py` | Add `get_agent_scores_matrix()` method |
| `pipeline/confidence.py` | Extend `ConfidenceSummary`; add `corr_matrix` param to `aggregate_agent_confidence()` |
| `pipeline/state.py` | Add `effective_n_agents: Optional[float]`; update `initial_state` |
| `pipeline/orchestrator.py` | `aggregate_confidence_node` loads matrix, passes to aggregation, emits field |
| `api/webhook.py` | `GET /calibration/agent-correlations`; add `effective_n_agents` to run response |
| `requirements.txt` | `numpy>=1.26.0`, `scikit-learn>=1.4.0` explicit |
| `tests/unit/test_correlation.py` | **New** |

---

## Step 1 — Store Query (calibration/store.py)

Pivot `calibration_log` from rows to a runs × agents structure. Only include
resolved rows (ground_truth resolved) so noise from unvalidated runs doesn't
corrupt the estimate.

```python
async def get_agent_scores_matrix(self) -> list[dict]:
    async with self._conn.execute(
        """SELECT run_id, agent, confidence
           FROM calibration_log
           WHERE ground_truth != 'UNKNOWN' AND gt_source != 'pending'
           ORDER BY run_id, agent"""
    ) as cursor:
        rows = await cursor.fetchall()

    runs: dict[str, dict[str, float]] = {}
    for row in rows:
        runs.setdefault(row["run_id"], {})[row["agent"]] = float(row["confidence"])

    return [
        {"run_id": run_id, "agent_scores": scores}
        for run_id, scores in runs.items()
    ]
```

---

## Step 2 — AgentCorrelationMatrix (calibration/correlation.py)

### Construction

```python
@classmethod
async def from_store(cls, store, min_runs=MIN_RUNS):
    rows = await store.get_agent_scores_matrix()
    if len(rows) < min_runs:
        return None                  # cold start — caller uses uniform weights

    agents  = sorted({a for row in rows for a in row["agent_scores"]})
    complete = [r for r in rows if all(a in r["agent_scores"] for a in agents)]

    if len(complete) < min_runs:
        return None

    X    = np.array([[row["agent_scores"][a] for a in agents] for row in complete])
    corr = np.corrcoef(X, rowvar=False)   # Pearson correlation matrix (N×N)
    cov  = np.cov(X, rowvar=False)        # sample covariance matrix (N×N)

    return cls(agents=agents, corr=corr, cov=cov, n_runs=len(complete))
```

**Complete-case restriction:** Only runs where every known agent reported a score
are used. Partial runs introduce missing-data bias in the covariance estimate.
A future improvement could use EM-based covariance estimation (e.g., `sklearn.impute`)
for runs where some agents did not fire (e.g., endpoint was not consumed by all).

### Effective N

```python
def effective_n(self) -> float:
    eigenvalues = np.linalg.eigvalsh(self.corr)
    eigenvalues = np.maximum(eigenvalues, 0.0)    # clip numerical negatives
    sum_sq = float(np.sum(eigenvalues ** 2))
    if sum_sq == 0.0:
        return float(len(self.agents))
    return float(np.sum(eigenvalues) ** 2 / sum_sq)
```

Uses `eigvalsh` (symmetric eigensolver) rather than `eigvals` — faster and
guaranteed real eigenvalues for symmetric positive-semidefinite matrices.

### Precision weights

```python
def precision_weights(self, present_agents: list[str]) -> np.ndarray:
    idx     = [self.agents.index(a) for a in present_agents if a in self.agents]
    cov_sub = self.cov[np.ix_(idx, idx)]
    cov_reg = cov_sub + REGULARIZATION * np.eye(len(idx))

    try:
        cov_inv = np.linalg.inv(cov_reg)
    except np.linalg.LinAlgError:
        return np.ones(len(idx)) / len(idx)   # fallback: uniform

    ones  = np.ones(len(idx))
    raw_w = cov_inv @ ones
    raw_w = np.maximum(raw_w, 0.0)             # clip negatives
    total = raw_w.sum()
    return raw_w / total if total > 1e-12 else np.ones(len(idx)) / len(idx)
```

Results are cached by the sorted-agent-tuple key so repeated calls during a
single aggregation are O(1).

### adjusted_mean

```python
def adjusted_mean(self, scores: dict[str, float]) -> float:
    overlap     = [a for a in self.agents if a in scores]
    non_overlap = [a for a in scores if a not in self.agents]

    if len(overlap) < 2:
        return statistics.mean(scores.values())   # fallback: not enough overlap

    weights    = self.precision_weights(overlap)
    known_wmean = float(np.dot(weights, [scores[a] for a in overlap]))

    if not non_overlap:
        return known_wmean

    # Blend known (precision-weighted) + unknown (uniform) subsets by size
    n        = len(overlap) + len(non_overlap)
    unk_mean = statistics.mean(scores[a] for a in non_overlap)
    return (len(overlap) / n) * known_wmean + (len(non_overlap) / n) * unk_mean
```

The blending handles the case where a new consumer appears that was not in the
historical correlation matrix (e.g., a newly registered service). It receives
equal weight rather than being excluded.

---

## Step 3 — ConfidenceSummary Extension (pipeline/confidence.py)

Add two new fields with defaults (backwards-compatible):

```python
@dataclass
class ConfidenceSummary:
    mean: float
    minimum: float
    variance: float
    uncertainty_score: float
    verdict: str
    effective_n: Optional[float] = field(default=None)       # None when corr unavailable
    correlation_adjusted: bool   = field(default=False)      # True when precision weighting used
```

Update `aggregate_agent_confidence()` signature:

```python
def aggregate_agent_confidence(
    scores: dict[str, float],
    corr_matrix: "AgentCorrelationMatrix | None" = None,
) -> ConfidenceSummary:
    ...
    raw_mean = statistics.mean(vals)

    if corr_matrix is not None and len(vals) >= 2:
        effective_mean       = corr_matrix.adjusted_mean(scores)
        effective_n          = corr_matrix.effective_n()
        correlation_adjusted = True
    else:
        effective_mean       = raw_mean
        effective_n          = None
        correlation_adjusted = False

    uncertainty_score = round((1 - effective_mean) * 0.7 + variance * 0.3, 4)
    ...
    return ConfidenceSummary(
        mean=round(raw_mean, 4),          # raw mean for audit trail
        ...
        effective_n=round(effective_n, 3) if effective_n is not None else None,
        correlation_adjusted=correlation_adjusted,
    )
```

`raw_mean` is always stored on the dataclass so operators can compare the raw
vs. adjusted means and verify the correction direction.

---

## Step 4 — State Field (pipeline/state.py)

```python
# In MicroservicePipelineState, under confidence propagation:
effective_n_agents: Optional[float]   # Kish n_eff — None before first 10 runs (Feature 7)
```

Add to `initial_state`:

```python
effective_n_agents=None,
```

---

## Step 5 — Orchestrator Integration (pipeline/orchestrator.py)

In `aggregate_confidence_node`, load the matrix before calling aggregation:

```python
# Feature 7: load correlation matrix (no-op cold start when < MIN_RUNS)
corr_matrix = None
if os.getenv("CALIBRATION_ENABLED", "true").lower() != "false":
    try:
        from calibration.correlation import AgentCorrelationMatrix
        from calibration.store import CalibrationStore, CALIBRATION_DB
        async with CalibrationStore(CALIBRATION_DB) as _store:
            corr_matrix = await AgentCorrelationMatrix.from_store(_store)
    except Exception as _exc:
        logger.debug("Correlation matrix unavailable: %s", _exc)

summary = aggregate_agent_confidence(scores, corr_matrix=corr_matrix)
```

Return the new field:

```python
return {
    ...
    "effective_n_agents": summary.effective_n,
}
```

The correlation matrix load adds one async SQLite query per run. This is
acceptable because it executes once per pipeline run (not per consumer), and
the query scans only the resolved subset of `calibration_log` which has an
index on `ground_truth`.

---

## Step 6 — API (api/webhook.py)

### New endpoint

```python
@app.get("/calibration/agent-correlations")
async def agent_correlations():
    from calibration.correlation import AgentCorrelationMatrix, MIN_RUNS
    async with CalibrationStore(CALIBRATION_DB) as store:
        matrix = await AgentCorrelationMatrix.from_store(store)
    if matrix is None:
        return {"status": "insufficient_data", "min_runs": MIN_RUNS, "message": ...}
    return {"status": "ok", **matrix.to_dict()}
```

### Run response update

```python
return {
    ...
    "effective_n_agents": run["state"].get("effective_n_agents"),   # ← new
    ...
}
```

---

## Environment Variables

```
CORR_MIN_RUNS=10          # runs required before matrix activates (cold start threshold)
CORR_REGULARIZATION=0.10  # Tikhonov λ for covariance inversion stability
CALIBRATION_ENABLED=true  # false gates all calibration paths including this one
```

Add to `.env.example`.

---

## Tests (tests/unit/test_correlation.py)

| Test | Covers |
|------|--------|
| `test_effective_n_independent_agents` | Independent agents → n_eff = N |
| `test_effective_n_perfectly_correlated` | Corr=1 → n_eff = 1 |
| `test_effective_n_partially_correlated` | 1 ≤ n_eff ≤ N for real R |
| `test_precision_weights_sum_to_one` | ‖w‖₁ = 1.0 always |
| `test_precision_weights_uniform_when_independent` | All equal when Σ = σ²I |
| `test_precision_weights_downweight_correlated_pair` | High-corr agents get lower weight |
| `test_precision_weights_no_negatives` | All weights ≥ 0 after clipping |
| `test_adjusted_mean_equal_arithmetic_when_independent` | Identity case |
| `test_adjusted_mean_discounts_correlated_pair` | Correlated pair → mean shifts toward independent agent |
| `test_adjusted_mean_unknown_agents_blended` | Agents not in history get uniform weight |
| `test_from_store_returns_none_below_min_runs` | < MIN_RUNS → None |
| `test_from_store_returns_none_incomplete_runs` | Runs missing some agents → None |
| `test_from_store_builds_matrix` | n_runs ≥ MIN_RUNS and complete → matrix |
| `test_aggregate_confidence_passthrough_no_matrix` | corr_matrix=None → unchanged |
| `test_aggregate_confidence_uses_adjusted_mean` | corr_matrix set → different score |
| `test_confidence_summary_effective_n_none_without_matrix` | effective_n=None when no matrix |
| `test_confidence_summary_effective_n_populated` | effective_n set when matrix present |
| `test_to_dict_serialisable` | No numpy types in output |

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `AgentCorrelationMatrix` class + helpers | 2.5 hours |
| `get_agent_scores_matrix()` store query | 30 min |
| `ConfidenceSummary` extension + aggregation update | 45 min |
| State field + initial_state | 15 min |
| Orchestrator wiring | 30 min |
| API endpoint + run response | 30 min |
| Tests (18 cases) | 2.5 hours |
| **Total** | **~7.5 hours** |
