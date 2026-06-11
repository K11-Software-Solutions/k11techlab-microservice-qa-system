# Feature 6 — Adaptive Confidence Recalibration from HITL Feedback

## Overview

Closes the online learning loop between human reviewer decisions and agent
confidence scores. Each time a reviewer approves or overrides a flagged
pipeline run, the decision is recorded as a labelled sample in the calibration
store. Periodically, per-agent **isotonic regression** (or **Platt scaling**)
models are re-fitted against those accumulated labels so that future confidence
scores reflect historical accuracy rather than raw LLM self-assessment.

Feature 6 is Paper 3's symmetrical answer to Paper 2's Adaptive Threshold
Learner: where Paper 2 adjusts the risk _threshold_ from reviewer feedback,
Feature 6 adjusts the _confidence score_ itself.

---

## The Problem

The calibration study (Section 4 of the paper) shows a mean ECE of 0.140 and
a systematic overconfidence on ambiguous changes (s06 enum rename: agent
reports BREAKING at 0.91–0.93 confidence, expected UNCERTAIN). Raw LLM
self-reported confidence is not inherently well-calibrated:

```
Raw agent response: { "verdict": "BREAKING", "confidence": 0.93 }

Reality (after n=30 accumulation):
  BREAKING verdicts at conf=0.90–0.95 are correct ~68% of the time
  → agent is overconfident at this range
```

Without recalibration, the HITL threshold (`UNCERTAINTY_THRESHOLD`) must be
set conservatively to compensate, generating unnecessary human reviews. With
Feature 6, each agent's confidence is corrected _before_ it reaches the
threshold check, reducing false positives over time.

---

## How It Works

### Phase 1 — Label accumulation

Every HITL reviewer decision is written to `hitl_outcomes` in `calibration.db`
(one row per consumer per run):

| `hitl_decision` | Meaning for per-consumer label |
|-----------------|-------------------------------|
| `approve`       | BREAKING verdict was appropriate → **correct = 1** |
| `reject`        | BREAKING verdict was appropriate (PR must change) → **correct = 1** |
| `override`      | BREAKING was a false positive; change is safe → **correct = 0** |

These join with `calibration_log` (which holds the raw confidence score) to
form training pairs `(confidence, correct)` per `(run_id, consumer)`.

Rows from resolved explicit ground truth (`calibration_log.ground_truth !=
'UNKNOWN'`) are also included, deduplicated by `(run_id, consumer)` so that
HITL rows and ground-truth rows are never double-counted.

### Phase 2 — Model fitting

`RecalibrationEngine.fit_all(store)` groups all labelled samples by agent
(`contract_compliance_agent:{consumer}`), then for each agent with ≥ 5 samples:

1. Computes pre-fit ECE (before calibration)
2. Fits the chosen model on `(confidence, correct)` pairs
3. Transforms training confidence scores through the fitted model
4. Computes post-fit ECE (after calibration)
5. Persists the pickled sklearn object to `calibration_models` in `calibration.db`

Two model types are supported:

| Model | Implementation | When to use |
|-------|---------------|-------------|
| **Isotonic regression** (default) | `sklearn.IsotonicRegression(out_of_bounds="clip", increasing=True)` | Small datasets; non-parametric; preserves rank order |
| **Platt scaling** | `sklearn.LogisticRegression(C=1.0, solver="lbfgs")` | Larger datasets; smooth sigmoid; better at interpolation |

Isotonic regression is monotone by construction: a higher raw confidence
always produces a higher (or equal) calibrated confidence. This prevents
counter-intuitive inversions where correcting one range accidentally flips
another.

### Phase 3 — Inference-time transformation

`RecalibrationEngine.transform(agent, raw_confidence)` applies the stored
model at inference time. If no model exists for an agent (identity case),
the raw score passes through unchanged — there is no cold-start regression.

The transform is applied _before_ `UNCERTAINTY_THRESHOLD` is evaluated in
`aggregate_confidence_node`, so the HITL gate benefits from calibrated scores
as soon as any model is fitted.

---

## Flow

