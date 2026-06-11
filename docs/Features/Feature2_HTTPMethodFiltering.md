# Feature 2 — HTTP Method Filtering in Consumer Matching

## Overview

Fixes a false-positive bug confirmed in the `sonnet+complete-graph` ablation run:
a consumer registered with a **POST** edge was incorrectly flagged as affected
by a **GET-only** contract change. The root cause was that `find_affected_consumers`
matched consumers by endpoint path pattern alone, ignoring HTTP methods entirely.

This feature adds method-intersection filtering so a consumer is only included
when its registered edge methods overlap with the HTTP methods that actually changed.

---

## The Bug

**File:** [graph/consumer_finder.py](graph/consumer_finder.py) (pre-fix, line 76–81)

```python
for changed in changed_endpoints:
    if ep_pattern and (ep_pattern in changed or changed in ep_pattern):
        consuming.append(changed)          # ← path match only, method ignored
        for m in edge_data.get("methods", ["GET"]):
            all_methods.add(m.upper())
```

**Scenario that triggered the false positive:**

```
k11-user-service  changes  GET /api/v2/users/{id}   (response field removed)

consumer_post registered edge: POST /api/v2/users/{id}   criticality=medium
```

Before the fix: `consumer_post` was included → LLM verdict: COMPATIBLE  
(but it cost tokens, added latency, and inflated impact radius)

After the fix: `consumer_post` is skipped — its POST edge does not intersect GET.

---

## How It Works

### Input Normalisation (`_normalise_specs`)

`find_affected_consumers` now accepts three input forms and normalises them all:

| Input form | Example | Result |
|------------|---------|--------|
| Plain path string | `"/api/v2/users/{id}"` | methods = `{}` (match all) |
| Prefixed string | `"GET /api/v2/users/{id}"` | methods = `{"GET"}` |
| Structured dict | `{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}` | methods = `{"GET"}` |
| Legacy Phase 1 dict | `{"endpoint": "/api/v2/users/{id}", "method": "GET"}` | methods = `{"GET"}` |

An **empty methods set** means "match all" — preserving backward-compatible behaviour
for callers that don't provide method information.

### Matching Logic

For each consumer edge, two conditions must both be true to include the consumer:

```
1. Path overlap:   ep_pattern ⊂ changed_pattern  OR  changed_pattern ⊂ ep_pattern
2. Method overlap: edge_methods ∩ changed_methods ≠ ∅
                   (skipped if changed_methods is empty — match-all mode)
```

Consumers with no matching edge (on both path AND method) are silently skipped
with a `DEBUG` log entry.

### Endpoint Spec Propagation

`DependencyGraphAgent` now extracts structured specs from `state["changed_endpoints"]`
instead of flat path strings:

```python
# Before
changed_endpoints = [ep.get("endpoint", "") for ep in state.get("changed_endpoints", [])]

# After
changed_endpoints = [
    {
        "pattern": ep.get("endpoint", ""),
        "methods": [ep["method"].upper()] if ep.get("method") else [],
    }
    for ep in state.get("changed_endpoints", [])
]
```

---

## Before / After

### Scenario: GET-only change to `/api/v2/users/{id}`

| Consumer | Edge Methods | Before Fix | After Fix |
|----------|-------------|------------|-----------|
| `k11-order-service` | GET | ✅ Included | ✅ Included |
| `k11-payment-service` | GET, POST | ✅ Included | ✅ Included |
| `k11-notification-svc` | POST | ✅ Included (false positive) | ❌ Excluded |

---

## Files Changed

| File | Change |
|------|--------|
| `graph/consumer_finder.py` | `_normalise_specs()` + method-intersection filter in `find_affected_consumers` |
| `agents/dependency_graph_agent.py` | Structured `{pattern, methods}` dicts instead of flat path strings |
| `tests/unit/test_consumer_finder.py` | 19 unit tests |

---

## Test Coverage

```
TestMethodFiltering          6 tests — core method-intersection behaviour
TestBackwardCompatibility    5 tests — all 3 input forms + mixed input
TestNormaliseSpecs           5 tests — _normalise_specs edge cases
TestEdgeMetadata             3 tests — criticality and methods propagation
────────────────────────────────────────────────────────────────────────
Total                       19 tests  ✅ all passing
```

---

## Impact on Evaluation Results

The `sonnet+complete-graph` ablation showed **1 false positive** (8T/1F HITL)
caused by a POST consumer edge matching a GET-only change. With this fix:

- Impact radius is accurate — only genuinely affected consumers are validated
- LLM call count is reduced (no tokens spent on unaffected consumers)
- HITL false trigger rate improves — fewer spurious UNCERTAIN verdicts from irrelevant consumers
