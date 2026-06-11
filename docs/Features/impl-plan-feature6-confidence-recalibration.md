# Implementation Plan: Feature 6 — Adaptive Confidence Recalibration
**Branch:** `feature/confidence-recalibration`
**Depends on:** Feature 1 (agent_confidence_scores in state), Feature 3 (HITL pipeline / hitl.py), calibration study infrastructure (calibration/store.py)

---

## Overview

The calibration study (Section 4) reports ECE = 0.140, with payment-service
overconfident at 0.91–0.93 on ambiguous enum changes. Raw LLM self-reported
confidence is not inherently well-calibrated. Without correction, the
`UNCERTAINTY_THRESHOLD` must be set conservatively to compensate, generating
unnecessary HITL triggers.

Feature 6 closes the feedback loop by:

1. Recording each reviewer's HITL decision as a per-consumer training label
   (`hitl_outcomes` table in `calibration.db`)
2. Periodically fitting per-agent isotonic regression (or Platt scaling)
   models against those accumulated labels
3. Applying the fitted models at inference time so calibrated confidence flows
   into the HITL threshold check

This mirrors Paper 2's Adaptive Threshold Learner: Paper 2 adjusts _when_ the
gate fires (the threshold); Feature 6 adjusts _how reliable_ the signal is
(the score).

---

## Design Decisions

### Label mapping

| `hitl_decision` | Label |
|-----------------|-------|
| `approve` | correct = 1 (BREAKING was appropriate) |
| `reject` | correct = 1 (BREAKING was appropriate; PR must change) |
| `override` | correct = 0 (BREAKING was a false positive) |

`override` is the key signal: it tells the model that a high-confidence BREAKING
verdict was wrong, pulling future predictions downward for that confidence range.

### Per-agent, not global

Calibration models are keyed by `agent = "contract_compliance_agent:{consumer}"`.
Each consumer can have independent systematic bias — payment-service may be
overconfident while notification-svc is well-calibrated. A global model would
wash out per-consumer signal.

### Non-blocking inference

Model loading happens once (`load_all`) before the pipeline runs; the per-call
transform is O(1). If no model exists for an agent, raw confidence passes through
unchanged (identity). The feature degrades gracefully with zero cold-start cost.

### Batch, not online

Fitting runs on demand (`POST /calibration/recalibrate` or CLI) rather than
after every reviewer decision. Isotonic regression is O(n log n) and safe to
re-run on all accumulated labels; online incremental fitting is not needed for
the expected volume (2–5 decisions per week).

### `MIN_SAMPLES_TO_FIT = 5`

Fitting on fewer than 5 labels produces an overfit model. Agents with fewer
labels return `status="skipped"` and keep identity. The constant is in
`calibration/recalibration.py`; increase for high-volume deployments.

---

## Files to Change

| File | Change Type |
|------|-------------|
| `calibration/store.py` | Add `hitl_outcomes` + `calibration_models` tables; add 5 new methods |
| `calibration/recalibration.py` | **New file** — `RecalibrationEngine`, helpers, dataclass |
| `pipeline/hitl.py` | `cross_repo_human_review()` calls `_record_hitl_labels()` after decision |
| `api/webhook.py` | Three new endpoints; `RecalibrateRequest` pydantic model |
| `scripts/recalibrate.py` | **New file** — CLI for fit / list / transform / plot |
| `tests/unit/test_recalibration.py` | **New** |

---

## Step 1 — Database Schema (calibration/store.py)

Add two new tables inside `_create_tables()`:

```python
# HITL outcome labels — one row per (run_id, consumer) reviewer decision
await self._conn.execute("""
    CREATE TABLE IF NOT EXISTS hitl_outcomes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT    NOT NULL,
        consumer        TEXT    NOT NULL,
        hitl_decision   TEXT    NOT NULL,  -- 'approve' | 'override' | 'reject'
        reviewer        TEXT    NOT NULL DEFAULT 'unknown',
        comment         TEXT    NOT NULL DEFAULT '',
        recorded_at     TEXT    NOT NULL,
        UNIQUE(run_id, consumer)
    )
""")
# Serialised per-agent recalibration models (isotonic or Platt)
await self._conn.execute("""
    CREATE TABLE IF NOT EXISTS calibration_models (
        agent           TEXT    NOT NULL,
        model_type      TEXT    NOT NULL,  -- 'isotonic' | 'platt'
        model_data      BLOB    NOT NULL,  -- pickled sklearn object
        n_samples       INTEGER NOT NULL DEFAULT 0,
        before_ece      REAL,
        after_ece       REAL,
        trained_at      TEXT    NOT NULL,
        PRIMARY KEY (agent, model_type)
    )
""")
```

New methods on `CalibrationStore`:

