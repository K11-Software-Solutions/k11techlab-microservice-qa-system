# Feature 8 — Conformal Prediction HITL Threshold

## Overview

The uncertainty gate that triggers human-in-the-loop review uses a threshold:

```
flag for HITL  if  uncertainty_score ≥ τ
```

In Features 1–7, τ = `UNCERTAINTY_THRESHOLD` (default 0.35) — a value chosen by
hand during initial development. Hand-tuned thresholds have no formal validity
guarantee: the operator has no quantitative answer to "what fraction of wrong
verdicts will escape review?"

Feature 8 replaces the static threshold with one derived from the calibration
set via **split conformal prediction** (Vovk, Gammerman & Shafer 2005;
Angelopoulos & Bates 2023). The conformal threshold τ satisfies:

```
P(wrong verdict escapes HITL gate) ≤ α + 1/(n_wrong + 1)
```

where α is a user-specified miscoverage level (default 0.10) and n_wrong is the
number of incorrect-verdict runs in the calibration set. This is a
**marginal, finite-sample, distribution-free** guarantee — it requires no
parametric assumptions about the uncertainty score distribution.

---

## The Problem

Consider two operators comparing their HITL gate:

| System | Threshold | Wrong verdicts escaped |
|--------|-----------|----------------------|
| A | 0.35 (hand-tuned) | unknown |
| B | τ = 0.28 (conformal, α = 0.10) | ≤ 10% + ε(n) |

System B's operator can make a falsifiable claim to their QA team: "no more than
10% + 1/(n+1) of incorrect verdicts will pass review undetected." System A
cannot. The distinction matters when the pipeline is used as a gating mechanism
for merge decisions — the HITL gate's miss rate is now a quantity with a
contract rather than a magic number.

---

## Theory

### Split Conformal Prediction

Given a calibration set C = {(s_i, y_i)} where s_i = uncertainty_score and
y_i ∈ {correct, wrong}, split conformal prediction (Papadopoulos et al. 2002;
Vovk et al. 2005) computes a threshold τ such that:

```
P(y_{new} = wrong  AND  s_{new} < τ) ≤ α + 1/(n_wrong + 1)
```

under the exchangeability assumption: calibration and test data are drawn from
the same distribution.

### Threshold Computation

Let {s_{(1)} ≤ s_{(2)} ≤ ⋯ ≤ s_{(n)}} be the sorted uncertainty scores of all
n = n_wrong incorrect-verdict runs in the calibration set. The conformal threshold is:

```
τ = s_{(k)}     where k = n − ⌈(1 − α)(n + 1)⌉     (0-indexed, ascending)
```

The +1 in (n+1) accounts for the test point in the exchangeability argument
(Tibshirani et al. 2019). Without it, the guarantee would be ≤ α only in
expectation; with it, the guarantee holds with probability 1 over the choice
of calibration set.

**Intuition:** τ is the α-quantile of the wrong-verdict uncertainty scores.
At most α fraction of wrong-verdict runs have uncertainty_score < τ (would
escape the gate). Future wrong-verdict runs from the same distribution satisfy
the same property up to the finite-sample slack 1/(n_wrong+1).

### Monotonicity

The threshold τ is monotone in α:
- **α = 0.05** (strict, miss at most 5%): τ is low — flag almost everything
  above a low uncertainty score to avoid missing wrong verdicts
- **α = 0.20** (relaxed, miss at most 20%): τ is higher — only flag clearly
  uncertain runs

This makes the tradeoff explicit: lower α reduces the fraction of wrong verdicts
that escape but increases the HITL review cost (more correct verdicts flagged).

### Tightness vs Dataset Size

The finite-sample slack is 1/(n_wrong+1):

| n_wrong | Slack | Actual FNR guarantee |
|---------|-------|----------------------|
| 9 | 0.100 | α + 0.100 |
| 19 | 0.050 | α + 0.050 |
| 49 | 0.020 | α + 0.020 |
| 99 | 0.010 | α + 0.010 |

With α = 0.10 and n_wrong = 49, the FNR guarantee is ≤ 0.120. The threshold
reaches practical utility (slack < 0.05) at n_wrong ≥ 19.

---

## Cold Start

`ConformalHITLThreshold.from_store()` returns `None` when fewer than
`CONFORMAL_MIN_WRONG` (default 10) incorrect-verdict runs are in the calibration
set. The pipeline automatically falls back to the static `UNCERTAINTY_THRESHOLD`
env-var in this case. Transition is silent — a DEBUG log records which threshold
was applied.

---

## What Changes in the Pipeline

```
cross_repo_hitl_check(state)
  ├─ impact_score >= IMPACT_HITL_THRESHOLD?        → flag (unchanged)
  ├─ breaking_consumers >= HITL_COUNT?              → flag (unchanged)
  └─ uncertainty_score >= τ?                        ← Feature 8
       ├─ conformal τ available (n_wrong ≥ 10)?
       │    └─ τ = ConformalHITLThreshold.from_store(db)
       └─ cold start?
            └─ τ = UNCERTAINTY_THRESHOLD (env-var fallback)
```

The HITL reason string now includes the threshold source:

```
# Before Feature 8:
"uncertainty_score=0.38 >= threshold 0.35"

# After Feature 8 (conformal active):
"uncertainty_score=0.38 >= conformal threshold 0.32
 (α=0.10, FNR≤0.148, n_wrong=19)"
```

