# Evaluation Plan — K11tech Microservice QA System

**Version:** 1.0  
**Date:** 2026-06-08  
**Target venues:** ICSME, SANER, MSR, FSE Industry Track, or ArXiv preprint

---

## 1. Research Questions

| ID | Question |
|----|----------|
| RQ1 | How accurately does the pipeline detect breaking API contract changes? (precision, recall, F1) |
| RQ2 | Does LLM-based compliance checking reduce false negatives compared to diff-only analysis? |
| RQ3 | How does the dependency graph traversal affect impact radius accuracy? |
| RQ4 | At what rate does the HITL gate correctly escalate high-risk changes? |
| RQ5 | What is the latency and cost profile per pipeline run? |

---

## 2. Test Scope

### Phase A — Controlled scenarios on K11-Software-Solutions repos (feasible now)

Uses the 4 existing repos: `k11-user-service`, `k11-order-service`, `k11-payment-service`, `k11-notification-svc`.

All changes are authored by the research team. Framing: **proof-of-concept / controlled evaluation**.

### Phase B — External open-source repos (required for full paper)

Uses third-party microservice projects with real historical PRs not authored by the research team. Required for **generalization claims**.

Recommended sources:
- [GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo) (Online Boutique — 10 services, gRPC + REST)
- [microservices-demo/microservices-demo](https://github.com/microservices-demo/microservices-demo) (Sock Shop)
- Any open-source platform with OpenAPI specs and inter-service dependencies (e.g., Backstage plugins, Temporal samples)

---

## 3. Phase A — Scenario Matrix (K11 repos)

Target: **15 PRs** across 5 breaking-change categories + 4 non-breaking + 2 edge cases.

### 3.1 Breaking Change Scenarios (ground truth = BREAKING)

| PR# | Repo | Change | Breaking Type | Expected consumers flagged |
|-----|------|--------|---------------|---------------------------|
| A-01 | k11-user-service | Remove `GET /api/v2/users/{id}` *(done — PR#1)* | Endpoint removal | order, payment, notification |
| A-02 | k11-user-service | Add required field `phone` to `POST /api/v2/users` | Required field added | gateway (if added), order |
| A-03 | k11-user-service | Rename field `id` → `user_id` in response schema | Field renamed | order, payment, notification |
| A-04 | k11-user-service | Change `GET /api/v2/users/{id}` response `200` → `202` | Response code changed | order, payment |
| A-05 | k11-user-service | Remove `email` from `GET /api/v2/users/{id}` response | Required field removed | payment (uses email) |
| A-06 | k11-order-service | Remove `GET /api/v1/orders/{id}` | Endpoint removal | payment |
| A-07 | k11-order-service | Add required `currency` param to `POST /api/v1/orders` | Required param added | payment |

### 3.2 Non-Breaking Change Scenarios (ground truth = COMPATIBLE)

| PR# | Repo | Change | Non-Breaking Type |
|-----|------|--------|-------------------|
| A-08 | k11-user-service | Add optional `avatar_url` field to response | Additive field |
| A-09 | k11-user-service | Add new endpoint `GET /api/v2/users/{id}/preferences` | New endpoint |
| A-10 | k11-order-service | Add optional `notes` field to order response | Additive field |
| A-11 | k11-payment-service | Add new `GET /api/v1/payments/{id}/receipt` endpoint | New endpoint |

### 3.3 Edge Cases

| PR# | Repo | Change | Purpose |
|-----|------|--------|---------|
| A-12 | k11-user-service | Bump version only, no spec change | Tests version-bump-without-change path |
| A-13 | k11-notification-svc | Change `.proto` stub (add gRPC path) | Tests gRPC contract extraction |
| A-14 | k11-user-service | Multi-change PR: remove one endpoint + add two new ones | Tests mixed breaking/non-breaking diff |
| A-15 | k11-order-service | Change that triggers HITL gate (impact score > 0.60) | Validates HITL escalation path |

### 3.4 Topology Extensions (before running A-06, A-07)

Before running order-service scenarios, extend the dependency graph:

```
k11-notification-svc → k11-order-service  /api/v1/orders/{id}  GET  medium
```

This adds a transitive consumer path: `notification-svc → order-service → user-service`, enabling RQ3 (transitive impact radius) testing.

---

## 4. Baseline Comparison (RQ2)

Run each Phase A scenario through two pipelines and compare:

| Pipeline | Description |
|----------|-------------|
| **B1 — Diff only** | `oasdiff` CLI on the two `openapi.yaml` files. Reports breaking changes but has no concept of consumers or usage patterns. Verdict = BREAKING if any breaking diff, else COMPATIBLE. |
| **B2 — Graph + Diff (no LLM)** | Pipeline with `ContractComplianceAgent` disabled. Consumer verdict = BREAKING if the consumer has a registered edge to the changed endpoint, else COMPATIBLE. No LLM call. |
| **B3 — Full pipeline (LLM)** | This system. |

Metrics per pipeline:

- Precision, Recall, F1 against ground truth labels
- False positive rate on non-breaking scenarios
- UNCERTAIN rate (B3 only — how often LLM cannot decide)

**Hypothesis:** B3 reduces UNCERTAIN rate vs B2 and reduces false positives vs B1.

---

## 5. Ablation Studies (RQ2, RQ3)

| Ablation | What's removed | Measured effect |
|----------|----------------|-----------------|
| **Abl-1** | LLM compliance check → replaced with edge-existence check | UNCERTAIN rate, precision change |
| **Abl-2** | Dependency graph → all registered services treated as consumers | Impact radius accuracy, false positive rate |
| **Abl-3** | Usage patterns (endpoints_called) → pass only `{"repo": ...}` | UNCERTAIN rate on real edge scenarios |
| **Abl-4** | `edge_criticality` weighting → all edges treated as `medium` | Impact score distribution |

---

## 6. HITL Gate Evaluation (RQ4)

Test the HITL trigger across three threshold settings:

| Config | `IMPACT_HITL_THRESHOLD` | `BREAKING_CONSUMER_HITL_COUNT` |
|--------|------------------------|-------------------------------|
| Strict | 0.35 | 1 |
| Default | 0.60 | 2 |
| Lenient | 0.80 | 3 |

For each threshold, record:
- Number of HITL triggers across all 15 Phase A scenarios
- True escalations (breaking scenarios that triggered HITL)
- False escalations (non-breaking scenarios that triggered HITL)
- Missed escalations (breaking scenarios that did not trigger HITL)

---

## 7. Latency and Cost Profile (RQ5)

Collect per run:

| Metric | How |
|--------|-----|
| Total wall time (s) | `completed_at - started_at` in pipeline state |
| Phase 1 time | Log timestamps in `ContractExtractorAgent` |
| Phase 3 time | Log timestamps across parallel consumer nodes |
| LLM input tokens | `resp.usage.input_tokens` per compliance call |
| LLM output tokens | `resp.usage.output_tokens` per compliance call |
| Estimated cost | Tokens × model pricing (`claude-sonnet-4-6`: $3/$15 per 1M in/out) |

Report as mean ± std across all runs, broken down by impact radius (1, 2, 3 consumers).

---

## 8. Phase B — External Repo Evaluation (Full Paper)

### 8.1 Data collection

1. Fork or clone a multi-service repo with committed OpenAPI specs (e.g., Online Boutique)
2. Mine `git log` for PRs that changed any `openapi.yaml` / `*.proto` file
3. For each such commit, manually label ground truth: BREAKING / COMPATIBLE (by reading the diff)
4. Target: **50 labeled change events** minimum

### 8.2 Registration

Run `scripts/register_real_services.py` adapted for the external topology, populating the graph and baseline contracts.

### 8.3 Replay

For each labeled change event, construct a synthetic webhook payload with the correct `head_sha` and `base_sha` and POST to the pipeline. Compare the pipeline verdict against the ground truth label.

### 8.4 Inter-rater reliability

Have two independent annotators label a random 20% of the change events. Compute Cohen's κ. Only proceed if κ > 0.70.

---

## 9. Metrics Summary

| Metric | Formula | Target (Phase A) |
|--------|---------|-----------------|
| Precision | TP / (TP + FP) | ≥ 0.90 |
| Recall | TP / (TP + FN) | ≥ 0.90 |
| F1 | 2 × P × R / (P + R) | ≥ 0.90 |
| UNCERTAIN rate | UNCERTAIN / total | ≤ 0.10 |
| HITL precision | Correct escalations / total escalations | ≥ 0.90 |
| Mean run time | Wall time per run | ≤ 30s |
| Mean cost per run | USD at list pricing | ≤ $0.10 |

---

## 10. Execution Order

```
Step 1 — Extend topology (add notification → order edge)
Step 2 — Run Phase A scenarios A-01 through A-15 (open PRs, trigger webhooks, log results)
Step 3 — Run Baseline B1 (oasdiff) on same PRs
Step 4 — Run Baseline B2 (graph + diff, no LLM) on same PRs
Step 5 — Run Ablation studies Abl-1 through Abl-4
Step 6 — Run HITL threshold sweep (3 configs × 15 scenarios)
Step 7 — Collect latency/cost data
Step 8 — Compute all RQ1–RQ5 metrics
Step 9 — (Full paper only) Phase B external repo evaluation
Step 10 — Write up
```

---

## 11. Tooling Needed

| Tool | Purpose | Status |
|------|---------|--------|
| `oasdiff` CLI | Baseline B1 diff-only comparison | Install: `go install github.com/tufin/oasdiff@latest` |
| `eval/02_run_evaluation.py` | Phase A harness (already exists) | Ready |
| `eval/03_analyse_results.py` | RQ1–RQ4 metrics (already exists) | Extend for cost/latency |
| Webhook simulator script | POST signed webhooks for each PR | Write: `scripts/simulate_webhook.py` |
| Token usage logger | Patch `ContractComplianceAgent` to log token counts | Small addition to `agents/contract_compliance_agent.py` |

---

## 12. Limitations to Acknowledge

- **Phase A construct validity:** all service topologies and PRs were designed by the authors — results may not generalise to arbitrary architectures
- **Single LLM:** only `claude-sonnet-4-6` tested; results may differ with other models
- **OpenAPI only (Phase A):** gRPC and GraphQL paths not fully evaluated
- **No production traffic data:** consumer usage patterns come from registered graph edges, not real API call logs — a production system might infer patterns from observability data
- **Simulated webhooks:** Phase A uses manually crafted webhook payloads, not live GitHub webhook delivery

---

## 13. Paper Framing Options

| Framing | Scope | Suitable for |
|---------|-------|--------------|
| **Proof-of-concept tool paper** | Phase A only (15 scenarios, 4 repos) | MSR tool track, ICSME ERA, workshop paper |
| **Empirical evaluation** | Phase A + Phase B (50+ external PRs) | ICSME main, SANER, FSE industry track |
| **Full research paper** | Phase A + Phase B + user study | ICSE, FSE, IEEE TSE |
