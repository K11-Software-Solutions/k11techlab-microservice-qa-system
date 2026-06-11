# Feature 5 — Transitive Consumer Validation

## Overview

Fixes incorrect context (methods, criticality) for transitive consumers in the
parallel compliance validation phase. Previously, only direct (one-hop)
consumers of a changed provider received accurate edge metadata. Transitive
consumers — those connected via intermediate services — were validated with
wrong methods (`GET` by default) and wrong criticality (`low`), producing
unreliable COMPATIBLE/BREAKING verdicts.

Feature 5 resolves this by traversing the shortest path through the dependency
graph and aggregating metadata from every hop along the chain.

---

## The Problem

For a two-hop chain `C → B → A` when provider `A` changes:

```
C's outgoing edges: [C → B]   (C has no direct edge to A)

Old behaviour:
  find_affected_consumers(provider=A):
    for edge in C.edges:          # C → B only
        if edge.target != A:      # always True for C
            continue
    consuming = []                # nothing matched
    → C skipped entirely
```

Before Feature 5, transitive consumers were silently dropped. `B` was validated
correctly (direct edge to `A`), but `C` — which depends on `B` and is therefore
indirectly affected by `A`'s change — was never validated at all.

**After Feature 5:**

```
C → B → A   (hop_depth = 2)
  path_edges = [edge(C→B), edge(B→A)]
  methods    = union({"GET"}, {"POST"})   = ["GET", "POST"]
  criticality = max("critical", "high")  = "critical"
  → C validated with correct context
```

---

## Design

### `shortest_path_edges(source, target)`

Added to `DependencyGraph`. Uses `nx.shortest_path(graph, source, target)` on
the original directed graph (edges go consumer → provider). For `C → B → A`,
returns `[edge_data(C→B), edge_data(B→A)]`.

### `_resolve_consumer_context()`

New internal helper in `consumer_finder.py` that handles two cases:

| Consumer type | Logic |
|---------------|-------|
| **Direct** (`hop_depth=1`) | Same method+path spec filtering as Feature 2 |
| **Transitive** (`hop_depth≥2`) | Walk shortest path; union methods; max criticality |

Direct consumers are unchanged — Feature 2's method filtering is preserved.

### `hop_depth` in `ConsumerContext`

```python
@dataclass
class ConsumerContext:
    ...
    hop_depth: int = 1   # 1 = direct, 2+ = transitive
```

Serialised into `downstream_consumers` state dicts and forwarded to Phase 3.

### LLM prompt annotation

When `hop_depth > 1`, `ContractComplianceAgent.run()` appends a transitivity
note to the prompt:

```
Note: k11-report-service is a TRANSITIVE consumer at depth 2
(it depends on an intermediate service that depends on the provider).
Its exposure to this change may be indirect — consider this in your verdict.
```

This allows the model to calibrate its BREAKING/UNCERTAIN/COMPATIBLE verdict
appropriately — a transitive consumer is less likely to break directly but
still warrants review.

---

## Flow

```
Phase 2 — traverse_graph_node
  └─ find_affected_consumers()
       ├─ direct consumers   → _resolve_consumer_context(is_direct=True)
       │    └─ method+path spec filtering (Feature 2)
       └─ transitive consumers → _resolve_consumer_context(is_direct=False)
            └─ shortest_path_edges(consumer, provider)
            └─ union methods + max criticality across path

Phase 3 — dispatch_consumers (Send fan-out)
  └─ validate_consumer_node
       └─ ContractComplianceAgent.run(hop_depth=N)
            └─ transitivity note injected into LLM prompt when N > 1
```

---

## Sort Order

Consumers are returned sorted:

1. Direct consumers first (hop_depth=1)
2. Transitive consumers, sorted by hop_depth ascending
3. Within same depth, higher criticality first

---

## Configuration

```
TRANSITIVE_VALIDATION_ENABLED=true   # set to false to skip transitive consumers entirely
MAX_TRANSITIVE_DEPTH=3               # consumers > this many hops from provider are skipped
```

When `TRANSITIVE_VALIDATION_ENABLED=false`, only direct consumers are validated
(equivalent to pre-Feature-5 behaviour, but with correct methods/criticality).

`MAX_TRANSITIVE_DEPTH` caps the validation cost in large dependency graphs.
Consumers at depth 4+ in a chain with default `MAX_TRANSITIVE_DEPTH=3` are
skipped with a DEBUG log entry.

---

## Files Changed

| File | Change |
|------|--------|
| `graph/dependency_graph.py` | `shortest_path_edges(source, target)` method |
| `graph/consumer_finder.py` | `hop_depth` on `ConsumerContext`; `_resolve_consumer_context()`; updated `find_affected_consumers()`; `serialise_consumers()` emits `hop_depth`; env vars |
| `agents/contract_compliance_agent.py` | `run()` accepts `hop_depth`; transitivity note in LLM prompt |
| `pipeline/phase3.py` | Forwards `hop_depth` from `consumer_ctx` to `agent.run()` and `usage_patterns` |
| `.env.example` | `TRANSITIVE_VALIDATION_ENABLED`, `MAX_TRANSITIVE_DEPTH` |
| `tests/unit/test_transitive_consumers.py` | 23 unit tests |

---

## Test Coverage

```
── shortest_path_edges ──────────────────────────────────────────
test_direct_edge_returns_one_item         single hop
test_two_hop_returns_two_edges            C→B→A path length
test_three_hop_returns_three_edges        E→C→B→A path length
test_no_path_returns_empty                reverse direction — no path
test_missing_node_returns_empty           unknown node safe
test_same_source_and_target_returns_empty trivial self-path
── Direct consumers ─────────────────────────────────────────────
test_direct_consumer_hop_depth_1          hop_depth=1 on direct
test_direct_consumer_included             D and B both returned
── Transitive depth 2 ───────────────────────────────────────────
test_two_hop_consumer_hop_depth_2         C gets hop_depth=2
test_transitive_consumer_is_not_direct    is_direct=False
test_transitive_criticality_is_path_max   critical > high → critical
test_transitive_methods_union             {GET}∪{POST} = {GET,POST}
── Transitive depth 3 ───────────────────────────────────────────
test_three_hop_consumer_depth_3           E gets hop_depth=3
test_three_hop_criticality_max            critical wins across 3 hops
test_three_hop_methods_union              {DELETE,GET,POST}
── Depth cap ────────────────────────────────────────────────────
test_beyond_max_depth_excluded            depth 3 > MAX=2 → skipped
test_at_max_depth_included                depth 3 == MAX=3 → included
── Feature flag ─────────────────────────────────────────────────
test_transitive_disabled_excludes_indirect C excluded when flag=false
test_transitive_enabled_includes_indirect  C included when flag=true
── Sort order ───────────────────────────────────────────────────
test_sort_order_direct_before_transitive  direct indices < transitive
── Serialisation ────────────────────────────────────────────────
test_serialise_includes_hop_depth         all items have hop_depth key
test_serialise_direct_hop_depth_is_1      D serialised with hop_depth=1
test_serialise_transitive_hop_depth_is_2  C serialised with hop_depth=2
─────────────────────────────────────────────────────────────────
Total  23 tests  ✅ all passing
```