```
pipeline run
  └─ ContractComplianceAgent.run()
       └─ raw confidence emitted per consumer

hitl gate fires (uncertainty_score >= UNCERTAINTY_THRESHOLD)
  └─ cross_repo_human_review()
       └─ reviewer approves / rejects / overrides
            └─ _record_hitl_labels()          ← Feature 6 label capture
                 └─ hitl_outcomes INSERT per consumer

                          [batch trigger]
                               ↓
POST /calibration/recalibrate  ← operator or cron job
  └─ RecalibrationEngine.fit_all(store)
       ├─ gt_rows  (ground truth labels, if resolved)
       ├─ hitl_rows (reviewer decision labels)
       └─ per-agent IsotonicRegression fit
            └─ calibration_models UPSERT

                     [next pipeline run]
                               ↓
aggregate_confidence_node
  └─ RecalibrationEngine.transform(agent, raw_confidence)
       └─ calibrated confidence → UNCERTAINTY_THRESHOLD check
```

---

## Design: No Hot-Path Inference Overhead

Model loading happens once at engine construction via `load_all(store)`. The
transformation is a single `model.predict([x])[0]` call (isotonic) or
`model.predict_proba([[x]])[0][1]` (Platt) — both O(log n) or O(1). The
overhead per consumer is negligible relative to the LLM call.

When `CALIBRATION_ENABLED=false`, neither label capture nor transformation
runs — the flag gates both paths.

---

## Convergence Behaviour

Isotonic regression with `increasing=True` and `out_of_bounds="clip"` converges
in two regimes:

- **Overconfident agent** (curve below diagonal): model pulls high-confidence
  bins downward toward observed accuracy. HITL threshold fires more often on
  previously over-confident scores.
- **Underconfident agent** (curve above diagonal): model raises low-confidence
  bins upward. Fewer unnecessary HITL triggers for agents that habitually
  under-report their confidence.

With `MIN_SAMPLES_TO_FIT = 5`, the first meaningful fit requires 5 labelled
HITL decisions for a single agent. In a medium-sized deployment (2–3 HITL
triggers per week), per-agent models stabilise within 2–4 weeks.

---

## Database Schema

### `hitl_outcomes`

```sql
CREATE TABLE hitl_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    consumer        TEXT    NOT NULL,
    hitl_decision   TEXT    NOT NULL,  -- 'approve' | 'override' | 'reject'
    reviewer        TEXT    NOT NULL DEFAULT 'unknown',
    comment         TEXT    NOT NULL DEFAULT '',
    recorded_at     TEXT    NOT NULL,
    UNIQUE(run_id, consumer)
)
```

### `calibration_models`

```sql
CREATE TABLE calibration_models (
    agent           TEXT    NOT NULL,
    model_type      TEXT    NOT NULL,  -- 'isotonic' | 'platt'
    model_data      BLOB    NOT NULL,  -- pickled sklearn estimator
    n_samples       INTEGER NOT NULL DEFAULT 0,
    before_ece      REAL,
    after_ece       REAL,
    trained_at      TEXT    NOT NULL,
    PRIMARY KEY (agent, model_type)
)
```

---

## API Endpoints

### `POST /calibration/recalibrate`

Fit (or re-fit) per-agent models from all accumulated labels. Call this
after collecting a batch of HITL decisions — typically via cron or operator
trigger.

Request body (optional):
```json
{ "model_type": "isotonic", "gt_source": null }
```

Response:
```json
{
  "model_type": "isotonic",
  "agents": [
    {
      "agent":      "contract_compliance_agent:k11-payment-service",
      "status":     "fitted",
      "n_samples":  12,
      "before_ece": 0.2255,
      "after_ece":  0.0831,
      "message":    ""
    },
    {
      "agent":      "contract_compliance_agent:k11-order-service",
      "status":     "skipped",
      "n_samples":  3,
      "before_ece": null,
      "after_ece":  null,
      "message":    "Only 3 samples (min 5)"
    }
  ]
}
```

### `GET /calibration/calibrated-confidence`

Apply the stored model to a single raw confidence value.

```
GET /calibration/calibrated-confidence?agent=contract_compliance_agent:k11-payment-service&raw=0.92
```

