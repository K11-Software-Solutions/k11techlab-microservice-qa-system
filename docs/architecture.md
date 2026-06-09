# Architecture — K11tech Microservice QA System

## Core Concept: The Blind Spot in Single-Repo QA

Every existing CI/CD quality tool — including Paper 1's K11tech Agentic AI QA System —
operates within a single repository boundary. A change that passes all local quality
gates can still cause distributed system failures if it breaks an API contract
consumed by another service.

This system fills that blind spot by maintaining a cross-repository view of
API contracts and service dependencies, enabling impact analysis at PR time
before any code is merged.

## Four-Phase Pipeline

### Phase 1 — Contract Extraction
Triggered when a PR is opened in any monitored service repository.

1. `extract_contract` — parses the PR branch for API contract files:
   - OpenAPI 3.x (`openapi.yaml`, `swagger.yaml`)
   - gRPC proto files (`*.proto`)
   - GraphQL schemas (`schema.graphql`)
2. `fetch_previous_contract` — retrieves the current contract version from
   the Contract Registry MCP for comparison
3. `diff_contracts` — produces a structured ContractDiff with breaking and
   non-breaking changes classified
4. If no contract files changed → pipeline exits early (no impact possible)

### Phase 2 — Dependency Graph Traversal
Given the changed endpoints from Phase 1:

1. `load_graph` — fetches the current dependency graph from Graph Store MCP
2. `find_consumers` — traverses the graph to find all services that consume
   the changed endpoints (direct + transitive)
3. `score_impact` — computes impact_score and impact_radius from consumer
   count and edge criticality weights
4. `check_hitl` — if impact_score >= IMPACT_HITL_THRESHOLD OR
   breaking_consumers >= BREAKING_CONSUMER_HITL_COUNT → set hitl_required=True

### Phase 3 — Parallel Consumer Validation
For each identified downstream consumer:

1. `fetch_usage_patterns` — retrieves how the consumer actually calls the
   provider (from GitHub MCP: search for API calls in consumer repo)
2. `ContractComplianceAgent.run()` — LLM validates whether the proposed
   change will break the consumer given its usage patterns
3. All consumers are validated in parallel via LangGraph Send API
4. Results accumulate via list-append reducers on compliance_results

### Phase 4 — Impact Report
1. `aggregate_results` — combines all compliance results
2. `generate_report` — produces a structured impact report listing:
   - Breaking consumers with violation details
   - Compatible consumers confirmed safe
   - Uncertain consumers flagged for manual review
3. `file_cross_repo_issues` — for each breaking consumer, files a GitHub
   issue in BOTH the provider repo AND the consumer repo
4. `notify_teams` — Slack notifications to affected service team channels

## Contract Registry MCP

The Contract Registry is a persistent MCP server (port 8010) exposing:

| Tool | Purpose |
|------|---------|
| `register_service` | Register a new service with its repo and contract path |
| `store_contract` | Store a versioned contract snapshot |
| `get_contract` | Retrieve the current contract for a service |
| `get_contract_history` | Get all versions of a service's contract |
| `search_consumers` | Find all services that consume a given endpoint |

Storage: SQLite with contract versions as JSON blobs. Versioned by git SHA.

## Dependency Graph MCP

The Graph Store MCP (port 8011) wraps the NetworkX dependency graph:

| Tool | Purpose |
|------|---------|
| `add_service` | Add a new service node to the graph |
| `record_consumption` | Record that service A consumes service B's endpoint |
| `get_downstream` | Get all downstream consumers of a service |
| `get_impact_score` | Compute impact score for a set of changed endpoints |
| `export_graph` | Export full graph as node-link JSON |

## HITL Gate — Cross-Repo Escalation

The cross-repo HITL gate has two independent triggers:

1. **Impact score trigger**: `impact_score >= IMPACT_HITL_THRESHOLD` (default: 0.60)
   — fires when a high proportion of critical consumers are affected

2. **Breaking consumer count trigger**: `breaking_consumers >= N` (default: 2)
   — fires when at least N consumers are confirmed breaking regardless of score

Either trigger suspends the pipeline and notifies the PR author AND all
affected service team owners via Slack. The reviewer can approve (proceed
with the breaking change, consumers must update) or reject (change the API
to be backwards compatible).

## Relation to Paper 1 Pipeline

The two pipelines run in parallel and combine verdicts:

```
PR opened
    ├──▶ Paper 1 pipeline (single-repo QA)    ──▶ verdict_local
    └──▶ Paper 3 pipeline (cross-repo impact) ──▶ verdict_cross_repo

Final decision = APPROVE only if BOTH verdicts = APPROVE
```

Integration is via a combined webhook endpoint that dispatches to both
pipelines and awaits both verdicts before posting the final GitHub check.
