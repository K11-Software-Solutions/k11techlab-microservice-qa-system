# Feature 1 — Agent Confidence Propagation

## Overview

Each agent in the pipeline reports a `confidence_score` alongside its verdict. These scores are aggregated into a single `uncertainty_score` that acts as a third HITL trigger — surfacing low-confidence verdicts to humans even when `impact_score` and `breaking_consumer` count are both below their thresholds. This is the **"I passed but I'm not sure"** signal.

---

## How It Works

### 1. Per-Agent Confidence Scoring

| Agent | Method | Score Range |
|-------|--------|-------------|
| `DependencyGraphAgent` (Phase 2) | Deterministic — based on graph load success and consumer count | 0.40 – 0.92 |
| `ContractComplianceAgent` × N (Phase 3) | LLM self-assessment — returned in JSON response | 0.0 – 1.0 |

**Deterministic confidence rules (Phase 2):**

| Condition | Confidence |
|-----------|-----------|
| Error during graph load | 0.40 |
| Graph not loaded | 0.50 |
| Graph loaded, no consumers found | 0.85 |
| Graph loaded, consumers found | 0.92 |

**LLM confidence (Phase 3):**  
The `ContractComplianceAgent` system prompt instructs the LLM to return a `confidence` field (0.0–1.0) with every verdict. The value is extracted from the JSON response and stored per-consumer.

### 2. State Accumulation

All scores are merged into a single dict field in `MicroservicePipelineState`:

```
agent_confidence_scores: {
    "dependency_graph_agent": 0.92,
    "contract_compliance_agent:k11-order-service": 0.85,
    "contract_compliance_agent:k11-payment-service": 0.40,
    ...
}
```

The dict uses a merge reducer so parallel Phase 3 workers write their entries without collisions.

### 3. Aggregation Node

A new `aggregate_confidence` node runs between Phase 3 and the HITL check:

```
phase3 → aggregate_confidence → hitl_check
```

It computes:

```
uncertainty_score = (1 - mean_confidence) × 0.7 + confidence_variance × 0.3
```

Uncertainty rises when:
- Mean confidence across agents is low (agents unsure), **or**
- Agents disagree with each other (high variance)

| `uncertainty_score` | `uncertainty_verdict` |
|---------------------|----------------------|
| < 0.20 | LOW |
| 0.20 – 0.35 | MEDIUM |
| ≥ 0.35 | HIGH |

### 4. HITL Gate — Third Trigger

`cross_repo_hitl_check` now has three independent conditions:

| Trigger | Condition | Default Threshold |
|---------|-----------|------------------|
| Impact score | `impact_score >= IMPACT_HITL_THRESHOLD` | 0.60 |
| Breaking consumers | `breaking_consumers >= BREAKING_CONSUMER_HITL_COUNT` | 2 |
| **Uncertainty** | **`uncertainty_score >= UNCERTAINTY_THRESHOLD`** | **0.35** |

When the uncertainty trigger fires, the HITL interrupt message explicitly states that confidence — not impact — was the reason:

> *"Low agent confidence detected. Human review required even though impact score is below threshold."*

### 5. API Exposure

`GET /runs/{run_id}` now returns:

```json
{
  "run_id": "...",
  "status": "completed",
  "summary": { ... },
  "uncertainty_score": 0.41,
  "uncertainty_verdict": "HIGH",
  "hitl_required": true
}
```

---

## Configuration

Add to `.env`:

```
UNCERTAINTY_THRESHOLD=0.35     # 0.0–1.0; above this triggers HITL
CONFIDENCE_WEIGHTS=0.5,0.25,0.25  # reserved for future eval-metric blending
```

---

## Files Changed

| File | Change |
|------|--------|
| `pipeline/confidence.py` | **New** — `aggregate_agent_confidence()`, `compute_deterministic_confidence()` |
| `pipeline/state.py` | Added `agent_confidence_scores`, `uncertainty_score`, `uncertainty_verdict` |
| `pipeline/phase2.py` | `traverse_graph_node` attaches deterministic confidence |
| `pipeline/phase3.py` | `validate_consumer_node` attaches LLM confidence per consumer |
| `pipeline/orchestrator.py` | New `aggregate_confidence` node; rewired `phase3 → aggregate_confidence → hitl_check` |
| `pipeline/hitl.py` | Third HITL trigger on `uncertainty_score`; uncertainty fields added to interrupt payload |
| `api/webhook.py` | `/runs/{run_id}` returns `uncertainty_score` and `uncertainty_verdict` |
| `.env.example` | `UNCERTAINTY_THRESHOLD`, `CONFIDENCE_WEIGHTS` |
| `tests/unit/test_confidence.py` | 15 unit tests |
| `tests/unit/test_hitl_uncertainty.py` | 7 unit tests |

---

## Test Coverage

```
tests/unit/test_confidence.py         15 tests — aggregation math, edge cases, dict reducer
tests/unit/test_hitl_uncertainty.py    7 tests — HITL trigger conditions, boundary values
────────────────────────────────────────────────
Total                                 22 tests  ✅ all passing
```

---

## Example: Uncertainty-Only HITL

A PR with no breaking changes and low impact score (0.10) — but where one `ContractComplianceAgent` returns `confidence=0.15` while others return `0.85`:

```
agent_confidence_scores = {
    "dependency_graph_agent":                      0.92,
    "contract_compliance_agent:k11-order-service": 0.85,
    "contract_compliance_agent:k11-payment-service": 0.15,   ← outlier
}

mean     = 0.64
variance = 0.16
uncertainty_score = (1 - 0.64) × 0.7 + 0.16 × 0.3 = 0.300  (MEDIUM — no HITL)
```

If `k11-payment-service` drops further to `confidence=0.05`:

```
mean     = 0.61
variance = 0.19
uncertainty_score = (1 - 0.61) × 0.7 + 0.19 × 0.3 = 0.330  → still MEDIUM

# With two low-confidence agents (0.20, 0.20):
mean     = 0.44
variance = 0.11
uncertainty_score = (1 - 0.44) × 0.7 + 0.11 × 0.3 = 0.425  → HIGH → HITL triggered ✅
```
