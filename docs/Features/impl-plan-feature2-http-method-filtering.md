# Implementation Plan: Feature 2 — HTTP Method Filtering in Consumer Matching
**Branch:** `feature/http-method-filtering`

---

## Problem

`graph/consumer_finder.py::find_affected_consumers` matches consumer edges to
changed endpoints by **path pattern substring only**. It ignores HTTP methods.

**Consequence (confirmed in ablation run):** A consumer registered with a POST
edge on `/api/v2/users/{id}` is flagged as affected when only the GET handler
changes. This produced a false positive in the `sonnet+complete-graph` ablation.

**Root cause (line 76–81 in consumer_finder.py):**
```python
for changed in changed_endpoints:
    if ep_pattern and (ep_pattern in changed or changed in ep_pattern):
        consuming.append(changed)
        for m in edge_data.get("methods", ["GET"]):
            all_methods.add(m.upper())
```
No check: does `edge_data["methods"]` overlap with the methods that actually changed?

---

## Files to Change

| File | Change Type |
|------|-------------|
| `graph/consumer_finder.py` | Add method-intersection filter in `find_affected_consumers` |
| `pipeline/phase1.py` | Propagate per-endpoint changed methods into state |
| `tests/unit/test_consumer_finder.py` | **New** — method filtering tests |

---

## Step 1 — Propagate Changed Operation Methods (pipeline/phase1.py)

The `changed_endpoints` list in state contains dicts from Phase 1. Currently the
`DependencyGraphAgent` collapses them to plain strings:

```python
# dependency_graph_agent.py line 72 — CURRENT
changed_endpoints = [ep.get("endpoint", "") for ep in state.get("changed_endpoints", [])]
```

Change this to pass structured records so the consumer finder knows which methods changed:

```python
# NEW — keep method info alongside the path
changed_endpoint_specs = [
    {
        "pattern": ep.get("endpoint", ""),
        "methods": [ep.get("method", "GET").upper()] if ep.get("method") else [],
    }
    for ep in state.get("changed_endpoints", [])
]
```

Then pass `changed_endpoint_specs` to `find_affected_consumers` instead of flat strings.

---

## Step 2 — Method-Intersection Filter (graph/consumer_finder.py)

### New function signature

```python
def find_affected_consumers(
    graph: DependencyGraph,
    provider: str,
    changed_endpoints: list[str | dict],   # accepts both old str and new dict form
) -> list[ConsumerContext]:
```

### Normalise input

```python
def _normalise_specs(changed_endpoints: list) -> list[dict]:
    """
    Accept either flat strings ("GET /api/v2/users/{id}") or
    dicts ({"pattern": "/api/v2/users/{id}", "methods": ["GET"]}).
    Returns list of {"pattern": str, "methods": set[str]}.
    """
    specs = []
    for ep in changed_endpoints:
        if isinstance(ep, str):
            parts = ep.split(" ", 1)
            if len(parts) == 2 and parts[0].isupper():
                specs.append({"pattern": parts[1], "methods": {parts[0]}})
            else:
                specs.append({"pattern": ep, "methods": set()})  # empty = match all methods
        else:
            specs.append({
                "pattern": ep.get("pattern", ep.get("endpoint", "")),
                "methods": {m.upper() for m in ep.get("methods", [])},
            })
    return specs
```

### Updated matching loop

```python
specs = _normalise_specs(changed_endpoints)

for consumer in all_down:
    consuming: list[str] = []
    all_methods: set[str] = set()
    max_criticality = "low"

    for _, target, edge_data in graph._g.edges(consumer, data=True):
        if target != provider:
            continue

        ep_pattern      = edge_data.get("endpoint_pattern", "")
        edge_methods    = {m.upper() for m in edge_data.get("methods", ["GET"])}
        edge_crit       = edge_data.get("criticality", "medium")

        for spec in specs:
            changed_pattern = spec["pattern"]
            changed_methods = spec["methods"]

            # Path must match
            if not (ep_pattern and (ep_pattern in changed_pattern
                                    or changed_pattern in ep_pattern)):
                continue

            # Method must intersect — empty changed_methods means "all methods"
            if changed_methods and not edge_methods.intersection(changed_methods):
                continue   # ← THE KEY FIX

            consuming.append(changed_pattern)
            all_methods.update(edge_methods)

        if crit_rank.get(edge_crit, 0) > crit_rank.get(max_criticality, 0):
            max_criticality = edge_crit

    if not consuming:
        # This consumer has no edges that match both path AND method — skip it
        continue

    consumers.append(ConsumerContext(...))
```

