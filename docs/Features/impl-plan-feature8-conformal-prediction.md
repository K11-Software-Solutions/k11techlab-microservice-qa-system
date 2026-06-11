# Implementation Plan: Feature 8 — Conformal Prediction HITL Threshold
**Branch:** `feature/conformal-hitl-threshold`
**Depends on:** Feature 1 (uncertainty_score, UNCERTAINTY_THRESHOLD), calibration store (Feature 3/6 infra)

---

## Overview

Replace the hand-tuned `UNCERTAINTY_THRESHOLD` with a threshold τ derived via
split conformal prediction from the calibration set. The guarantee:

```
P(wrong verdict escapes HITL) ≤ α + 1/(n_wrong + 1)
```

Activation is automatic once n_wrong ≥ CONFORMAL_MIN_WRONG (default 10).
Falls back to the static threshold silently. No schema migration required.

---

## Design Decisions

### Why split conformal, not full conformal?
Full (transductive) conformal re-runs the fitting procedure for every test
point, making it O(n) per pipeline run. Split conformal computes τ once from
a held-out calibration set — constant cost at inference time (one async DB
query of ~100 rows). For our use case there is no reason to pay the full
conformal cost.

### Why control FNR, not FPR?
FNR (fraction of wrong verdicts that escape review) is the safety-critical
quantity. FPR (fraction of correct verdicts flagged unnecessarily) is a cost
quantity. The conformal guarantee targets FNR; FPR is observable empirically
but not formally bounded by this approach. An operator who needs both bounded
simultaneously would need a simultaneous-coverage procedure (out of scope).

### Why not use the per-agent confidence directly?
The nonconformity score is the pipeline-level `uncertainty_score` (not per-agent
confidence) because:
1. The HITL trigger compares against `uncertainty_score`, so τ must be in the
   same space
2. `uncertainty_score` aggregates all agents (already handles correlation via
   Feature 7 when available)

### Cold-start threshold
`CONFORMAL_MIN_WRONG=10` gives an FNR slack of 1/11 ≈ 0.091. With α=0.10, the
FNR guarantee would be ≤ 0.191 — twice the target. We require at least 10 wrong
runs before activating because sub-10 slack is wider than the target α. Operators
can lower this via env-var if they're willing to accept a looser guarantee.

### No serialisation — always computed on-demand
Unlike Feature 6 models, the conformal threshold is a single quantile of ~10–50
data points. Computation is negligible (~1ms). Storing it separately would create
a stale-threshold risk (the stored τ would diverge from the current calibration
data). We compute it fresh per pipeline run from a single SQLite scan.

### is_wrong definition
A run is `any_wrong = True` if at least one consumer received a verdict that
doesn't match its resolved ground truth. This is conservative (a run with one
wrong consumer out of three triggers the wrong-verdict count). An alternative
(majority-wrong) would be less conservative but harder to justify statistically.

---

## Files to Change

| File | Change Type |
|------|-------------|
| `calibration/conformal.py` | **New file** — `ConformalHITLThreshold` class |
| `calibration/store.py` | Add `get_run_calibration_data()` method |
| `pipeline/hitl.py` | `_get_effective_uncertainty_threshold()` helper; use in `cross_repo_hitl_check` |
| `api/webhook.py` | `GET /calibration/conformal-threshold` endpoint |
| `scripts/conformal_threshold.py` | **New file** — CLI |
| `tests/unit/test_conformal.py` | **New file** |

---

## Step 1 — Store Query (calibration/store.py)

Extend the calibration store with a per-run correctness pivot. This method is
reused by both conformal (Feature 8) and future analyses.

