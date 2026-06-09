# Dependency Graph Model

## Concept

The dependency graph is a directed graph where:
- **Nodes** are microservices
- **Edges** point FROM consumer TO provider: `order-service → user-service` means "order-service depends on user-service"

When a provider's contract changes, we traverse the graph in the **reverse direction** to find all consumers affected.

## Example Topology

```
user-service (provider)
    ↑ consumes
order-service ──────────────→ analytics-svc
    ↑ consumes                (order-service is provider here)
payment-service

notification-svc → user-service
gateway-service  → user-service
gateway-service  → order-service
gateway-service  → payment-service
```

**Impact when user-service changes:**
- Direct: order-service, payment-service, notification-svc, gateway-service
- Transitive: analytics-svc (through order-service)
- Total impact radius: 5

## Edge Properties

Each edge carries:

| Property | Values | Effect |
|----------|--------|--------|
| `endpoint_pattern` | `/api/v2/users/*` | Which endpoint the consumer calls |
| `methods` | `["GET", "POST"]` | HTTP methods consumed |
| `criticality` | `low \| medium \| high \| critical` | Weight in impact score calculation |

## Impact Score Calculation

Impact score ∈ [0.0, 1.0] blends two signals:

1. **Graph-based score** — weighted sum of consuming edge criticalities for changed endpoints, normalised to assume a maximum of 10 critical consumers.
2. **Severity boost** — 30% contribution from the worst-case breaking change severity if breaking changes exist.

Formula:
```
graph_score = sum(criticality_weight for edges touching changed endpoints) / 10.0
impact_score = min(1.0, graph_score * 0.70 + worst_severity * 0.30)
```

## HITL Thresholds

Two independent triggers:

```python
IMPACT_HITL_THRESHOLD          = 0.60   # impact_score threshold
BREAKING_CONSUMER_HITL_COUNT   = 2      # minimum breaking consumers
```

Either trigger causes the pipeline to pause for human review.

## Registering Consumption Edges

```bash
curl -X POST http://localhost:9003/graph/consumption \
  -H "Content-Type: application/json" \
  -d '{
    "consumer":         "order-service",
    "provider":         "user-service",
    "endpoint_pattern": "/api/v2/users/{id}",
    "methods":          ["GET"],
    "criticality":      "high"
  }'
```

## Persistence

The graph is stored as a serialised node-link JSON snapshot in SQLite via the Graph Store MCP. Every mutation (add_service, record_consumption) triggers a new snapshot. The latest snapshot is restored on startup.

The Graph Store MCP exposes REST/MCP endpoints for the pipeline to call, keeping the graph state decoupled from individual pipeline runs.
