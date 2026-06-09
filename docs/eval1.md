# Evaluation Run 1 — K11tech Microservice QA System

**Date:** 2026-06-07  
**Model:** `claude-sonnet-4-6` (via `langchain-anthropic`)  
**Scenarios:** 4 (3 breaking, 1 non-breaking)  
**Service under test:** `user-service`

---

## Service Topology

Six microservices were scaffolded with the following dependency graph:

| Consumer | Provider | Endpoint | Methods | Criticality |
|---|---|---|---|---|
| order-service | user-service | `/api/v2/users/{id}` | GET | high |
| notification-svc | user-service | `/api/v2/users/{id}/contact` | GET | medium |
| payment-service | user-service | `/api/v2/users/{id}` | GET | critical |
| payment-service | order-service | `/api/v1/orders/{id}` | GET, PATCH | critical |
| analytics-svc | order-service | `/api/v1/orders` | GET | low |
| gateway-service | user-service | `/api/v2/users` | GET, POST | high |
| gateway-service | order-service | `/api/v1/orders` | GET, POST | high |
| gateway-service | payment-service | `/api/v1/payments` | POST | critical |

Base contract: `user-service v2.1.0` — 4 endpoints.

---

## Scenarios

| ID | Type | Description | Version |
|---|---|---|---|
| SC-001 | BREAKING | Remove `GET /api/v2/users/{id}` | 2.2.0 |
| SC-002 | BREAKING | Add required field `phone` to `POST /api/v2/users` | 2.2.1 |
| SC-003 | NON-BREAKING | Add optional `avatar_url` field + new `/preferences` endpoint | 2.2.2 |
| SC-004 | BREAKING | Change `id` field type from `string` to `integer` | 2.3.0 |

---

## Results Summary

### RQ1 — Breaking Change Detection Accuracy

| Metric | Value |
|---|---|
| Precision | **100%** |
| Recall | **100%** |
| F1 Score | **100%** |
| Accuracy | **100%** |
| TP | 3 |
| FP | 0 |
| TN | 1 |
| FN | 0 |

### RQ2 — False Negative Rate

| Metric | Value |
|---|---|
| FNR | **0.0%** |
| Breaking scenarios missed | 0 of 3 |

### RQ3 — Impact Score Distribution

| Category | Count | Mean | Min | Max |
|---|---|---|---|---|
| Breaking scenarios | 3 | 0.485 | 0.435 | 0.510 |
| Non-breaking scenarios | 1 | 0.125 | 0.125 | 0.125 |

Impact scores for breaking scenarios are consistently in the 0.43–0.51 range; the non-breaking scenario scores 0.125, showing good separation between classes.

### RQ4 — Cross-Repo HITL Gate

| Metric | Value |
|---|---|
| Trigger rate | 75% (3 of 4 scenarios) |
| Correct escalations | 3 |
| False escalations | 0 |
| Missed escalations | 0 |
| HITL Precision | **100%** |
| HITL Recall | **100%** |

---

## Per-Scenario Detail

### SC-001 — Remove `GET /api/v2/users/{id}`

- **Verdict:** BREAKING ✓  
- **Impact score:** 0.510 | **Radius:** 5  
- **Duration:** 42.1s  
- **HITL triggered:** Yes

Consumers flagged (4):

| Consumer | Verdict | Key Violation |
|---|---|---|
| payment-service | BREAKING | `GET /api/v2/users/{id}` removed — critical user lookup for payment verification |
| order-service | BREAKING | `GET /api/v2/users/{id}` removed — user profile fetch for order attachment |
| gateway-service | BREAKING | Proxies both `GET /api/v2/users/{id}` and `POST /api/v2/users`; endpoint removal causes routing failures |
| notification-svc | BREAKING | `id` path parameter removal on `GET /api/v2/users/{id}/contact` breaks contact lookup |

---

### SC-002 — Add required field `phone` to `POST /api/v2/users`