Response:
```json
{
  "agent":        "contract_compliance_agent:k11-payment-service",
  "raw":          0.92,
  "calibrated":   0.71,
  "model_type":   "isotonic",
  "model_loaded": true
}
```

### `GET /calibration/models`

List all stored models.

```json
{
  "models": [
    {
      "agent":       "contract_compliance_agent:k11-payment-service",
      "model_type":  "isotonic",
      "n_samples":   12,
      "before_ece":  0.2255,
      "after_ece":   0.0831,
      "trained_at":  "2026-06-10T14:22:00"
    }
  ]
}
```

---

## CLI

```bash
# Fit models from all labelled data, print before/after ECE per agent
python scripts/recalibrate.py

# Use Platt scaling
python scripts/recalibrate.py --model-type platt

# Fit but do not persist (inspect results without side effects)
python scripts/recalibrate.py --dry-run

# List all stored models
python scripts/recalibrate.py --list-models

# Apply stored model to a raw confidence value
python scripts/recalibrate.py --transform 0.92
python scripts/recalibrate.py --transform 0.92 --agent k11-payment-service

# Generate before/after reliability diagram
python scripts/recalibrate.py --plot before_after_calibration.png

# Filter to controlled-evaluation data only
python scripts/recalibrate.py --gt-source controlled --plot calibration_recal.png
```

---

## Files Changed

| File | Change |
|------|--------|
| `calibration/store.py` | Two new tables: `hitl_outcomes`, `calibration_models`; methods: `record_hitl_outcome()`, `get_hitl_labels()`, `save_model()`, `load_model()`, `list_models()` |
| `calibration/recalibration.py` | **New file.** `RecalibrationEngine`, `AgentRecalibrationResult`, `_fit_isotonic()`, `_fit_platt()`, `_ece()`, `_is_correct()` |
| `pipeline/hitl.py` | `cross_repo_human_review()` calls `_record_hitl_labels()` after reviewer decision |
| `api/webhook.py` | Three new endpoints: `POST /calibration/recalibrate`, `GET /calibration/calibrated-confidence`, `GET /calibration/models`; `RecalibrateRequest` pydantic model |
| `scripts/recalibrate.py` | **New file.** CLI for fitting, listing, transforming, plotting |

---

## Configuration

```
CALIBRATION_ENABLED=true   # set to false to disable both label capture and transformation
CALIBRATION_DB=calibration.db
```

`MIN_SAMPLES_TO_FIT = 5` (constant in `calibration/recalibration.py`) — agents
with fewer labelled rows produce a `"skipped"` result and are left at identity.
Increase this constant when deploying against high-volume pipelines to avoid
overfitting on early, unrepresentative labels.

---

## Relationship to Paper 2's Adaptive Threshold Learner

| Dimension | Paper 2 (Adaptive Threshold Learner) | Paper 3 Feature 6 (Confidence Recalibration) |
|-----------|--------------------------------------|----------------------------------------------|
| **What changes** | HITL trigger threshold | Agent confidence score |
| **Signal source** | Reviewer approve/reject decisions | Same — but stored per-consumer in `hitl_outcomes` |
| **Learning method** | Online exponential moving average | Batch isotonic regression / Platt scaling |
| **Trigger** | After every reviewer decision | After collecting a batch (operator or cron) |
| **Granularity** | Per-service or global threshold | Per-agent (per consumer) model |
| **Effect** | Fewer or more HITL triggers globally | Each agent's raw score corrected individually |

The two mechanisms are **complementary, not redundant**: Paper 2 adjusts
_when_ the gate fires; Feature 6 adjusts _how reliable_ the signal that drives
the gate is. Deploying both produces a self-calibrating system where the HITL
threshold adapts to drift and per-agent scores adapt to historical bias.

---

## Paper Section Placement

Feature 6 belongs in **Section 3 — System Design**, under a subsection titled
*"3.6 Adaptive Confidence Recalibration"* or *"3.6 Online Learning Loop"*.
The before/after ECE comparison (generated by `--plot`) should appear as a
figure in Section 4 (Evaluation), alongside the static calibration study
results. The improvement in ECE quantifies the benefit of accumulating HITL
labels over the baseline static evaluation.