```python
async def get_run_calibration_data(self) -> list[dict]:
    """
    Returns one dict per resolved run:
      {run_id, agent_scores: {agent: confidence}, any_wrong: bool}
    """
    async with self._conn.execute(
        """SELECT run_id, agent, confidence, verdict, ground_truth
           FROM calibration_log
           WHERE ground_truth != 'UNKNOWN' AND gt_source != 'pending'
           ORDER BY run_id, agent"""
    ) as cursor:
        rows = await cursor.fetchall()

    runs: dict[str, dict] = {}
    for row in rows:
        run_id = row["run_id"]
        if run_id not in runs:
            runs[run_id] = {"agent_scores": {}, "any_wrong": False}
        runs[run_id]["agent_scores"][row["agent"]] = float(row["confidence"])
        if row["verdict"] != row["ground_truth"]:
            runs[run_id]["any_wrong"] = True

    return [
        {"run_id": rid, "agent_scores": d["agent_scores"], "any_wrong": d["any_wrong"]}
        for rid, d in runs.items()
    ]
```

---

## Step 2 — ConformalHITLThreshold (calibration/conformal.py)

Core class. Frozen dataclass — all fields set at construction, no mutation.

### Threshold formula

Given sorted wrong-verdict uncertainty scores s_{(0)} ≤ … ≤ s_{(n-1)}:

```
k = n − ⌈(1 − α)(n + 1)⌉     (0-indexed)
k = clamp(k, 0, n − 1)
τ = s_{(k)}
```

This is the conformal α-quantile corrected for the test point (+1 in denominator).

```python
@classmethod
def fit(cls, pairs, alpha=DEFAULT_ALPHA):
    wrong = sorted(s for s, is_wrong in pairs if is_wrong)
    n = len(wrong)
    if n == 0:
        return cls(alpha=alpha, threshold=0.0, ...)
    k = max(0, min(n - math.ceil((1 - alpha) * (n + 1)), n - 1))
    tau = wrong[k]
    fnr_ub = alpha + 1.0 / (n + 1)
    return cls(alpha=alpha, threshold=round(tau, 4), n_wrong=n,
               fnr_upper_bound=round(fnr_ub, 4), ...)
```

### from_store classmethod

```python
@classmethod
async def from_store(cls, store, alpha=DEFAULT_ALPHA, min_wrong=MIN_WRONG_SAMPLES):
    from pipeline.confidence import aggregate_agent_confidence
    run_data = await store.get_run_calibration_data()
    pairs = [
        (aggregate_agent_confidence(run["agent_scores"]).uncertainty_score, run["any_wrong"])
        for run in run_data
    ]
    n_wrong = sum(1 for _, is_wrong in pairs if is_wrong)
    if n_wrong < min_wrong:
        return None    # cold start
    return cls.fit(pairs, alpha=alpha)
```

---

## Step 3 — Pipeline Integration (pipeline/hitl.py)

Add `_get_effective_uncertainty_threshold()` before `cross_repo_hitl_check`.
This function is the single decision point for which threshold to use.

```python
async def _get_effective_uncertainty_threshold() -> tuple[float, str]:
    if os.getenv("CALIBRATION_ENABLED", "true").lower() == "false":
        return UNCERTAINTY_THRESHOLD, f"static threshold {UNCERTAINTY_THRESHOLD}"
    try:
        from calibration.conformal import ConformalHITLThreshold
        from calibration.store import CalibrationStore, CALIBRATION_DB
        async with CalibrationStore(CALIBRATION_DB) as _store:
            ct = await ConformalHITLThreshold.from_store(_store)
        if ct is not None:
            return ct.threshold, f"conformal threshold {ct.threshold:.3f} ..."
    except Exception as exc:
        logger.debug("Conformal threshold unavailable: %s", exc)
    return UNCERTAINTY_THRESHOLD, f"static threshold {UNCERTAINTY_THRESHOLD}"
```

In `cross_repo_hitl_check`, replace the `elif uncertainty_score >= UNCERTAINTY_THRESHOLD`
branch with:

```python
else:
    unc_threshold, unc_desc = await _get_effective_uncertainty_threshold()
    if uncertainty_score >= unc_threshold:
        hitl_required = True
        hitl_reason = f"... >= {unc_desc} ..."
```

The HITL reason string becomes self-documenting: operators see which threshold
was applied and its guarantee parameters.

---

## Step 4 — API Endpoint (api/webhook.py)

