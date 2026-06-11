# Implementation Plan: Feature 1 — Agent Confidence Propagation
**Repo:** `k11techlab-agentic-ai-qa-system`
**Branch:** `feature/agent-confidence-propagation`

---

## Overview

Each Phase 2 agent reports a `confidence_score` alongside its pass/fail result. The evaluation layer combines agent confidence with DeepEval/RAGAS metrics into a single `uncertainty_score`. The HITL gate surfaces low-confidence verdicts to humans even when `risk_score` is below threshold — the "I passed but I'm not sure" signal.

---

## Files to Change

| File | Change Type |
|---|---|
| `pipeline/state.py` | Add 3 new fields to TypedDict |
| `pipeline/phase2.py` | Each agent node returns `confidence_score` |
| `pipeline/evaluation.py` | `aggregate_eval` computes `uncertainty_score` |
| `pipeline/hitl.py` | Second HITL condition on `uncertainty_score` |
| `pipeline/confidence.py` | **New file** — aggregation helpers |
| `pipeline/orchestrator.py` | Wire new confidence node |
| `api/webhook.py` | Expose `uncertainty_score` in response payload |

---

## Step 1 — `pipeline/state.py`

Add three fields to `CIPipelineState`:

```python
from typing import TypedDict, Annotated
import operator

class CIPipelineState(TypedDict):
    # --- existing fields (unchanged) ---
    pr_url: str
    risk_score: float
    risk_level: str
    test_plan: list[dict]
    agent_results: Annotated[list[dict], operator.add]
    eval_results: dict
    hitl_required: bool
    hitl_decision: str | None
    report_url: str | None

    # --- NEW fields ---
    agent_confidence_scores: Annotated[dict[str, float], lambda a, b: {**a, **b}]
    uncertainty_score: float          # 0.0 = certain, 1.0 = maximally uncertain
    uncertainty_verdict: str          # "LOW" | "MEDIUM" | "HIGH"
```

---

## Step 2 — `pipeline/confidence.py` (new file)

```python
"""
Confidence aggregation helpers for Phase 2 agents and the evaluation layer.
"""
import statistics
from dataclasses import dataclass

UNCERTAINTY_THRESHOLD = float(os.getenv("UNCERTAINTY_THRESHOLD", "0.35"))

@dataclass
class ConfidenceSummary:
    mean: float
    minimum: float
    variance: float
    uncertainty_score: float   # 1 - mean, adjusted for variance
    verdict: str               # LOW / MEDIUM / HIGH


def aggregate_agent_confidence(scores: dict[str, float]) -> ConfidenceSummary:
    """
    Given a dict of {agent_name: confidence_score}, compute aggregate
    uncertainty metrics.
    """
    vals = list(scores.values())
    if not vals:
        return ConfidenceSummary(0.0, 0.0, 0.0, 1.0, "HIGH")

    mean = statistics.mean(vals)
    minimum = min(vals)
    variance = statistics.variance(vals) if len(vals) > 1 else 0.0

    # Uncertainty rises when mean confidence is low OR variance is high
    uncertainty_score = round((1 - mean) * 0.7 + variance * 0.3, 4)
    uncertainty_score = min(1.0, max(0.0, uncertainty_score))

    if uncertainty_score < 0.20:
        verdict = "LOW"
    elif uncertainty_score < UNCERTAINTY_THRESHOLD:
        verdict = "MEDIUM"
    else:
        verdict = "HIGH"

    return ConfidenceSummary(
        mean=round(mean, 4),
        minimum=round(minimum, 4),
        variance=round(variance, 4),
        uncertainty_score=uncertainty_score,
        verdict=verdict,
    )


def combine_with_eval_metrics(
    agent_uncertainty: float,
    deepeval_score: float,      # already 0-1, higher = better quality
    ragas_score: float,         # already 0-1, higher = better quality
    weights: tuple[float, float, float] = (0.5, 0.25, 0.25),
) -> float:
    """
    Weighted combination of agent uncertainty and eval metric uncertainty.
    deepeval and ragas scores are quality scores — invert them to uncertainty.
    """
    w_agent, w_deep, w_ragas = weights
    combined = (
        w_agent * agent_uncertainty
        + w_deep * (1 - deepeval_score)
        + w_ragas * (1 - ragas_score)
    )
    return round(min(1.0, max(0.0, combined)), 4)
```