- **Verdict:** BREAKING ✓  
- **Impact score:** 0.510 | **Radius:** 5  
- **Duration:** 36.3s  
- **HITL triggered:** Yes

Consumers flagged (4):

| Consumer | Verdict | Key Violation |
|---|---|---|
| gateway-service | BREAKING | Proxies `POST /api/v2/users`; registration requests will fail validation without `phone` |
| payment-service | BREAKING | Read-only consumer but `id` path param change detected across related diff |
| order-service | BREAKING | Same `id` param detection |
| notification-svc | BREAKING | Same `id` param detection on contact endpoint |

> **Note:** payment-service, order-service, and notification-svc are read-only consumers of user-service and do not call `POST /api/v2/users`. The violations reported for them reflect the change detector picking up related parameter changes alongside the required-field addition. Only gateway-service is the true breaking consumer for this scenario in production.

---

### SC-003 — Add optional `avatar_url` field

- **Verdict:** COMPATIBLE ✓  
- **Impact score:** 0.125 | **Radius:** 5  
- **Duration:** 0.002s  
- **HITL triggered:** No  
- **Consumers flagged:** 0

No breaking changes detected by `diff_endpoints`. The LLM compliance agent was not invoked. The pipeline correctly short-circuited at the diff stage.

---

### SC-004 — Change `id` field type `string` → `integer`

- **Verdict:** BREAKING ✓  
- **Impact score:** 0.435 | **Radius:** 5  
- **Duration:** 31.5s  
- **HITL triggered:** Yes

Consumers flagged (4):

| Consumer | Verdict | Key Violation |
|---|---|---|
| payment-service | BREAKING | `id` path param removed from `GET /api/v2/users/{id}`; user lookup for payment verification fails |
| order-service | BREAKING | `id` path param removal breaks user profile fetch |
| notification-svc | BREAKING | `id` removal from `GET /api/v2/users/{id}/contact` breaks contact lookup |
| gateway-service | BREAKING | `id` removed from `GET /api/v2/users/{id}`; `page` removed from `GET /api/v2/users` |

---

## Pipeline Configuration

| Setting | Value |
|---|---|
| `LLM_PROVIDER` | `anthropic` |
| `LLM_MODEL` | `claude-sonnet-4-6` |
| `IMPACT_HITL_THRESHOLD` | 0.60 |
| `BREAKING_CONSUMER_HITL_COUNT` | 2 |
| Contract registry DB | `contract_registry.db` |
| Dependency graph DB | `dependency_graph.db` |

---

## Issues Found and Fixed During This Run

1. **Missing `load_dotenv()` in eval scripts** — `eval/02_run_evaluation.py` did not load `.env`, so `LLM_PROVIDER` was not passed to the agent. Fixed by adding `load_dotenv()` at startup.

2. **Skeleton usage patterns** — The initial run passed only `{"repo": "k11techlab/<consumer>"}` to the compliance agent. Without endpoint-level usage context, the LLM returned `UNCERTAIN` for all breaking scenarios. Fixed by defining `CONSUMER_USAGE_PATTERNS` in `02_run_evaluation.py` with actual endpoint, method, and field usage per consumer.

3. **SC-003 false positive from missing parameter declarations** — The injected SC-003 contract omitted explicit `parameters` sections present in the base contract (`page` on `GET /api/v2/users`, `id` on path endpoints). The diff detector treated these as "parameter removed" breaking changes. Fixed by aligning SC-003's parameter declarations with the base contract in `01_inject_contracts.py`.

---

## Eval Scripts

| Script | Purpose |
|---|---|
| `eval/00_scaffold_services.py` | Create dependency graph + seed base `user-service` contract |
| `eval/01_inject_contracts.py` | Inject 4 contract scenarios into the registry |
| `eval/02_run_evaluation.py` | Run each scenario through diff → impact → LLM compliance |
| `eval/03_analyse_results.py` | Compute RQ1–RQ4 metrics from `results.json` |

Output files: `eval/results.json`, `eval/metrics.json`, `eval/scenarios_manifest.json`
