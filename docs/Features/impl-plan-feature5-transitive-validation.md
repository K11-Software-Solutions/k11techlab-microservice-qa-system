# Implementation Plan: Feature 5 — Transitive Consumer Validation
**Branch:** `feature/transitive-consumer-validation`

---

## Overview

The dependency graph already returns all transitive consumers via
`downstream_consumers()` (uses `nx.descendants`). **However**, the edge metadata
lookup in `find_affected_consumers` only works for direct (one-hop) edges:

```python
for _, target, edge_data in graph._g.edges(consumer, data=True):
    if target != provider:
        continue   # ← transitive consumers never satisfy this
```

For a two-hop consumer `C → B → A` when `A` changes:
- `C`'s edges point to `B`, not `A` — so `target != provider` is always true
- `consuming_endpoints` falls back to all `changed_endpoints` (over-broad)
- `edge_methods` defaults to `["GET"]` (wrong)
- `edge_criticality` defaults to `"low"` (under-estimated)

**Result:** Transitive consumers are validated with incorrect context, producing
unreliable COMPATIBLE/BREAKING verdicts. The LLM is told the consumer uses `GET`
when it may use `POST`, and that it's `"low"` criticality when the actual chain
is `"critical"`.

---

## Design

For a transitive consumer, traverse the shortest path in the graph
(`C → B → A`) and collect edge metadata along the path. The criticality
of the transitive dependency is the **maximum** criticality along the path.

Add `hop_depth: int` to `ConsumerContext` so Phase 3 agents can reflect
on whether they are validating a direct or transitive dependency.

Add `TRANSITIVE_VALIDATION_ENABLED` and `MAX_TRANSITIVE_DEPTH` env vars.

---

## Files to Change

| File | Change Type |
|------|-------------|
| `graph/consumer_finder.py` | Add transitive path traversal; add `hop_depth` to `ConsumerContext` |
| `graph/dependency_graph.py` | Add `shortest_path_edges()` helper |
| `pipeline/state.py` | No change needed — `downstream_consumers` already serialises all consumers |
| `pipeline/phase3.py` | Pass `hop_depth` into compliance agent prompt |
| `agents/contract_compliance_agent.py` | Include hop_depth context in LLM prompt |
| `tests/unit/test_transitive_consumers.py` | **New** |

---

## Step 1 — Graph Helper (graph/dependency_graph.py)

```python
def shortest_path_edges(
    self,
    source: str,
    target: str,
) -> list[dict]:
    """
    Return edge data dicts along the shortest path from source to target.
    Returns [] if no path exists.

    Example: source=C, target=A in graph C→B→A
    Returns [edge_data(C→B), edge_data(B→A)]
    """
    try:
        path = nx.shortest_path(self._g, source=source, target=target)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []

    edges = []
    for u, v in zip(path[:-1], path[1:]):
        edges.append(self._g.edges[u, v])
    return edges
```

---

## Step 2 — ConsumerContext (graph/consumer_finder.py)

Add `hop_depth` field:

```python
@dataclass
class ConsumerContext:
    consumer:            str
    repo:                str
    team:                str
    slack_channel:       str
    consuming_endpoints: list[str]          = field(default_factory=list)
    edge_methods:        list[str]          = field(default_factory=lambda: ["GET"])
    edge_criticality:    str                = "medium"
    is_direct:           bool               = True
    hop_depth:           int                = 1   # 1 = direct, 2+ = transitive
```

---

## Step 3 — Transitive Path Metadata (graph/consumer_finder.py)

Replace the direct-edge lookup with a path-aware version:

```python
import os

TRANSITIVE_VALIDATION_ENABLED = os.getenv("TRANSITIVE_VALIDATION_ENABLED", "true").lower() != "false"
MAX_TRANSITIVE_DEPTH = int(os.getenv("MAX_TRANSITIVE_DEPTH", "3"))


def _resolve_consumer_context(
    graph: DependencyGraph,
    consumer: str,
    provider: str,
    changed_endpoints: list[str],
    is_direct: bool,
) -> ConsumerContext | None:
    """
    Resolve edge metadata for a consumer, handling both direct and transitive cases.
    Returns None if the consumer exceeds MAX_TRANSITIVE_DEPTH.
    """
    node_data = graph._g.nodes.get(consumer, {})
    crit_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}

    if is_direct:
        # Original direct-edge logic
        consuming: list[str] = []
        all_methods: set[str] = set()
        max_criticality = "low"

        for _, target, edge_data in graph._g.edges(consumer, data=True):
            if target != provider:
                continue
            ep_pattern = edge_data.get("endpoint_pattern", "")
            for changed in changed_endpoints:
                if ep_pattern and (ep_pattern in changed or changed in ep_pattern):
                    consuming.append(changed)
                    for m in edge_data.get("methods", ["GET"]):
                        all_methods.add(m.upper())
            edge_crit = edge_data.get("criticality", "medium")
            if crit_rank.get(edge_crit, 0) > crit_rank.get(max_criticality, 0):
                max_criticality = edge_crit

        return ConsumerContext(
            consumer=consumer, repo=node_data.get("repo", ""),
            team=node_data.get("team", ""), slack_channel=node_data.get("slack_channel", ""),
            consuming_endpoints=consuming if consuming else changed_endpoints,
            edge_methods=sorted(all_methods) if all_methods else ["GET"],
            edge_criticality=max_criticality,
            is_direct=True, hop_depth=1,
        )

    else:
        # Transitive: walk the shortest path C→...→provider
        if not TRANSITIVE_VALIDATION_ENABLED:
            return None

        path_edges = graph.shortest_path_edges(consumer, provider)
        hop_depth  = len(path_edges)

        if hop_depth > MAX_TRANSITIVE_DEPTH:
            logger.debug(
                "Skipping transitive consumer %s (depth %d > max %d)",
                consumer, hop_depth, MAX_TRANSITIVE_DEPTH,
            )
            return None

        # Aggregate methods and criticality across the path
        all_methods: set[str] = set()
        max_criticality = "low"
        for edge_data in path_edges:
            for m in edge_data.get("methods", ["GET"]):
                all_methods.add(m.upper())
            edge_crit = edge_data.get("criticality", "medium")
            if crit_rank.get(edge_crit, 0) > crit_rank.get(max_criticality, 0):
                max_criticality = edge_crit

        return ConsumerContext(
            consumer=consumer, repo=node_data.get("repo", ""),
            team=node_data.get("team", ""), slack_channel=node_data.get("slack_channel", ""),
            consuming_endpoints=changed_endpoints,   # transitive: assume all changed endpoints relevant
            edge_methods=sorted(all_methods) if all_methods else ["GET"],
            edge_criticality=max_criticality,
            is_direct=False, hop_depth=hop_depth,
        )
```

