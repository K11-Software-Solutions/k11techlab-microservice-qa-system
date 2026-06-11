# Feature 7 — Correlation-Aware Uncertainty Aggregation

## Overview

The uncertainty aggregation formula introduced in Feature 1 computes:

```
uncertainty_score = (1 − mean_confidence) × 0.7  +  variance × 0.3
```

where `mean_confidence` is the arithmetic mean over all consumer compliance
agents. This formula **assumes the agents are independent**, so N agents
reporting low confidence provides N times the evidence for uncertainty. In
practice the agents are not independent — every agent processes the same PR
diff, so a confusing change makes multiple agents uncertain simultaneously.

Feature 7 replaces the arithmetic mean with a **precision-weighted mean**
derived from the empirical inter-agent covariance structure. Correlated agents
are down-weighted relative to uniform; anti-correlated agents (those that
disagree systematically on different PR types) are up-weighted. The effective
number of independent signals (`n_eff`) is reported alongside the verdict to
make the compression visible.

---

## The Problem

Consider two scenarios, both with three agents all reporting confidence 0.4:

| Scenario | Agent correlation | Current formula | Correct interpretation |
|----------|------------------|-----------------|------------------------|
| **A** | Independent (r ≈ 0) | uncertainty = 0.42 | Three separate signals agree → strong evidence for uncertainty |
| **B** | Perfectly correlated (r = 1) | uncertainty = 0.42 | One signal, observed three times → same evidence as one agent |

The current formula produces identical `uncertainty_score = 0.42` for both.
In Scenario B, the three agents offer no more information than one — the
HITL gate behaves as if it had three independent confirmations when it has one.
This is the **phantom precision** problem: the effective sample size is
overstated, producing false confidence in the uncertainty estimate itself.

The converse also exists: if agents A and B are historically anti-correlated
(A is uncertain on auth PRs, B is uncertain on schema PRs, but they rarely
fire together), a run where *both* report low confidence is genuinely surprising
and deserves more weight than the arithmetic mean suggests.

---

## Theory

### Gauss-Markov Optimal Aggregation

Given agent confidence scores **x** = [x₁, …, xN] drawn from a joint
distribution with covariance Σ, the minimum-variance linear unbiased estimator
of the population mean is the **precision-weighted mean**:

```
x̄* = (1ᵀ Σ⁻¹ x) / (1ᵀ Σ⁻¹ 1)

with weights  w = Σ⁻¹ 1 / (1ᵀ Σ⁻¹ 1)   (sum to 1)
```

This is the Gauss-Markov theorem: **w** minimises the variance of the linear
estimator for any unbiased combination. When agents are independent
(Σ = σ²I), the weights collapse to the uniform 1/N vector. When agents are
perfectly correlated (Σ = σ² J, J = all-ones), the weights also remain 1/N —
you cannot do better than equal weighting with redundant information. The gains
appear when correlation is *heterogeneous*: some pairs highly correlated,
others not.

### Effective Number of Independent Agents

Let R be the N×N Pearson correlation matrix with eigenvalues λ₁, …, λN.
Since tr(R) = N, the **Kish effective sample size** adapted for eigenvalues is:

```
n_eff = (Σᵢ λᵢ)² / Σᵢ λᵢ²  =  N² / Σᵢ λᵢ²
```

Range: 1 ≤ n_eff ≤ N.
- All eigenvalues equal 1 (independent agents): n_eff = N
- One eigenvalue = N, rest = 0 (perfect correlation): n_eff = 1

`n_eff` is stored in pipeline state as `effective_n_agents` and returned by
`GET /runs/{run_id}`, giving reviewers a quantitative measure of how much
redundant information the current set of consumer agents is providing.

### Regularisation

The sample covariance matrix Σ̂ is computed from a finite number of historical
runs and becomes ill-conditioned when agents are nearly collinear. Tikhonov
regularisation is applied before inversion:

```
Σ_reg = Σ̂  +  λ·I
```

Default λ = 0.10 (`CORR_REGULARIZATION` env var). This shrinks the weights
toward uniform in proportion to the noise level. Negative precision weights
(which arise at small λ with near-singular matrices) are clipped to 0 and the
remainder is renormalised, producing a valid convex combination.

---

## Cold Start

The correlation matrix is only meaningful with enough historical data. Before
`CORR_MIN_RUNS` (default 10) complete runs are accumulated:

- `AgentCorrelationMatrix.from_store()` returns `None`
- `aggregate_agent_confidence()` falls back to the Feature 1 arithmetic mean
- `effective_n_agents` is `None` in the run state
- The HITL trigger and all downstream logic are unchanged

The transition is invisible to operators: on the 10th qualifying run, precision
weighting activates automatically. A DEBUG log entry records each cold-start
fallback.

---

## What Changes in the Aggregation Formula

```
# Before Feature 7 (Features 1–6):
mean = arithmetic_mean(agent_confidence_scores.values())
uncertainty_score = (1 − mean) × 0.7 + variance × 0.3

# After Feature 7 (when corr_matrix available):
mean          = arithmetic_mean(...)          # unchanged — stored for audit trail
adjusted_mean = corr_matrix.adjusted_mean(agent_confidence_scores)
uncertainty_score = (1 − adjusted_mean) × 0.7 + variance × 0.3
```

The variance term is intentionally left as the raw variance, not the
precision-weighted variance. The variance term captures *within-run*
disagreement between agents (one agent says 0.9, another says 0.3); this is
orthogonal to the cross-run correlation structure and should remain unmodified.

---

## Flow

```
Phase 3 — parallel consumer validation
  └─ ContractComplianceAgent emits confidence per consumer
       └─ agent_confidence_scores accumulated in state

aggregate_confidence_node
  ├─ AgentCorrelationMatrix.from_store(calibration_db)  ← Feature 7
  │    ├─ n_runs < 10  → None (cold start, uniform weights)
  │    └─ n_runs ≥ 10  → covariance matrix estimated from history
  │
  ├─ aggregate_agent_confidence(scores, corr_matrix)
  │    ├─ corr_matrix=None  → arithmetic mean (Feature 1 formula)
  │    └─ corr_matrix set   → precision-weighted mean (Feature 7 formula)
  │         └─ effective_n computed from eigenvalues of R
  │
  ├─ uncertainty_score → HITL threshold check (unchanged)
  └─ effective_n_agents stored in state

GET /runs/{run_id}
  └─ effective_n_agents exposed in response

GET /calibration/agent-correlations
  └─ full correlation matrix + precision weights + n_eff
```

---

## Example: Before vs After

Suppose three consumer agents are observed over 15 historical runs. The
estimated correlation matrix is:

|   | payment | order | notification |
|---|---------|-------|--------------|
| **payment** | 1.00 | 0.91 | 0.15 |
| **order** | 0.91 | 1.00 | 0.13 |
| **notification** | 0.15 | 0.13 | 1.00 |

payment-service and order-service are highly correlated (r = 0.91) — both tend
to be uncertain on the same PRs because both consume the same endpoints.
notification-svc is largely independent.

For a run where all three report confidence 0.40:

| Metric | Feature 1 (uniform) | Feature 7 (precision) |
|--------|---------------------|----------------------|
| Effective weights | 0.33 / 0.33 / 0.33 | 0.18 / 0.18 / 0.64 |
| Adjusted mean | 0.40 | 0.426 |
| Uncertainty score | 0.28 → MEDIUM | 0.26 → MEDIUM |
| n_eff | — | 2.04 of 3 |

The correlated payment+order pair is treated as ~1 independent signal.
notification-svc (independent) gets 3.5× its naïve weight. The aggregated mean
rises slightly toward the notification-svc score — a more honest representation
of the actual information content.

---

