# Implementation Plan: Feature 4 — Contract Change History & Drift Detection
**Branch:** `feature/drift-detection`

---

## Overview

The contract registry stores versioned contract snapshots keyed by `(service, sha)`,
and `get_contract_history()` already exists. This feature adds a **drift detector**
that analyses the change history before the pipeline runs a full diff.

A service whose contract has changed frequently in a short window is statistically
more likely to introduce a breaking change — and warrants elevated HITL sensitivity.

**Drift signal:** number of distinct SHAs recorded for a service in the past N days.
A high-velocity service gets a raised `uncertainty_score` floor and a HITL warning
even if the current PR looks clean.

---

## Files to Change

| File | Change Type |
|------|-------------|
| `analyzer/drift_detector.py` | **New** — `DriftReport`, `detect_drift()` |
| `contracts/registry.py` | Add `get_contract_history_since()` query |
| `pipeline/state.py` | Add `drift_report: Optional[dict]` |
| `pipeline/phase1.py` | Call drift detector after contract extraction |
| `pipeline/hitl.py` | Surface drift in HITL interrupt payload |
| `pipeline/confidence.py` | `apply_drift_floor()` — raise uncertainty floor when drift is high |
| `pipeline/orchestrator.py` | Pass drift_report into aggregate_confidence_node |
| `api/webhook.py` | Expose `drift_report` in `/runs/{run_id}` |
| `tests/unit/test_drift_detector.py` | **New** |

---

## Step 1 — Registry Query (contracts/registry.py)

Add a time-bounded history query:

```python
async def get_contract_history_since(
    self,
    service_name: str,
    since_iso: str,          # ISO 8601 timestamp
) -> list[dict]:
    """Return all contract records for service_name recorded after since_iso."""
    async with self._db.execute(
        """SELECT sha, version, recorded_at FROM contracts
           WHERE service_name = ? AND recorded_at >= ?
           ORDER BY id DESC""",
        (service_name, since_iso),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
```

---

## Step 2 — Drift Detector (analyzer/drift_detector.py)

```python
"""
analyzer/drift_detector.py
───────────────────────────
Detects contract change velocity — a leading indicator of instability.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DRIFT_WINDOW_DAYS   = int(os.getenv("DRIFT_WINDOW_DAYS", "30"))
DRIFT_HIGH_THRESHOLD = int(os.getenv("DRIFT_HIGH_THRESHOLD", "5"))   # changes in window
DRIFT_CRITICAL_THRESHOLD = int(os.getenv("DRIFT_CRITICAL_THRESHOLD", "10"))


@dataclass
class DriftReport:
    service:          str
    window_days:      int
    change_count:     int         # number of distinct SHAs in window
    change_velocity:  float       # changes per week
    drift_level:      str         # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    last_changed_at:  str | None  # ISO timestamp of most recent change
    uncertainty_floor: float      # minimum uncertainty_score to apply (0.0 if no drift)

    def to_dict(self) -> dict:
        return {
            "service":           self.service,
            "window_days":       self.window_days,
            "change_count":      self.change_count,
            "change_velocity":   round(self.change_velocity, 2),
            "drift_level":       self.drift_level,
            "last_changed_at":   self.last_changed_at,
            "uncertainty_floor": self.uncertainty_floor,
        }


def detect_drift(
    service: str,
    history: list[dict],         # rows from get_contract_history_since()
    window_days: int = DRIFT_WINDOW_DAYS,
) -> DriftReport:
    """
    Compute drift metrics from contract history rows.

    Each row must have at least {"sha": str, "recorded_at": str}.
    """
    unique_shas = {r["sha"] for r in history}
    change_count = len(unique_shas)

    velocity = (change_count / window_days) * 7  # per week

    if change_count == 0:
        drift_level = "LOW"
        uncertainty_floor = 0.0
    elif change_count < DRIFT_HIGH_THRESHOLD:
        drift_level = "MEDIUM"
        uncertainty_floor = 0.10
    elif change_count < DRIFT_CRITICAL_THRESHOLD:
        drift_level = "HIGH"
        uncertainty_floor = 0.25
    else:
        drift_level = "CRITICAL"
        uncertainty_floor = 0.40

    last_changed_at = history[0]["recorded_at"] if history else None

    return DriftReport(
        service=service,
        window_days=window_days,
        change_count=change_count,
        change_velocity=velocity,
        drift_level=drift_level,
        last_changed_at=last_changed_at,
        uncertainty_floor=uncertainty_floor,
    )
```