---

## Step 3 — Update DependencyGraphAgent (agents/dependency_graph_agent.py)

```python
# BEFORE
changed_endpoints = [ep.get("endpoint", "") for ep in state.get("changed_endpoints", [])]

# AFTER
changed_endpoints = [
    {
        "pattern": ep.get("endpoint", ""),
        "methods": [ep.get("method", "GET").upper()] if ep.get("method") else [],
    }
    for ep in state.get("changed_endpoints", [])
]
```

---

## Step 4 — Tests (tests/unit/test_consumer_finder.py)

```python
"""Tests for HTTP method filtering in find_affected_consumers."""
import pytest
from graph.dependency_graph import ConsumptionEdge, DependencyGraph, ServiceNode
from graph.consumer_finder import find_affected_consumers


def _build_graph():
    g = DependencyGraph()
    for svc in ["provider", "consumer_get", "consumer_post", "consumer_any"]:
        g.add_service(ServiceNode(name=svc, repo=f"org/{svc}"))
    g.add_consumption(ConsumptionEdge(
        consumer="consumer_get",  provider="provider",
        endpoint_pattern="/api/v2/users/{id}", methods=["GET"], criticality="high",
    ))
    g.add_consumption(ConsumptionEdge(
        consumer="consumer_post", provider="provider",
        endpoint_pattern="/api/v2/users/{id}", methods=["POST"], criticality="medium",
    ))
    g.add_consumption(ConsumptionEdge(
        consumer="consumer_any",  provider="provider",
        endpoint_pattern="/api/v2/users/{id}", methods=["GET", "POST"], criticality="low",
    ))
    return g


def test_get_change_only_affects_get_consumer():
    g = _build_graph()
    result = find_affected_consumers(
        g, "provider",
        [{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}],
    )
    names = {c.consumer for c in result}
    assert "consumer_get"  in names
    assert "consumer_post" not in names   # ← the false positive fix
    assert "consumer_any"  in names


def test_post_change_only_affects_post_consumer():
    g = _build_graph()
    result = find_affected_consumers(
        g, "provider",
        [{"pattern": "/api/v2/users/{id}", "methods": ["POST"]}],
    )
    names = {c.consumer for c in result}
    assert "consumer_post" in names
    assert "consumer_get"  not in names
    assert "consumer_any"  in names


def test_no_method_filter_matches_all():
    """Empty methods list = match all (backward compatibility)."""
    g = _build_graph()
    result = find_affected_consumers(
        g, "provider",
        [{"pattern": "/api/v2/users/{id}", "methods": []}],
    )
    assert len(result) == 3


def test_flat_string_input_still_works():
    """Backward compatibility: plain strings without method prefix."""
    g = _build_graph()
    result = find_affected_consumers(g, "provider", ["/api/v2/users/{id}"])
    assert len(result) == 3


def test_method_prefixed_string_parsed_correctly():
    """'GET /api/v2/users/{id}' format parsed from endpoint string."""
    g = _build_graph()
    result = find_affected_consumers(
        g, "provider", ["GET /api/v2/users/{id}"]
    )
    names = {c.consumer for c in result}
    assert "consumer_get" in names
    assert "consumer_post" not in names
```

---

## Environment Variables

None required — the fix is structural.

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `_normalise_specs` + method filter in consumer_finder.py | 1 hour |
| `DependencyGraphAgent` input update | 30 min |
| Tests | 1 hour |
| **Total** | **~2.5 hours** |
