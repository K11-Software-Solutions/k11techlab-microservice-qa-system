# Evaluation — Agent Confidence Calibration Study

## Purpose

Validates the paper's central claim: that agent self-reported confidence scores
are a statistically meaningful signal for triggering human review. A system that
fires HITL on "uncertainty" is only useful if low confidence actually correlates
with incorrect verdicts. This study produces the evidence.

---

## Research Question

> When `ContractComplianceAgent` reports confidence X for a verdict, is the
> verdict correct approximately X fraction of the time?

A well-calibrated agent has a **reliability curve that lies on the diagonal**
(expected confidence = observed accuracy). Systematic deviation reveals whether
the agent is overconfident (curve below diagonal) or underconfident (above).

---

## Methodology

### Data Collection

Run the pipeline against **30–50 real GitHub pull requests** in monitored
microservice repositories. For each PR, the pipeline records one row per
consumer in `calibration.db`:

| Field | Description |
|-------|-------------|
| `confidence` | Agent self-reported confidence [0.0, 1.0] |
| `verdict` | COMPATIBLE \| BREAKING \| UNCERTAIN |
| `hop_depth` | 1 = direct consumer, 2+ = transitive |
| `agent` | `contract_compliance_agent:{consumer}` |
| `repo_name` | Provider repository |
| `pr_number` | GitHub PR number |

### Ground Truth Resolution

Ground truth is collected **after** the provider PR merges via two strategies,
used in combination:

**Primary — CI failure signal (automated):**
- Wait `GT_WINDOW_HOURS` (default 48 h) after provider PR merges
- Query GitHub Actions API for each consumer's default branch
- Label consumer as `BREAKING` if any workflow run failed in the window;
  `COMPATIBLE` otherwise
- Trigger via: `POST /calibration/fetch-ci-ground-truth`

**Secondary — Manual annotation (highest trust):**
- Domain expert reviews the contract diff and consumer usage for each PR
- Labels each consumer verdict as correct or incorrect
- Supply via: `POST /calibration/resolve`
- Used as the authoritative source in paper-reported numbers

Both sources are tracked in the `gt_source` column. Final analysis filters by
`gt_source=manual` for the paper's primary results, with CI-failure results
reported separately as a robustness check.

### Correctness Definition

A verdict is **correct** when:

| Agent verdict | Ground truth | Correct? | Rationale |
|---------------|-------------|----------|-----------|
| COMPATIBLE    | COMPATIBLE  | ✅ Yes   | True negative |
| BREAKING      | BREAKING    | ✅ Yes   | True positive |
| UNCERTAIN     | BREAKING    | ✅ Yes   | Appropriate caution |
| COMPATIBLE    | BREAKING    | ❌ No    | False negative (dangerous) |
| BREAKING      | COMPATIBLE  | ❌ No    | False positive |
| UNCERTAIN     | COMPATIBLE  | ❌ No    | Unnecessary noise |

`UNCERTAIN` vs `BREAKING` counts as correct because it reflects appropriate
epistemic caution — the agent flagged doubt, and the change was indeed
breaking. This is the intended behaviour of Feature 3 (verdict downgrade).

### Calibration Curve Computation

Confidence scores are binned into 10 equal-width intervals [0.0, 0.1),
[0.1, 0.2), …, [0.9, 1.0]. For each non-empty bin:

```
bin_mean_confidence = mean(confidence scores in bin)
bin_accuracy        = fraction correct in bin
```

**Expected Calibration Error (ECE):**

```
ECE = Σ (|bin| / n) × |bin_mean_confidence − bin_accuracy|
```

ECE is the primary scalar metric reported in the paper. Lower is better;
ECE < 0.10 is considered well-calibrated in the literature.

---

## Breakdowns Reported

| Breakdown | Why it matters |
|-----------|---------------|
| Overall | Headline calibration result |
| Per-agent (`contract_compliance_agent:{consumer}`) | Reveals per-consumer variance |
| Direct (hop_depth=1) vs Transitive (hop_depth≥2) | Tests Feature 5 — do transitive consumers have lower calibration because the LLM receives less direct context? |

The direct vs transitive split is a secondary contribution of the study:
if transitive consumers show systematically lower calibration, it validates
the `hop_depth` prompt annotation (Feature 5) as a necessary correction.

---

## Running the Study

### Step 1 — Collect pipeline runs

Deploy the webhook server and monitor real PRs. Calibration logging is
automatic (`CALIBRATION_ENABLED=true`). Check progress:

```
GET /calibration/stats
→ {"total": 87, "resolved": 0, "pending": 87}
```

### Step 2 — Resolve ground truth

**CI-failure (automated, run after 48 h):**
```
POST /calibration/fetch-ci-ground-truth
```

**Manual annotation:**
```
POST /calibration/resolve
{
  "run_id": "abc-123",
  "consumer_verdicts": {
    "k11-payment-service": "BREAKING",
    "k11-order-service": "COMPATIBLE"
  }
}
```

### Step 3 — Generate calibration plot

```bash
# Full plot with CI ground truth fetched first
python scripts/plot_calibration.py --fetch-gt --output calibration.png

# Manual-only (paper's primary result)
python scripts/plot_calibration.py --gt-source manual --output calibration_manual.png

# Stats table only
python scripts/plot_calibration.py --stats-only
```

Output:
```
Calibration store: calibration.db
  Total rows : 142
  Resolved   : 138
  Pending    : 4

Agent                                               n   Accuracy      ECE
──────────────────────────────────────────────────────────────────────────
Overall                                           138      0.847   0.0631
  k11-payment-service                              24      0.833   0.0712
  k11-order-service                                19      0.895   0.0441
  ...
```

---

## Expected Results (Hypothesis)

Based on the design of Features 1–3:

- **Overall ECE < 0.10** — agents are broadly well-calibrated
- **Low-confidence bin (0.0–0.3) accuracy < 0.40** — low confidence predicts
  error; this is the key evidence that the HITL trigger is justified
- **High-confidence bin (0.8–1.0) accuracy > 0.85** — high confidence
  predicts correctness; HITL is not fired unnecessarily
- **Transitive ECE > Direct ECE** — transitive consumers have noisier context,
  so calibration is expected to be worse; the hop_depth prompt annotation
  (Feature 5) should narrow the gap

---

## Files

| File | Role |
|------|------|
| `calibration/store.py` | Async SQLite store; schema; record/resolve/query |
| `calibration/ground_truth.py` | GitHub CI failure signal; manual override |
| `calibration/curves.py` | `compute_calibration()`, `plot_calibration()`, ECE |
| `scripts/plot_calibration.py` | CLI analysis tool |
| `api/webhook.py` | Auto-records after every run; calibration API endpoints |
| `tests/unit/test_calibration_store.py` | 21 unit tests |

---

## Paper Section Placement

This study belongs in **Section 4 — Evaluation**, under a subsection titled
*"4.1 Confidence Calibration"* or *"4.1 Is Agent Confidence a Reliable Signal?"*

The calibration curve figure (output of `plot_calibration.py`) should appear
as a full-width figure immediately after the ECE table. The per-hop-depth
breakdown supports the Feature 5 contribution separately in
*"4.2 Transitive Consumer Validation"*.
