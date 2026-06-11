# Feature 3 — Confidence-Aware Verdict Downgrade

## Overview

Builds on Feature 1 (confidence propagation) to prevent low-confidence
`COMPATIBLE` verdicts from silently bypassing the HITL gate. When the
overall pipeline `uncertainty_verdict` is `HIGH`, any `COMPATIBLE` verdict
whose per-agent confidence is below `CONFIDENCE_DOWNGRADE_THRESHOLD` (default
0.50) is automatically downgraded to `UNCERTAIN`.

This is the **"I passed but I'm not sure"** correction — the agent reported
clean, but the confidence score reveals it wasn't really sure.

---

## The Problem

Without this feature, the following scenario produces a false-safe outcome:

```
Agent: ContractComplianceAgent  consumer=k11-payment-service
LLM response: { "verdict": "COMPATIBLE", "confidence": 0.15, ... }
```

- Feature 1 fires HITL (uncertainty_score >= threshold) ✅
- But `compliance_results` still shows `COMPATIBLE` for payment-service
- The human reviewer sees the interrupt but the report says "COMPATIBLE" — misleading

**After Feature 3:**

```
adjusted_compliance_results: { "verdict": "UNCERTAIN", "reasoning": "[Downgraded COMPATIBLE → UNCERTAIN — agent confidence 0.15 < threshold 0.50] ..." }
```

The report accurately reflects the agent's actual confidence.

---

## When the Downgrade Fires

Two conditions must **both** be true:

| Condition | Check |
|-----------|-------|
| Overall uncertainty is HIGH | `uncertainty_verdict == "HIGH"` |
| Per-agent confidence is low | `agent_confidence_scores["contract_compliance_agent:{consumer}"] < CONFIDENCE_DOWNGRADE_THRESHOLD` |

The downgrade only applies to `COMPATIBLE` verdicts — `BREAKING` and `UNCERTAIN`
verdicts are never touched. If a consumer has no confidence score entry, it
defaults to 1.0 (high confidence) and is not downgraded.

---

## Design: `adjusted_compliance_results`

`compliance_results` uses an `operator.add` reducer (append-only) — a node
cannot overwrite it by returning a new list. Feature 3 instead writes to a
new **non-reducing** state field:

```
adjusted_compliance_results: Optional[list[dict]]
```

- `None` when no verdicts were downgraded (zero overhead on the happy path)
- Set to the full post-downgrade list when ≥ 1 verdict changed

All downstream consumers of `compliance_results` — `hitl_check`,
`ImpactReportAgent`, and `GET /runs/{run_id}` — now prefer
`adjusted_compliance_results` when set:

```python
results = state.get("adjusted_compliance_results") or state.get("compliance_results", [])
```

---

## Flow

```
phase3 (parallel compliance checks)
  └─ agent_confidence_scores accumulated per consumer

aggregate_confidence_node
  ├─ aggregate_agent_confidence()     → uncertainty_verdict
  └─ downgrade_compatible_verdicts()  → adjusted_compliance_results (if any changed)

hitl_check
  └─ reads adjusted_compliance_results (post-downgrade breaking count)

phase4 / ImpactReportAgent
  └─ reads adjusted_compliance_results for final report
```

---

## Files Changed

| File | Change |
|------|--------|
| `pipeline/confidence.py` | `downgrade_compatible_verdicts()`, `CONFIDENCE_DOWNGRADE_THRESHOLD` |
| `pipeline/state.py` | `adjusted_compliance_results: Optional[list[dict]]` |
| `pipeline/orchestrator.py` | `aggregate_confidence_node` calls downgrade; returns adjusted results |
| `pipeline/hitl.py` | `cross_repo_hitl_check` and preliminary summary prefer adjusted results |
| `agents/impact_report_agent.py` | Reads adjusted results; report table adds Verdicts Downgraded and Agent Uncertainty rows |
| `api/webhook.py` | `GET /runs/{run_id}` returns `verdicts_downgraded` count |
| `.env.example` | `CONFIDENCE_DOWNGRADE_THRESHOLD=0.50` |
| `tests/unit/test_verdict_downgrade.py` | 13 unit tests |

---

## Configuration

```
CONFIDENCE_DOWNGRADE_THRESHOLD=0.50   # COMPATIBLE verdicts below this → UNCERTAIN when uncertainty=HIGH
UNCERTAINTY_THRESHOLD=0.35            # (Feature 1) threshold that sets uncertainty_verdict=HIGH
```

---

## Impact Report Changes

The Phase 4 markdown report now includes two new rows in the summary table:

| Metric | Value |
|--------|-------|
| Verdicts Downgraded | 1 |
| Agent Uncertainty | HIGH |

Downgraded consumers appear in the **Uncertain — Manual Review Recommended**
section (not Compatible), with reasoning annotated:

```
⚠️ `k11-payment-service` — [Downgraded COMPATIBLE → UNCERTAIN — agent confidence
0.15 < threshold 0.50] Response field removal may affect payment validation logic...
```

## API Response

`GET /runs/{run_id}` now includes:

```json
{
  "uncertainty_score": 0.42,
  "uncertainty_verdict": "HIGH",
  "verdicts_downgraded": 1
}
```

---

## Test Coverage

```
test_no_downgrade_when_uncertainty_low           only fires on HIGH
test_no_downgrade_when_uncertainty_medium        only fires on HIGH
test_compatible_low_confidence_downgraded        core behaviour
test_compatible_above_threshold_not_downgraded   confident COMPATIBLE preserved
test_compatible_at_exact_threshold_not_downgraded boundary: < not <=
test_breaking_verdict_never_downgraded           BREAKING untouched
test_uncertain_verdict_not_double_downgraded     UNCERTAIN untouched
test_reasoning_annotated_on_downgrade            annotation content
test_zero_downgrades_returns_original_list       no-op path
test_missing_score_key_defaults_to_high_conf     safe default
test_mixed_results_partial_downgrade             partial list
test_empty_results_returns_empty                 edge case
test_original_dict_not_mutated                   mutation safety
─────────────────────────────────────────────────────────────────
Total  13 tests  ✅ all passing
```