---

## Step 3 — `pipeline/phase2.py`

Each agent node must return a `confidence_score`. The simplest approach is an LLM self-assessment prompt appended to the existing agent prompt.

### Pattern to apply to all 8 agents:

```python
CONFIDENCE_INSTRUCTION = """
After completing your analysis, assign a confidence_score between 0.0 and 1.0
indicating how certain you are in your verdict:
  1.0 = completely certain (clear evidence, unambiguous result)
  0.7 = reasonably confident (some ambiguity but strong signal)
  0.5 = uncertain (conflicting signals)
  0.0 = no confidence (insufficient information)

Return your result as JSON with a top-level "confidence_score" field.
"""

# Example — api_agent node (apply same pattern to all 8)
async def api_agent_node(state: CIPipelineState) -> dict:
    result = await call_mcp("playwright", "run_api_tests", {
        "test_plan": state["test_plan"],
        "confidence_instruction": CONFIDENCE_INSTRUCTION,
    })

    return {
        "agent_results": [{"agent": "api_agent", **result}],
        "agent_confidence_scores": {
            "api_agent": result.get("confidence_score", 0.5)
        },
    }
```

> **Note:** For agents that call external tools (k6, Playwright MCP) and don't use an LLM for verdict, compute confidence from result metadata instead — e.g. `confidence = 1.0 if all tests passed else 0.6 if pass_rate > 0.8 else 0.3`.

### Deterministic confidence fallback (non-LLM agents):

```python
def compute_deterministic_confidence(result: dict) -> float:
    """For perf_agent, browser_agent — infer confidence from test metrics."""
    pass_rate = result.get("pass_rate", 0)
    error_count = result.get("errors", 0)
    if error_count == 0 and pass_rate == 1.0:
        return 0.95
    elif pass_rate >= 0.8:
        return 0.75
    elif pass_rate >= 0.5:
        return 0.55
    else:
        return 0.30
```

---

## Step 4 — `pipeline/evaluation.py`

In the existing `aggregate_eval` node, add confidence aggregation before the quality gate decision:

```python
from pipeline.confidence import aggregate_agent_confidence, combine_with_eval_metrics

def aggregate_eval_node(state: CIPipelineState) -> dict:
    # --- existing DeepEval + RAGAS logic (unchanged) ---
    deepeval_score = state["eval_results"].get("deepeval_score", 0.8)
    ragas_score    = state["eval_results"].get("ragas_score", 0.8)

    # --- NEW: agent confidence aggregation ---
    conf_summary = aggregate_agent_confidence(state["agent_confidence_scores"])

    uncertainty_score = combine_with_eval_metrics(
        agent_uncertainty=conf_summary.uncertainty_score,
        deepeval_score=deepeval_score,
        ragas_score=ragas_score,
    )

    return {
        "eval_results": {
            **state["eval_results"],
            "confidence_summary": {
                "mean_agent_confidence": conf_summary.mean,
                "min_agent_confidence": conf_summary.minimum,
                "confidence_variance": conf_summary.variance,
            },
        },
        "uncertainty_score": uncertainty_score,
        "uncertainty_verdict": conf_summary.verdict,
    }
```

---

## Step 5 — `pipeline/hitl.py`

Add the second HITL trigger condition:

```python
import os

RISK_THRESHOLD       = float(os.getenv("HITL_RISK_THRESHOLD", "0.85"))
UNCERTAINTY_THRESHOLD = float(os.getenv("UNCERTAINTY_THRESHOLD", "0.35"))

def should_escalate_to_hitl(state: CIPipelineState) -> bool:
    risk_triggered        = state["risk_score"] >= RISK_THRESHOLD
    uncertainty_triggered = state.get("uncertainty_score", 0.0) >= UNCERTAINTY_THRESHOLD

    if risk_triggered:
        print(f"[HITL] Triggered by risk_score={state['risk_score']:.3f}")
    if uncertainty_triggered:
        print(f"[HITL] Triggered by uncertainty_score={state['uncertainty_score']:.3f} "
              f"(verdict={state.get('uncertainty_verdict')})")

    return risk_triggered or uncertainty_triggered


def hitl_gate_node(state: CIPipelineState):
    if should_escalate_to_hitl(state):
        from langgraph.types import interrupt
        decision = interrupt({
            "reason": "hitl_required",
            "risk_score": state["risk_score"],
            "uncertainty_score": state.get("uncertainty_score"),
            "uncertainty_verdict": state.get("uncertainty_verdict"),
            "message": (
                "Low agent confidence detected. Human review required "
                "even though risk score is below threshold."
                if state.get("uncertainty_score", 0) >= UNCERTAINTY_THRESHOLD
                   and state["risk_score"] < RISK_THRESHOLD
                else "Risk score exceeds threshold."
            ),
        })
        return {"hitl_decision": decision, "hitl_required": True}
    return {"hitl_required": False}
```

---

## Step 6 — `pipeline/orchestrator.py`

No new nodes needed — `aggregate_eval` already runs before the quality gate. Just verify the edge order:

```
phase2_fan_in → aggregate_eval (now includes confidence) → hitl_gate → phase3
```

If `aggregate_eval` runs after `hitl_gate` in the current graph, swap the order:

```python
# Before (current):
builder.add_edge("phase2_fan_in", "hitl_gate")
builder.add_edge("hitl_gate", "aggregate_eval")

# After (new order):
builder.add_edge("phase2_fan_in", "aggregate_eval")
builder.add_edge("aggregate_eval", "hitl_gate")
```

---

## Step 7 — `api/webhook.py`

Add `uncertainty_score` to the pipeline response:

```python
@app.post("/api/pipeline/run")
async def run_pipeline_endpoint(payload: PRWebhookPayload):
    result = await run_pipeline(payload.pr_url)
    return {
        "pr_url": payload.pr_url,
        "risk_score": result["risk_score"],
        "uncertainty_score": result.get("uncertainty_score"),
        "uncertainty_verdict": result.get("uncertainty_verdict"),
        "hitl_required": result["hitl_required"],
        "report_url": result.get("report_url"),
    }
```

---

## Environment Variables

Add to `.env.example`:

```
UNCERTAINTY_THRESHOLD=0.35     # 0.0-1.0; above this triggers HITL
CONFIDENCE_WEIGHTS=0.5,0.25,0.25  # agent / deepeval / ragas blend
```

---

## Testing Plan

| Test | What to verify |
|---|---|
| `test_confidence_aggregation.py` | `aggregate_agent_confidence` returns correct mean/variance/verdict |
| `test_combine_metrics.py` | Weighted combination stays in [0, 1] across edge cases |
| `test_hitl_uncertainty_trigger.py` | HITL fires when uncertainty > threshold even if risk < 0.85 |
| `test_agent_confidence_score.py` | Each agent node returns `confidence_score` key in output dict |
| `test_state_reducer.py` | `agent_confidence_scores` dict reducer merges correctly across parallel agents |

---

## Estimated Effort

| Task | Effort |
|---|---|
| state.py + confidence.py | 2 hours |
| phase2.py (8 agents) | 3 hours |
| evaluation.py + hitl.py | 2 hours |
| orchestrator.py wiring | 1 hour |
| Tests | 3 hours |
| **Total** | **~11 hours** |