```python
@app.get("/calibration/conformal-threshold")
async def conformal_threshold(alpha: float = 0.10):
    from calibration.conformal import ConformalHITLThreshold, MIN_WRONG_SAMPLES
    from pipeline.confidence import UNCERTAINTY_THRESHOLD, aggregate_agent_confidence

    async with CalibrationStore(CALIBRATION_DB) as store:
        run_data = await store.get_run_calibration_data()

    pairs = [
        (aggregate_agent_confidence(run["agent_scores"]).uncertainty_score, run["any_wrong"])
        for run in run_data
    ]
    n_wrong = sum(1 for _, is_wrong in pairs if is_wrong)

    if n_wrong < MIN_WRONG_SAMPLES:
        return {"status": "insufficient_data", ..., "static_threshold": UNCERTAINTY_THRESHOLD}

    ct = ConformalHITLThreshold.fit(pairs, alpha=alpha)
    return {"status": "ok", "static_threshold": UNCERTAINTY_THRESHOLD, **ct.to_dict()}
```

Alpha is a query parameter so operators can explore the α–τ tradeoff:
`GET /calibration/conformal-threshold?alpha=0.05`

---

## Step 5 — CLI (scripts/conformal_threshold.py)

```
python scripts/conformal_threshold.py
  → table at α = 0.05 / 0.10 / 0.15 / 0.20

python scripts/conformal_threshold.py --alpha 0.10
  → single-row output

python scripts/conformal_threshold.py --alpha 0.10 --compare
  → side-by-side: static threshold vs conformal threshold
  → columns: threshold, empirical FNR, FNR guarantee, flag rate, n
```

The `--compare` mode is the most useful for operators choosing whether to
activate and at what α level.

---

## Environment Variables

```
CONFORMAL_MIN_WRONG=10    # wrong runs needed before activation (default 10)
CONFORMAL_ALPHA=0.10      # default alpha for from_store() (default 0.10)
CALIBRATION_ENABLED=true  # false gates all calibration paths
```

---

## Tests (tests/unit/test_conformal.py)

| Test | Covers |
|------|--------|
| `test_threshold_is_alpha_quantile_of_wrong_scores` | k-formula concrete example |
| `test_higher_alpha_gives_higher_threshold` | monotonicity (higher α → less strict gate) |
| `test_zero_wrong_returns_threshold_zero` | no wrong data → threshold=0 (flag everything) |
| `test_single_wrong_returns_that_score` | edge case n_wrong=1 |
| `test_n_calibration_counts_all_pairs` | n_calibration vs n_wrong distinction |
| `test_fnr_upper_bound_formula` | α + 1/(n+1) |
| `test_coverage_guarantee_complement_of_fnr_bound` | 1 - fnr_ub |
| `test_coverage_guarantee_non_negative` | clamp at 0 |
| `test_threshold_within_unit_interval` | [0,1] sanity check |
| `test_alpha_stored_on_instance` | field persistence |
| `test_empirical_fnr_at_most_alpha_large_n` | guarantee holds on calibration data |
| `test_threshold_zero_means_all_wrong_flagged` | n_wrong=0 safety |
| `test_fnr_tighter_with_more_data` | 1/(n+1) shrinks |
| `test_returns_none_below_min_wrong` | cold start |
| `test_returns_threshold_when_sufficient_wrong` | activation |
| `test_alpha_param_respected` | from_store passes alpha |
| `test_custom_min_wrong` | override min_wrong |
| `test_has_required_keys` | to_dict keys |
| `test_json_serialisable` | no numpy scalars |
| `test_values_match_instance_fields` | to_dict fidelity |
| `test_fallback_to_static_when_insufficient_data` | pipeline fallback path |
| `test_fallback_when_calibration_disabled` | env-var gate |

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `calibration/conformal.py` | 1.5 hours |
| `get_run_calibration_data()` in store | 20 min |
| `hitl.py` integration | 30 min |
| API endpoint | 20 min |
| CLI | 45 min |
| Tests (22 cases) | 1.5 hours |
| Feature doc + impl plan | 1 hour |
| **Total** | **~6 hours** |