---

## Step 3 — State Field (pipeline/state.py)

```python
# In the "Phase 1" section:
drift_report: Optional[dict]   # DriftReport.to_dict() for the provider service
```

Add to `initial_state`:
```python
drift_report=None,
```

---

## Step 4 — Phase 1 Integration (pipeline/phase1.py)

After contract extraction, add a drift detection step:

```python
async def detect_drift_node(state: MicroservicePipelineState) -> dict:
    """Query contract history and compute drift metrics for the provider service."""
    import os
    from datetime import datetime, timedelta, timezone
    from analyzer.drift_detector import detect_drift, DRIFT_WINDOW_DAYS
    from contracts.registry import ContractRegistry

    service = state["repo_name"].split("/")[-1]
    db = os.getenv("CONTRACT_REGISTRY_DB", "contract_registry.db")
    since = (
        datetime.now(timezone.utc) - timedelta(days=DRIFT_WINDOW_DAYS)
    ).isoformat()

    try:
        async with ContractRegistry(db) as registry:
            history = await registry.get_contract_history_since(service, since)
        report = detect_drift(service, history)
        return {"drift_report": report.to_dict()}
    except Exception as exc:
        logger.warning("Drift detection failed for %s: %s", service, exc)
        return {"drift_report": None}
```

Wire in `build_phase1()`:
```python
builder.add_node("detect_drift", detect_drift_node)
# After extract_contract node, before END:
builder.add_edge("extract_contract", "detect_drift")
builder.add_edge("detect_drift", END)
```

---

## Step 5 — Drift Floor in Confidence Aggregation (pipeline/confidence.py)

```python
def apply_drift_floor(
    uncertainty_score: float,
    drift_report: dict | None,
) -> float:
    """
    Raise uncertainty_score to the drift floor if drift is HIGH/CRITICAL.
    Ensures high-velocity services are never reported as fully certain.
    """
    if not drift_report:
        return uncertainty_score
    floor = drift_report.get("uncertainty_floor", 0.0)
    return max(uncertainty_score, floor)
```

Call in `aggregate_confidence_node` (orchestrator.py):
```python
uncertainty_score = apply_drift_floor(
    summary.uncertainty_score,
    state.get("drift_report"),
)
```

---

## Step 6 — HITL Payload (pipeline/hitl.py)

Add drift context to the interrupt payload:

```python
decision = interrupt({
    ...
    "drift_report":       state.get("drift_report"),
    "message": ...,
})
```

Also log drift level when HITL triggers:
```python
drift = state.get("drift_report") or {}
if drift.get("drift_level") in ("HIGH", "CRITICAL"):
    logger.warning(
        "High drift detected for %s: %d changes in %d days (velocity=%.1f/week)",
        state["repo_name"], drift["change_count"],
        drift["window_days"], drift["change_velocity"],
    )
```

---

## Step 7 — API Exposure (api/webhook.py)

```python
return {
    ...
    "drift_report": run["state"].get("drift_report"),
}
```

---

## Environment Variables

```
DRIFT_WINDOW_DAYS=30          # lookback window for history query
DRIFT_HIGH_THRESHOLD=5        # changes in window → HIGH drift
DRIFT_CRITICAL_THRESHOLD=10   # changes in window → CRITICAL drift
```

Add to `.env.example`.

---

## Tests (tests/unit/test_drift_detector.py)

| Test | Covers |
|------|--------|
| `test_no_history_returns_low_drift` | Empty registry |
| `test_few_changes_medium_drift` | 2–4 changes |
| `test_high_changes_triggers_floor` | ≥ 5 changes, floor = 0.25 |
| `test_critical_drift_maximum_floor` | ≥ 10 changes, floor = 0.40 |
| `test_velocity_calculated_per_week` | Math check |
| `test_apply_drift_floor_raises_score` | Floor overrides lower score |
| `test_apply_drift_floor_no_report` | None drift_report is safe |
| `test_duplicate_shas_counted_once` | Unique SHA counting |

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `drift_detector.py` + registry query | 2 hours |
| State field + phase1 node | 1 hour |
| `apply_drift_floor` + orchestrator wiring | 30 min |
| hitl.py + api/webhook.py | 30 min |
| Tests | 2 hours |
| **Total** | **~6 hours** |