```python
async def record_hitl_outcome(self, run_id, consumer, hitl_decision, reviewer, comment) -> None:
    """INSERT OR REPLACE into hitl_outcomes."""

async def get_hitl_labels(self) -> list[dict]:
    """JOIN hitl_outcomes with calibration_log to return (agent, consumer, confidence, correct)."""

async def save_model(self, agent, model_type, model_data, n_samples, before_ece, after_ece) -> None:
    """INSERT OR REPLACE into calibration_models."""

async def load_model(self, agent, model_type) -> bytes | None:
    """Return pickled bytes or None."""

async def list_models(self) -> list[dict]:
    """Return all rows from calibration_models (without model_data blob)."""
```

Key SQL for `get_hitl_labels` — must JOIN to get confidence from calibration_log:

```sql
SELECT cl.agent, cl.consumer, cl.confidence, cl.verdict,
       ho.hitl_decision,
       CASE ho.hitl_decision WHEN 'override' THEN 0 ELSE 1 END AS correct
FROM hitl_outcomes ho
JOIN calibration_log cl ON cl.run_id = ho.run_id AND cl.consumer = ho.consumer
```

---

## Step 2 — RecalibrationEngine (calibration/recalibration.py)

### Dataclass

```python
@dataclass
class AgentRecalibrationResult:
    agent:      str
    model_type: str
    n_samples:  int
    before_ece: float | None
    after_ece:  float | None
    status:     str   # 'fitted' | 'skipped'
    message:    str = ""
```

### ECE helper

```python
def _ece(confidences: list[float], correct: list[bool], n_bins: int = 10) -> float:
    confs = np.array(confidences)
    hits  = np.array([float(c) for c in correct])
    bins  = np.linspace(0.0, 1.0, n_bins + 1)
    ece, n = 0.0, len(confs)
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (confs >= lo) & (confs < hi)
        if hi == 1.0:
            mask |= confs == 1.0
        if not mask.any():
            continue
        ece += (mask.sum() / n) * abs(confs[mask].mean() - hits[mask].mean())
    return float(ece)
```

### Model fitters

```python
def _fit_isotonic(confidences, correct):
    from sklearn.isotonic import IsotonicRegression
    ir = IsotonicRegression(out_of_bounds="clip", increasing=True)
    ir.fit(confidences, correct)
    return ir

def _fit_platt(confidences, correct):
    from sklearn.linear_model import LogisticRegression
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)
    lr.fit(confidences.reshape(-1, 1), correct)
    return lr
```

`increasing=True` is critical: preserves monotonicity so that a higher raw
confidence never maps to a lower calibrated confidence.

### Engine class

```python
class RecalibrationEngine:
    MIN_SAMPLES = 5

    def __init__(self, model_type: str = "isotonic") -> None:
        self.model_type = model_type
        self._models: dict[str, object] = {}

    def transform(self, agent: str, raw: float) -> float:
        """Apply model; identity if no model stored for agent."""
        model = self._models.get(agent)
        if model is None:
            return raw
        if self.model_type == "isotonic":
            return float(model.predict([raw])[0])
        return float(model.predict_proba([[raw]])[0][1])

    def has_model(self, agent: str) -> bool:
        return agent in self._models

    async def fit_all(self, store) -> list[AgentRecalibrationResult]:
        """Merge GT + HITL labels, group by agent, fit each."""
        # ... gather gt_rows + hitl_rows, merge deduplicated by (run_id, consumer)
        # ... group into agent_samples: dict[str, list[tuple[float, bool]]]
        # ... for each agent call _fit_agent()

    async def _fit_agent(self, store, agent, samples) -> AgentRecalibrationResult:
        if len(samples) < MIN_SAMPLES_TO_FIT:
            return AgentRecalibrationResult(..., status="skipped", ...)
        confs  = np.array([s[0] for s in samples])
        labels = np.array([float(s[1]) for s in samples])
        before_ece = _ece(confs.tolist(), labels.astype(bool).tolist())
        model = _fit_isotonic(confs, labels)  # or _fit_platt
        self._models[agent] = model
        cal_confs = model.predict(confs).tolist()
        after_ece = _ece(cal_confs, labels.astype(bool).tolist())
        await store.save_model(agent, self.model_type, pickle.dumps(model),
                               len(samples), before_ece, after_ece)
        return AgentRecalibrationResult(..., status="fitted", ...)

    async def load_all(self, store) -> int:
        """Deserialise all stored models of self.model_type from the database."""
```

### Merging GT and HITL labels

When both a ground-truth row and a HITL outcome exist for the same
`(run_id, consumer)`, the ground-truth label takes precedence (higher trust).
Merge order: `{**hitl_labels, **gt_labels}` — dict update ensures GT wins.

---

## Step 3 — Label Capture in HITL Pipeline (pipeline/hitl.py)

Add call to `_record_hitl_labels` immediately after the reviewer decision is
received in `cross_repo_human_review()`:

```python
hitl_decision = decision.get("decision", "")
reviewer      = decision.get("reviewer", "unknown")
comment       = decision.get("comment", "")

# Record reviewer decision as calibration labels (Feature 6)
await _record_hitl_labels(state, hitl_decision, reviewer, comment)

return {"hitl_decision": hitl_decision, ...}
```

The helper loops over `compliance_results` and calls `store.record_hitl_outcome()`
per consumer:

```python
async def _record_hitl_labels(state, hitl_decision, reviewer, comment) -> None:
    if not os.getenv("CALIBRATION_ENABLED", "true").lower() != "false":
        return
    run_id = state.get("run_id", "")
    results = state.get("adjusted_compliance_results") or state.get("compliance_results", [])
    async with CalibrationStore(CALIBRATION_DB) as store:
        for result in results:
            consumer = result.get("consumer", "")
            if consumer:
                await store.record_hitl_outcome(
                    run_id=run_id, consumer=consumer,
                    hitl_decision=hitl_decision, reviewer=reviewer, comment=comment,
                )
```

Wrapped in `try/except` — a calibration write failure must never crash the
main pipeline path.

---

## Step 4 — API Endpoints (api/webhook.py)

```python
class RecalibrateRequest(BaseModel):
    model_type: str = "isotonic"   # 'isotonic' | 'platt'
    gt_source:  str | None = None

@app.post("/calibration/recalibrate")
async def calibration_recalibrate(body: RecalibrateRequest = RecalibrateRequest()):
    from calibration.recalibration import RecalibrationEngine
    engine = RecalibrationEngine(model_type=body.model_type)
    async with CalibrationStore(CALIBRATION_DB) as store:
        results = await engine.fit_all(store)
    return {"model_type": body.model_type, "agents": [...]}

@app.get("/calibration/calibrated-confidence")
async def calibration_transform(agent: str, raw: float, model_type: str = "isotonic"):
    engine = RecalibrationEngine(model_type=model_type)
    async with CalibrationStore(CALIBRATION_DB) as store:
        await engine.load_all(store)
    return {"agent": agent, "raw": raw, "calibrated": round(engine.transform(agent, raw), 4),
            "model_loaded": engine.has_model(agent)}

@app.get("/calibration/models")
async def calibration_models():
    async with CalibrationStore(CALIBRATION_DB) as store:
        return {"models": await store.list_models()}
```

---

## Step 5 — CLI (scripts/recalibrate.py)

Four modes via argparse:

| Flag | Behaviour |
|------|-----------|
| _(default)_ | Fit models, print before/after ECE table |
| `--model-type platt` | Use Platt scaling instead of isotonic |
| `--dry-run` | Fit but do NOT persist to database |
| `--list-models` | Print stored models table, exit |
| `--transform 0.92` | Apply stored models to raw value, exit |
| `--agent <substring>` | Filter `--transform` or `--list-models` |
| `--plot <path.png>` | Side-by-side before/after reliability diagram |
| `--gt-source controlled` | Filter training data by gt_source |

The plot function uses `matplotlib` (optional import) to draw per-agent
reliability curves in two panels: raw confidence (before) and calibrated
confidence (after). Falls back gracefully with an install hint if matplotlib
is absent.

---

## Tests (tests/unit/test_recalibration.py)

| Test | Covers |
|------|--------|
| `test_ece_perfect_calibration` | ECE = 0.0 when confidence == accuracy |
| `test_ece_overconfident_agent` | ECE > 0 when confidence > accuracy |
| `test_ece_empty_input` | Returns 0.0, no crash |
| `test_fit_isotonic_monotone` | Output order preserved: transform(0.3) ≤ transform(0.7) |
| `test_fit_platt_returns_probability` | Output ∈ [0, 1] |
| `test_transform_identity_no_model` | Returns raw unchanged when no model |
| `test_transform_applies_model` | Returns different value when model loaded |
| `test_fit_agent_skipped_below_min` | `status="skipped"` when n < 5 |
| `test_fit_agent_fitted_above_min` | `status="fitted"` when n ≥ 5 |
| `test_fit_all_merges_gt_and_hitl` | GT rows override HITL rows for same (run_id, consumer) |
| `test_fit_all_no_data_returns_empty` | Empty store → empty results list |
| `test_load_all_restores_model` | save → load → transform gives same value |
| `test_has_model_false_before_load` | `has_model()` returns False before load |
| `test_has_model_true_after_fit` | `has_model()` returns True after fit |
| `test_is_correct_compatible_gt` | COMPATIBLE/COMPATIBLE → True |
| `test_is_correct_uncertain_breaking_gt` | UNCERTAIN/BREAKING → True (appropriate caution) |
| `test_is_correct_compatible_breaking_gt` | COMPATIBLE/BREAKING → False (false negative) |

---

## Environment Variables

```
CALIBRATION_ENABLED=true    # false disables both label capture and inference transform
CALIBRATION_DB=calibration.db
```

No new env vars needed — Feature 6 reuses the existing `CALIBRATION_ENABLED`
flag to gate both the write path (label capture in hitl.py) and the read path
(transform at inference time).

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `hitl_outcomes` + `calibration_models` schema + store methods | 1.5 hours |
| `RecalibrationEngine` + helpers | 2 hours |
| `_record_hitl_labels()` in hitl.py | 30 min |
| API endpoints (3) + pydantic model | 45 min |
| `scripts/recalibrate.py` CLI + plot | 1.5 hours |
| Tests | 2 hours |
| **Total** | **~8.25 hours** |