Update `find_affected_consumers` to use this helper:

```python
def find_affected_consumers(
    graph: DependencyGraph,
    provider: str,
    changed_endpoints: list[str],
) -> list[ConsumerContext]:
    direct   = set(graph.direct_consumers(provider))
    all_down = set(graph.downstream_consumers(provider))

    consumers: list[ConsumerContext] = []
    for consumer in all_down:
        ctx = _resolve_consumer_context(
            graph, consumer, provider, changed_endpoints,
            is_direct=(consumer in direct),
        )
        if ctx is not None:
            consumers.append(ctx)

    # Sort: direct first, then by hop_depth, then criticality
    crit_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    consumers.sort(key=lambda c: (
        0 if c.is_direct else c.hop_depth,
        crit_order.get(c.edge_criticality, 2),
    ))

    logger.info(
        "Found %d affected consumers for provider=%s (%d direct, %d transitive)",
        len(consumers), provider,
        sum(1 for c in consumers if c.is_direct),
        sum(1 for c in consumers if not c.is_direct),
    )
    return consumers
```

---

## Step 4 — Serialise hop_depth (graph/consumer_finder.py)

```python
def serialise_consumers(consumers: list[ConsumerContext]) -> list[dict]:
    return [
        {
            ...
            "hop_depth":   c.hop_depth,   # ← add this field
        }
        for c in consumers
    ]
```

---

## Step 5 — Phase 3 LLM Prompt (agents/contract_compliance_agent.py)

Pass hop_depth context into the compliance prompt:

```python
async def run(
    self,
    consumer: str,
    contract_diff: dict,
    usage_patterns: dict,
    hop_depth: int = 1,           # ← new param
) -> ComplianceResult:
    transitivity_note = (
        ""
        if hop_depth == 1
        else (
            f"\nNote: {consumer} is a TRANSITIVE consumer at depth {hop_depth} "
            f"(it depends on an intermediate service that depends on the provider). "
            f"Its exposure to this change may be indirect — consider this in your verdict."
        )
    )
    prompt = (
        f"Consumer service: {consumer}{transitivity_note}\n\n"
        f"Contract diff (breaking changes only):\n{json.dumps(contract_diff, indent=2)}\n\n"
        f"Consumer usage patterns:\n{json.dumps(usage_patterns, indent=2)}"
    )
    ...
```

In `pipeline/phase3.py`, extract and forward `hop_depth`:

```python
result = await agent.run(
    consumer=consumer_ctx["consumer"],
    contract_diff={...},
    usage_patterns={...},
    hop_depth=consumer_ctx.get("hop_depth", 1),   # ← forward
)
```

---

## Environment Variables

```
TRANSITIVE_VALIDATION_ENABLED=true   # set to false to skip transitive consumers entirely
MAX_TRANSITIVE_DEPTH=3               # consumers > this many hops are skipped
```

Add to `.env.example`.

---

## Tests (tests/unit/test_transitive_consumers.py)

| Test | Covers |
|------|--------|
| `test_direct_consumer_hop_depth_1` | Direct consumer gets hop_depth=1 |
| `test_two_hop_consumer_depth_2` | C→B→A: C gets hop_depth=2 |
| `test_three_hop_consumer_depth_3` | Three-hop chain |
| `test_beyond_max_depth_excluded` | hop_depth > MAX excluded |
| `test_transitive_disabled_excludes_indirect` | TRANSITIVE_VALIDATION_ENABLED=false |
| `test_transitive_criticality_is_path_max` | Max criticality along path |
| `test_transitive_methods_union` | Methods union across path edges |
| `test_sort_order_direct_before_transitive` | Direct consumers sorted first |
| `test_serialise_includes_hop_depth` | serialise_consumers emits hop_depth |

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `shortest_path_edges()` in dependency_graph.py | 30 min |
| `hop_depth` field + `_resolve_consumer_context()` | 2 hours |
| Phase 3 / compliance agent prompt update | 45 min |
| Tests | 2 hours |
| **Total** | **~5.25 hours** |
