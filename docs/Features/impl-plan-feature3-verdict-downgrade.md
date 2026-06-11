# Implementation Plan: Feature 3 — Confidence-Aware Verdict Downgrade
**Branch:** `feature/confidence-verdict-downgrade`
**Depends on:** Feature 1 (agent_confidence_scores, uncertainty_score in state)

---

## Overview

When `uncertainty_verdict == "HIGH"`, a `COMPATIBLE` verdict from a
`ContractComplianceAgent` should be downgraded to `UNCERTAIN`. This prevents
a low-confidence "all clear" from silently passing through the HITL gate.

**Without this:** An agent that returns `confidence=0.15, verdict=COMPATIBLE`
looks like a clean pass. The uncertainty HITL trigger fires (Feature 1), but the
final report still shows `COMPATIBLE`, confusing the reviewer.

**With this:** The verdict is adjusted to `UNCERTAIN` before the HITL interrupt
payload is built. The report accurately reflects the agent's actual confidence level.

---

## Design Decision: Where to Apply the Downgrade

The `compliance_results` list uses an `operator.add` reducer (append-only).
A node cannot overwrite it by returning a new list — it would append.

**Solution:** Add a new non-reducing state field `adjusted_compliance_results`
that holds the post-downgrade snapshot. Downstream nodes (`hitl_check`, `phase4`)
prefer it over `compliance_results` when set.

The downgrade runs inside the existing `aggregate_confidence_node`
(no new graph node needed).

---

## Files to Change

| File | Change Type |
|------|-------------|
| `pipeline/state.py` | Add `adjusted_compliance_results: Optional[list[dict]]` |
| `pipeline/confidence.py` | Add `downgrade_compatible_verdicts()` |
| `pipeline/orchestrator.py` | `aggregate_confidence_node` returns adjusted results |
| `pipeline/hitl.py` | Read `adjusted_compliance_results` first |
| `pipeline/phase4.py` | Read `adjusted_compliance_results` in report |
| `tests/unit/test_verdict_downgrade.py` | **New** |

---

## Step 1 — State Field (pipeline/state.py)

```python
# In MicroservicePipelineState, under "Confidence propagation":
adjusted_compliance_results: Optional[list[dict]]   # post-downgrade snapshot; None if no adjustment made
```

Add to `initial_state`:
```python
adjusted_compliance_results=None,
```

---

## Step 2 — Downgrade Helper (pipeline/confidence.py)

```python
def downgrade_compatible_verdicts(
    compliance_results: list[dict],
    agent_confidence_scores: dict[str, float],
    uncertainty_verdict: str,
) -> tuple[list[dict], int]:
    """
    When uncertainty is HIGH, downgrade each COMPATIBLE verdict whose
    per-agent confidence is below CONFIDENCE_DOWNGRADE_THRESHOLD.

    Returns (adjusted_results, downgrade_count).
    """
    CONFIDENCE_DOWNGRADE_THRESHOLD = float(
        os.getenv("CONFIDENCE_DOWNGRADE_THRESHOLD", "0.50")
    )

    if uncertainty_verdict != "HIGH":
        return compliance_results, 0

    adjusted = []
    downgrade_count = 0
    for r in compliance_results:
        consumer = r.get("consumer", "")
        score_key = f"contract_compliance_agent:{consumer}"
        confidence = agent_confidence_scores.get(score_key, 1.0)

        if r.get("verdict") == "COMPATIBLE" and confidence < CONFIDENCE_DOWNGRADE_THRESHOLD:
            r = {
                **r,
                "verdict":   "UNCERTAIN",
                "reasoning": (
                    f"[Downgraded from COMPATIBLE — agent confidence {confidence:.2f} "
                    f"< threshold {CONFIDENCE_DOWNGRADE_THRESHOLD}] "
                    + r.get("reasoning", "")
                ),
            }
            downgrade_count += 1
        adjusted.append(r)

    return adjusted, downgrade_count
```

---

## Step 3 — Aggregate Confidence Node (pipeline/orchestrator.py)

```python
async def aggregate_confidence_node(state: MicroservicePipelineState) -> dict:
    scores   = state.get("agent_confidence_scores") or {}
    summary  = aggregate_agent_confidence(scores)

    adjusted, downgrade_count = downgrade_compatible_verdicts(
        compliance_results=state.get("compliance_results", []),
        agent_confidence_scores=scores,
        uncertainty_verdict=summary.verdict,
    )

    if downgrade_count:
        logger.warning(
            "Verdict downgrade: %d COMPATIBLE → UNCERTAIN (uncertainty=%s)",
            downgrade_count, summary.verdict,
        )

    return {
        "uncertainty_score":           summary.uncertainty_score,
        "uncertainty_verdict":         summary.verdict,
        "adjusted_compliance_results": adjusted if downgrade_count else None,
    }
```

Import in orchestrator.py:
```python
from pipeline.confidence import aggregate_agent_confidence, downgrade_compatible_verdicts
```

---

## Step 4 — HITL Check Reads Adjusted Results (pipeline/hitl.py)

```python
async def cross_repo_hitl_check(state: MicroservicePipelineState) -> dict:
    # Prefer adjusted verdicts if available
    results = state.get("adjusted_compliance_results") or state.get("compliance_results", [])

    breaking_consumers = sum(1 for r in results if r.get("verdict") == "BREAKING")
    # ... rest unchanged
```

---

## Step 5 — Phase 4 Report Reads Adjusted Results (pipeline/phase4.py)

In the impact report agent / phase4, wherever `compliance_results` is read for
the final verdict counts:

```python
results = state.get("adjusted_compliance_results") or state.get("compliance_results", [])
```

---

## Step 6 — API Exposure (api/webhook.py)

Add `downgrade_count` to `/runs/{run_id}` response (derived from summary):

```python
adjusted = run["state"].get("adjusted_compliance_results")
return {
    ...
    "uncertainty_score":          run["state"].get("uncertainty_score"),
    "uncertainty_verdict":        run["state"].get("uncertainty_verdict"),
    "verdicts_downgraded":        (
        sum(1 for r in adjusted if "[Downgraded" in r.get("reasoning", ""))
        if adjusted else 0
    ),
}
```

---

## Environment Variables

```
CONFIDENCE_DOWNGRADE_THRESHOLD=0.50   # COMPATIBLE verdicts below this get → UNCERTAIN when uncertainty=HIGH
```

Add to `.env.example`.

---

## Tests (tests/unit/test_verdict_downgrade.py)

| Test | Covers |
|------|--------|
| `test_no_downgrade_when_uncertainty_medium` | Only fires on HIGH verdict |
| `test_compatible_low_confidence_downgraded` | Core behaviour |
| `test_compatible_high_confidence_not_downgraded` | Confident COMPATIBLE preserved |
| `test_breaking_verdict_never_downgraded` | BREAKING stays BREAKING |
| `test_uncertain_verdict_not_double_downgraded` | UNCERTAIN stays UNCERTAIN |
| `test_reasoning_string_annotated` | Downgraded result carries annotation |
| `test_zero_downgrades_returns_original_list` | No adjustment when none needed |

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `downgrade_compatible_verdicts()` | 1 hour |
| State field + initial_state | 15 min |
| orchestrator_node update | 30 min |
| hitl.py + phase4.py read adjusted results | 30 min |
| Tests | 1.5 hours |
| **Total** | **~3.75 hours** |