## API

### `GET /calibration/agent-correlations`

Returns the current correlation matrix once sufficient data is available.

```json
{
  "status":    "ok",
  "n_agents":  3,
  "n_runs":    42,
  "effective_n": 2.04,
  "agents":    ["contract_compliance_agent:k11-notification-svc",
                "contract_compliance_agent:k11-order-service",
                "contract_compliance_agent:k11-payment-service"],
  "correlation_matrix": {
    "contract_compliance_agent:k11-notification-svc": {
      "contract_compliance_agent:k11-notification-svc": 1.0,
      "contract_compliance_agent:k11-order-service":   0.1312,
      "contract_compliance_agent:k11-payment-service": 0.1501
    },
    ...
  },
  "precision_weights": {
    "contract_compliance_agent:k11-notification-svc": 0.6412,
    "contract_compliance_agent:k11-order-service":    0.1794,
    "contract_compliance_agent:k11-payment-service":  0.1794
  }
}
```

When fewer than `CORR_MIN_RUNS` complete runs exist:

```json
{
  "status":   "insufficient_data",
  "min_runs": 10,
  "message":  "Need at least 10 complete runs with resolved ground truth. ..."
}
```

### `GET /runs/{run_id}`

Now includes `effective_n_agents` (null before 10 qualifying runs):

```json
{
  "uncertainty_score":  0.26,
  "uncertainty_verdict": "MEDIUM",
  "effective_n_agents": 2.04,
  ...
}
```

---

## Configuration

```
CORR_MIN_RUNS=10          # minimum runs before correlation matrix activates (default 10)
CORR_REGULARIZATION=0.10  # Tikhonov λ added to diagonal before inversion (default 0.10)
CALIBRATION_ENABLED=true  # set to false to disable entirely (falls back to Feature 1)
```

Increasing `CORR_REGULARIZATION` shrinks weights toward uniform (safer for
small datasets). Decreasing it toward 0 allows the weights to track the true
precision matrix but risks numerical instability on highly correlated agents.

---

## Files Changed

| File | Change |
|------|--------|
| `calibration/correlation.py` | **New file.** `AgentCorrelationMatrix`: construction, effective_n, precision_weights, adjusted_mean, to_dict |
| `calibration/store.py` | `get_agent_scores_matrix()` — pivot calibration_log to runs × agents format |
| `pipeline/confidence.py` | `ConfidenceSummary` gains `effective_n` + `correlation_adjusted` fields; `aggregate_agent_confidence()` accepts optional `corr_matrix` |
| `pipeline/state.py` | `effective_n_agents: Optional[float]` field + `initial_state` default |
| `pipeline/orchestrator.py` | `aggregate_confidence_node` loads correlation matrix, passes to aggregation, returns `effective_n_agents` |
| `api/webhook.py` | `GET /calibration/agent-correlations` endpoint; `effective_n_agents` in `GET /runs/{run_id}` |
| `requirements.txt` | `numpy>=1.26.0`, `scikit-learn>=1.4.0` made explicit |

---

## Paper Section Placement

Feature 7 belongs in **Section 3 — System Design** under a subsection titled
*"3.7 Correlation-Aware Uncertainty Aggregation"* or
*"3.7 Modelling Inter-Agent Dependence"*.

The correlation matrix figure (heatmap from `GET /calibration/agent-correlations`)
and the n_eff time series (as more runs accumulate) should appear in
**Section 4 — Evaluation** alongside the calibration study.

The theoretical framing (Gauss-Markov, Kish effective N) is the paper's
primary contribution to the formal uncertainty quantification community. The
key claim to substantiate empirically: *does precision weighting improve ECE
relative to the arithmetic mean baseline?* With the current 53-run controlled
dataset all agents cluster in one confidence bin (0.85–1.0), so the improvement
will be modest. The paper's honest position is that Feature 7's full benefit
requires a production dataset spanning the full confidence range.
