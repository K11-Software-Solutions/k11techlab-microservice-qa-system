# Feature 4 — Contract Change History & Drift Detection

## Overview

Tracks how rapidly a provider service's contract evolves over a rolling time
window and converts that velocity into an **uncertainty floor** that feeds
directly into Feature 1's confidence pipeline.

High-churn services — those with frequent contract edits — are inherently
riskier to approve silently, even when individual change diffs look clean. This
feature makes that velocity visible and automatically raises the uncertainty
score when the drift level is HIGH or CRITICAL.

---

## The Problem

Without this feature, two PRs can look identical to the pipeline:

```
PR A — k11-payment-service, first contract change in 6 months, no breaking diffs
PR B — k11-payment-service, 14th contract change this month, no breaking diffs
```

Both return `uncertainty_verdict=LOW` because the per-agent scores are high.
Only PR B should receive elevated scrutiny — the velocity pattern alone signals
systemic instability.

**After Feature 4:**

```
PR B → drift_level=CRITICAL → uncertainty_floor=0.40
     → uncertainty_score raised to max(agent_score, 0.40)
     → uncertainty_verdict=HIGH → HITL triggered
```

---

## Drift Classification

Drift is measured as distinct contract SHA count within a configurable window
(default 30 days). Duplicate SHAs (re-runs of the same commit) are deduplicated
before counting.

| Level | Condition | Uncertainty Floor |
|-------|-----------|-------------------|
| LOW | `change_count < DRIFT_MEDIUM_THRESHOLD` (default 5) | 0.00 |
| MEDIUM | `change_count >= 5` | 0.10 |
| HIGH | `change_count >= DRIFT_HIGH_THRESHOLD` (default 10) | 0.25 |
| CRITICAL | `change_count >= DRIFT_CRITICAL_THRESHOLD` (default 15) | 0.40 |

Change velocity is also computed as `change_count / (window_days / 7)` (changes
per week) and stored in the report for human reviewers.

---

## Flow

```
phase1
  ├─ extract_contract_node   (existing)
  └─ detect_drift_node       (new)
       └─ ContractRegistry.get_contract_history_since()
       └─ analyze_drift.detect_drift()
       └─ writes drift_report into state

aggregate_confidence_node  (Feature 1 + Feature 4 combined)
  ├─ aggregate_agent_confidence()  → base uncertainty_score
  ├─ apply_drift_floor()           → max(base, drift_floor)
  └─ recomputes verdict string if score changed
       → raises HITL trigger when floor pushes score above UNCERTAINTY_THRESHOLD

hitl.cross_repo_human_review
  └─ logs HIGH/CRITICAL drift warning
  └─ includes drift_report in interrupt payload
```

---

## `DriftReport` dataclass

```python
@dataclass
class DriftReport:
    service:          str
    window_days:      int
    change_count:     int
    change_velocity:  float   # changes per week
    drift_level:      str     # LOW | MEDIUM | HIGH | CRITICAL
    last_changed_at:  str | None
    uncertainty_floor: float

    def to_dict(self) -> dict: ...
```

---

## Files Changed

| File | Change |
|------|--------|
| `analyzer/drift_detector.py` | New file — `DriftReport`, `detect_drift()` |
| `contracts/registry.py` | `get_contract_history_since(service, since_iso)` |
| `pipeline/phase1.py` | `detect_drift_node`; graph adds `detect_drift` after `extract_contract` |
| `pipeline/confidence.py` | `apply_drift_floor(uncertainty_score, drift_report)` |
| `pipeline/state.py` | `drift_report: Optional[dict]` field |
| `pipeline/orchestrator.py` | `aggregate_confidence_node` calls `apply_drift_floor`; recomputes verdict |
| `pipeline/hitl.py` | Logs HIGH/CRITICAL drift warning; `drift_report` in interrupt payload |
| `api/webhook.py` | `GET /runs/{run_id}` returns `drift_report` |
| `.env.example` | `DRIFT_WINDOW_DAYS`, `DRIFT_MEDIUM_THRESHOLD`, `DRIFT_HIGH_THRESHOLD`, `DRIFT_CRITICAL_THRESHOLD` |
| `tests/unit/test_drift_detector.py` | 18 unit tests |

---

## Configuration

```
DRIFT_WINDOW_DAYS=30          # lookback window for history query
DRIFT_MEDIUM_THRESHOLD=5      # distinct SHAs in window → MEDIUM drift
DRIFT_HIGH_THRESHOLD=10       # distinct SHAs in window → HIGH drift (floor=0.25)
DRIFT_CRITICAL_THRESHOLD=15   # distinct SHAs in window → CRITICAL drift (floor=0.40)
UNCERTAINTY_THRESHOLD=0.35    # (Feature 1) score above this triggers HITL
```

---

## HITL Reviewer Context

When the drift floor raises uncertainty above the HITL threshold, the interrupt
payload includes the full `drift_report`:

```json
{
  "type": "cross_repo_human_review",
  "uncertainty_score": 0.40,
  "uncertainty_verdict": "CRITICAL",
  "drift_report": {
    "service": "k11-payment-service",
    "window_days": 30,
    "change_count": 16,
    "change_velocity": 3.73,
    "drift_level": "CRITICAL",
    "last_changed_at": "2026-06-10T08:00:00+00:00",
    "uncertainty_floor": 0.40
  }
}
```

Reviewers can see at a glance that the service has changed 16 times in the last
30 days (3.7 changes/week) before deciding whether to approve.

---

## API Response

`GET /runs/{run_id}` now includes:

```json
{
  "uncertainty_score": 0.40,
  "uncertainty_verdict": "HIGH",
  "drift_report": {
    "service": "k11-payment-service",
    "window_days": 30,
    "change_count": 12,
    "change_velocity": 2.80,
    "drift_level": "HIGH",
    "last_changed_at": "2026-06-09T14:22:00+00:00",
    "uncertainty_floor": 0.25
  }
}
```

`drift_report` is `null` when drift detection was skipped (no contract history
available or Phase 1 error).

---

## Test Coverage

```
test_empty_history_returns_low_drift          zero-history safe path
test_single_change_low_drift                  one SHA = LOW
test_below_medium_threshold_is_low            boundary below MEDIUM
test_at_medium_threshold_is_medium            boundary: MEDIUM floor=0.10
test_at_high_threshold_is_high                boundary: HIGH floor=0.25
test_at_critical_threshold_is_critical        boundary: CRITICAL floor=0.40
test_duplicate_shas_counted_once              dedup correctness
test_velocity_calculated_per_week             velocity formula
test_last_changed_at_is_first_row             most-recent entry used
test_to_dict_contains_all_fields              serialisation contract
test_service_name_preserved                   metadata passthrough
test_custom_window_days                       non-default window
─── apply_drift_floor ───────────────────────────────────────────
test_none_report_returns_score_unchanged      no-op when no drift
test_floor_raises_low_score                   floor applied
test_floor_does_not_lower_higher_score        floor is a minimum
test_zero_floor_no_effect                     LOW drift = no change
test_critical_floor_applied                   end-to-end floor path
test_missing_floor_key_safe                   malformed report safe
─────────────────────────────────────────────────────────────────
Total  18 tests  ✅ all passing
```