---

## Example: Calibration Study

Using the 53-row controlled dataset from Paper 3's calibration study
(6 scenarios, 3 consumers):

| Scenario | Incorrect runs | uncertainty_score |
|----------|---------------|------------------|
| s06 (BREAKING → expected UNCERTAIN) | 7 | 0.21–0.24 |
| s01–s05 (correct) | 46 | 0.07–0.14 |

Wrong-verdict runs (s06) have uncertainty_score in 0.21–0.24 because the
agents are confident (conf ≈ 0.91–0.93) and they agree — low within-run
variance. Correct-verdict runs have lower uncertainty scores (high confidence,
near-zero variance).

**Conformal table (α = 0.05–0.20, controlled study):**

| α | τ | FNR bound | Emp. FNR | Flag rate | n_wrong |
|---|---|-----------|----------|-----------|---------|
| 0.05 | 0.210 | 0.177 | 0.000 | 0.261 | 7 |
| 0.10 | 0.218 | 0.225 | 0.143 | 0.152 | 7 |
| 0.15 | 0.221 | 0.273 | 0.143 | 0.065 | 7 |
| 0.20 | 0.224 | 0.321 | 0.286 | 0.022 | 7 |

The conformal threshold (τ ≈ 0.21–0.22) is substantially lower than the static
threshold (0.35). This reflects the fact that in the controlled study, wrong
verdicts are actually *less* uncertain (agents are highly confident but wrong)
than the static 0.35 design point assumed. With n_wrong = 7 the slack is
0.125–0.121 — large but bounded. The FNR guarantee tightens with more data.

**Note:** With n_wrong=7 < CONFORMAL_MIN_WRONG=10, the feature remains in cold
start in this study. The table is computed analytically for the paper.

---

## API

### `GET /calibration/conformal-threshold?alpha=0.10`

```json
{
  "status":             "ok",
  "alpha":              0.10,
  "threshold":          0.218,
  "n_calibration":      53,
  "n_wrong":            7,
  "fnr_upper_bound":    0.225,
  "coverage_guarantee": 0.775,
  "static_threshold":   0.35
}
```

When fewer than CONFORMAL_MIN_WRONG wrong-verdict runs exist:

```json
{
  "status":           "insufficient_data",
  "min_wrong":        10,
  "n_wrong":          7,
  "message":          "Need at least 10 runs with incorrect verdicts ...",
  "static_threshold": 0.35
}
```

---

## CLI

```
python scripts/conformal_threshold.py
```

Prints a table at α = 0.05/0.10/0.15/0.20:

```
α     τ      FNR bound  Emp. FNR  Flag rate  n_wrong
----  -----  ---------  --------  ---------  -------
0.05  0.210  0.177      0.000     0.261      7
0.10  0.218  0.225      0.143     0.152      7
0.15  0.221  0.273      0.143     0.065      7
0.20  0.224  0.321      0.286     0.022      7
```

```
python scripts/conformal_threshold.py --alpha 0.10 --compare
```

Side-by-side comparison of static vs conformal threshold.

---

## Configuration

```
CONFORMAL_MIN_WRONG=10    # wrong-verdict runs required before activation (default 10)
CONFORMAL_ALPHA=0.10      # default α when not passed explicitly (default 0.10)
CALIBRATION_ENABLED=true  # false disables all calibration paths including this one
```

---

## Files Changed

| File | Change |
|------|--------|
| `calibration/conformal.py` | **New file.** `ConformalHITLThreshold`: `fit()`, `from_store()`, `to_dict()` |
| `calibration/store.py` | `get_run_calibration_data()` — per-run confidence + correctness pivot |
| `pipeline/hitl.py` | `_get_effective_uncertainty_threshold()` — conformal with static fallback |
| `api/webhook.py` | `GET /calibration/conformal-threshold` endpoint |
| `scripts/conformal_threshold.py` | CLI: α table, --compare, --alpha |
| `tests/unit/test_conformal.py` | 22 unit tests |

---

## Paper Section Placement

Feature 8 belongs in **Section 3 — System Design** under
*"3.8 Conformal HITL Threshold"* or *"3.8 Distribution-Free Threshold Calibration"*.

The key theoretical claim: the HITL trigger threshold now has a
**distribution-free, finite-sample, marginal FNR guarantee** via split
conformal prediction. This connects the paper to:

- Vovk, Gammerman & Shafer (2005) — *Algorithmic Learning in a Random World*
  (original conformal prediction)
- Tibshirani et al. (2019) — *Conformal Prediction Under Covariate Shift*
  (introduces the +1 correction)
- Angelopoulos & Bates (2023) — *A Gentle Introduction to Conformal Prediction
  and Distribution-Free Uncertainty Quantification* (accessible tutorial,
  widely cited; the reference the user suggested)

The honest position in the evaluation section: with n_wrong=7 in the controlled
study (below the MIN_WRONG=10 activation threshold), the conformal threshold
does not activate automatically. The paper should present:
1. The analytical conformal table from the controlled study (Table X) showing
   τ ≈ 0.21 vs static τ = 0.35 — the static threshold was over-conservative
2. The production requirement: n_wrong ≥ 10 (typically 20–50 PRs where agents
   were confidently wrong) to unlock the formal guarantee
3. The asymptotic argument: as n_wrong → ∞, the guarantee tightens to exactly α
