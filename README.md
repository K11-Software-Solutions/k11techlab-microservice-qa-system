# K11tech Microservice QA System

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![Paper 1 DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20543872.svg)](https://doi.org/10.5281/zenodo.20543872)
[![Paper 2 DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20613081.svg)](https://doi.org/10.5281/zenodo.20613081)

**System-Level Impact Analysis for Microservice CI/CD via Cross-Repository Dependency Graphs**

This repository implements Paper 3 in the K11tech Agentic AI QA research series — extending
single-repository quality assurance to distributed microservice systems by capturing inter-service
API contracts and performing downstream impact analysis from a single pull request trigger.

---

## Research Series

| Paper | Title | Status |
|-------|-------|--------|
| Paper 1 | [Autonomous CI/CD QA Using LangGraph Multi-Agent Orchestration](https://doi.org/10.5281/zenodo.20543872) | Published ✅ |
| Paper 2 | [Beyond Static Gates: Closing the Detect–Fix–Learn Loop](https://doi.org/10.5281/zenodo.20613081) | Published ✅ |
| Paper 3 | System-Level Impact Analysis for Microservice CI/CD via Cross-Repository Dependency Graphs | This repo — Evaluation complete |

---

## The Problem

Single-repository quality gates are blind to distributed system failures. A pull request that
changes a REST endpoint signature passes all local tests — but silently breaks three downstream
consumers in other repositories. These failures are invisible until deployment.

```
Service A (PR merged) ──changes──▶ /api/v2/users/{id}
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
              Service B             Service C             Service D
           (consumer — breaks)   (consumer — breaks)   (mock — OK)
```

**No existing CI/CD tool detects this at PR time.**

---

## Architecture

```
Pull Request Event (any service repo)
          │
          ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 1 — Contract Extraction                          │
│  parse_openapi · extract_changed_endpoints · diff       │
│  Fast-path: no consumers → COMPATIBLE in ~3s            │
└──────────────────────────┬──────────────────────────────┘
                           │
          ┌────────────────▼────────────────┐
          │  Contract Registry (SQLite MCP) │
          │  store / retrieve / version     │
          └────────────────┬────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│  PHASE 2 — Dependency Graph Traversal                   │
│  NetworkX directed graph · find_downstream_consumers    │
│  impact_radius scoring · edge criticality               │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│  PHASE 3 — Parallel Consumer Validation                 │
│  ContractComplianceAgent per consumer (LangGraph Send)  │
│  LLM verdict: BREAKING / COMPATIBLE / UNCERTAIN         │
└──────────────────────────┬──────────────────────────────┘
                           │
          ┌────────────────▼────────────────┐
          │  HITL Gate (cross-repo)         │
          │  interrupt if breaking_count≥2  │
          └────────────────┬────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│  PHASE 4 — Impact Report                                │
│  aggregate · rank_severity · notify affected owners     │
└─────────────────────────────────────────────────────────┘
```

Three pipeline tiers with different cost/accuracy trade-offs:

| Tier | Pipeline | Description | Latency |
|------|----------|-------------|---------|
| B1 | Diff only | Structural OpenAPI diff — no LLM | <1s |
| B2 | Graph + diff | B1 + dependency graph traversal | ~2s |
| B3 | Full | B1 + B2 + LLM consumer validation | 8–26s |

---

## Key Innovations

**1. Cross-Repository Contract Registry**
A persistent MCP server (SQLite-backed) storing versioned API contracts for every registered
service. Contracts are extracted automatically from each PR via GitHub webhook.

**2. Dependency Graph Engine**
A directed graph (NetworkX) where edges represent service-to-service API consumption with
per-edge metadata: endpoint pattern, HTTP methods, criticality, team ownership.
Supports impact radius scoring and direct/transitive consumer discovery.

**3. Parallel Consumer Validation**
For each downstream consumer identified in Phase 2, a `ContractComplianceAgent` validates
the proposed contract change against the consumer's current usage patterns — in parallel via
the LangGraph Send API. Edge methods (`GET`, `POST`, etc.) are propagated through the full
consumer context so the LLM sees accurate HTTP method information.

**4. Cross-Repository HITL Gate**
Extends the single-repo HITL gate from Paper 1 to consider impact radius: a PR breaking
2+ downstream consumers triggers human escalation before merge, regardless of local risk score.

---

## Evaluation Results

Full reports: [`docs/eval5.md`](docs/eval5.md) (Phase A) · [`docs/PhaseB_Final_Eval.md`](docs/PhaseB_Final_Eval.md) (Phase B final)

### Phase A — Controlled Evaluation (2026-06-08)

**Setup:** 15 scenarios (9 BREAKING, 6 COMPATIBLE) across 4 real GitHub repos
(k11-user-service, k11-order-service, k11-payment-service, k11-notification-svc).
HMAC-signed webhook → LangGraph pipeline → polled verdict.

| Pipeline | Precision | Recall | F1 | Accuracy | Mean Latency |
|----------|-----------|--------|-----|----------|-------------|
| B3 Full (LLM) | **100%** | **100%** | **100%** | 93.3% | 12.9s |
| B2 Graph+diff | **100%** | **100%** | **100%** | **100%** | ~2s |
| B1 Diff only | 90% | **100%** | 94.7% | 93.3% | <1s |

- HITL: 6 triggered (6 true, 0 false)
- Only ambiguous case: A-02 (UNCERTAIN) — POST consumer edges not yet registered

### Phase B — External Repos + Multi-Model Ablation (2026-06-08)

**B1 on 59 real-world OSS pull requests** (ory/hydra, ory/kratos, grafana/grafana, portainer/portainer):

| Pipeline | Precision | Recall | F1 | Accuracy | Dataset |
|----------|-----------|--------|-----|----------|---------|
| B1 — Diff only | **100%** | **100%** | **100%** | **100%** | 6 BREAKING, 53 COMPATIBLE |

Zero false positives across 4 domains (auth, observability, devops, demo).

**Multi-model ablation** (15 Phase A scenarios, Claude models only):

| Model | Precision | Recall | F1 | Accuracy | HITL | Mean Latency |
|-------|-----------|--------|-----|----------|------|-------------|
| **claude-haiku-4-5** ✅ | **100%** | **100%** | **100%** | **100%** | 8T/0F | **8.9s** |
| claude-sonnet-4-6 *(baseline)* | 100% | 100% | 100% | 93.3% | 6T/0F | 12.9s |
| claude-opus-4-8 | 100% | 100% | 100% | 86.7% | 6T/0F | 10.9s |
| sonnet-4-6 + complete graph | 90% | 100% | 94.7% | 93.3% | 8T/1F | 12.8s |

**Recommendation:** Deploy with **Claude Haiku 4.5** — highest accuracy, 31% faster, lowest cost.

### Research Questions — Answered

| RQ | Question | Answer |
|----|----------|--------|
| RQ1 | How accurately does the system detect breaking contract changes? | B3: P=100%, R=100%, F1=100% (Phase A); B1: P=100%, R=100%, F1=100% (Phase B external) |
| RQ2 | What is the false negative rate? | 0% — zero breaking changes reached a false COMPATIBLE verdict across all models |
| RQ3 | Does impact radius scoring correlate with severity? | HITL triggered correctly on all multi-consumer breaks; 0 false escalations in 4 ablation runs |
| RQ4 | Does the cross-repo HITL gate reduce failures without over-triggering? | 6–8 correct triggers, 0 false triggers across all models |

---

## Project Structure

```
k11techlab-microservice-qa-system/
│
├── analyzer/
│   ├── contract_extractor.py      # OpenAPI parser with content-sniff fallback
│   ├── change_detector.py         # Structural diff + response schema diffing
│   └── impact_scorer.py           # Impact radius scoring from graph
│
├── contracts/
│   ├── models.py                  # ServiceContract, ContractVersion, Endpoint
│   ├── registry.py                # Contract storage and retrieval (SQLite)
│   └── diff.py                    # Breaking change classification
│
├── graph/
│   ├── dependency_graph.py        # NetworkX directed graph + traversal
│   ├── consumer_finder.py         # ConsumerContext + edge_methods propagation
│   └── graph_store.py             # Persistent graph storage (SQLite)
│
├── agents/
│   ├── base.py
│   ├── contract_compliance_agent.py  # LLM verdict (Haiku/Sonnet/Opus-aware)
│   ├── breaking_change_agent.py
│   ├── contract_extractor_agent.py
│   ├── dependency_graph_agent.py
│   └── impact_report_agent.py
│
├── pipeline/
│   ├── state.py                   # MicroservicePipelineState TypedDict
│   ├── phase1.py                  # Contract extraction subgraph
│   ├── phase2.py                  # Dependency graph traversal
│   ├── phase3.py                  # Parallel consumer validation (LangGraph Send)
│   ├── phase4.py                  # Impact reporting
│   ├── hitl.py                    # Cross-repo HITL gate
│   └── orchestrator.py            # Main LangGraph StateGraph
│
├── mcps/
│   ├── contract_registry_mcp/     # MCP server: versioned contract storage
│   └── graph_store_mcp/           # MCP server: dependency graph persistence
│
├── api/
│   └── webhook.py                 # FastAPI: POST /webhook/github, GET /runs/{run_id}
│
├── scripts/
│   ├── run_phase_a_eval.py        # Phase A: 15-scenario controlled eval
│   ├── run_phase_b_eval.py        # Phase B: external repo full-pipeline eval
│   ├── find_external_prs.py       # OSS repo discovery + B1 auto-classification
│   ├── run_ablation.py            # Multi-model ablation (--model, --compare)
│   └── register_real_services.py  # Register k11techlab services + consumer edges
│
├── eval/
│   ├── phase_b_candidates.json    # 89 OSS candidates (59 labelled)
│   ├── results_phase_a.json       # Phase A Sonnet 4.6 baseline results
│   ├── results_phase_b.json       # Phase B full-pipeline results
│   ├── results_ablation_claude-haiku-4-5.json
│   ├── results_ablation_claude-opus-4-8.json
│   └── results_ablation_sonnet-complete-graph.json
│
├── docs/
│   ├── PhaseB_Final_Eval.md       # Final Phase B evaluation report
│   ├── eval5.md                   # Phase A canonical eval report
│   ├── eval_phase_b.md            # Phase B working notes
│   ├── eval1.md – eval4.md        # Iterative eval history (bug discovery)
│   ├── architecture.md
│   ├── contract-formats.md
│   └── graph-model.md
│
├── tests/
│   ├── unit/
│   │   ├── test_contract_models.py
│   │   └── test_dependency_graph.py
│   └── integration/
│       └── test_pipeline.py
│
├── requirements.txt
├── docker-compose.yml
└── .env.example
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/K11-Software-Solutions/k11techlab-microservice-qa-system
cd k11techlab-microservice-qa-system

# 2. Install
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Required: ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET
# Optional: LLM_MODEL (default: claude-haiku-4-5), LLM_PROVIDER (default: anthropic)

# 4. Start the webhook server
uvicorn api.webhook:app --port 9001

# 5. Register your services and consumer edges
python scripts/register_real_services.py

# 6. Send a test webhook
python -c "
import hmac, hashlib, json, requests
payload = json.dumps({'action':'opened','pull_request':{'number':1,'head':{'sha':'abc123'},'base':{'sha':'def456'}},'repository':{'full_name':'your-org/your-service'}}).encode()
sig = 'sha256=' + hmac.new(b'your-secret', payload, hashlib.sha256).hexdigest()
r = requests.post('http://localhost:9001/webhook/github', data=payload, headers={'X-Hub-Signature-256': sig, 'Content-Type': 'application/json'})
print(r.json())
"
```

### Running Evaluations

```powershell
# Phase A — 15 controlled scenarios (requires GITHUB_TOKEN)
$env:PYTHONIOENCODING="utf-8"
python scripts/run_phase_a_eval.py

# Phase B — external OSS repo discovery
python scripts/find_external_prs.py --discover     # fetch candidates
python scripts/find_external_prs.py --summary      # print breakdown

# Phase B — full pipeline eval against labelled candidates
python scripts/run_phase_b_eval.py                 # full (server must be running)
python scripts/run_phase_b_eval.py --dry-run       # B1 only, no server needed

# Multi-model ablation
python scripts/run_ablation.py --model claude-haiku-4-5 --provider anthropic
python scripts/run_ablation.py --model claude-opus-4-8  --provider anthropic
python scripts/run_ablation.py --compare                # print side-by-side table
```

---

## Service Topology (Phase A Eval)

The evaluation uses four real GitHub repos under [K11-Software-Solutions](https://github.com/K11-Software-Solutions):

```
k11-order-service       → k11-user-service    /api/v2/users/{id}         GET  high
k11-payment-service     → k11-user-service    /api/v2/users/{id}         GET  critical
k11-payment-service     → k11-order-service   /api/v1/orders/{id}        GET  critical
k11-notification-svc    → k11-user-service    /api/v2/users/{id}/contact GET  medium
k11-notification-svc    → k11-order-service   /api/v1/orders/{id}        GET  medium
```

---

## Known Limitations

**Phase 2 method filtering (open):** Consumer matching checks endpoint path pattern overlap
but not HTTP method. Registering POST consumer edges for a path that also has GET endpoints
can introduce false positives for GET-only compatible changes (demonstrated in the
`sonnet+complete-graph` ablation run). Fix: intersect `changed_op_methods` with `edge.methods`
in `graph/consumer_finder.py::find_affected_consumers`.

**External repo B3 recall:** B3 returns COMPATIBLE for external repos with no registered
consumers (correct by design — impact radius = 0). Use B1 output for external repos;
B3 adds value only when consumer edges are registered.

**go-gitea exclusion:** 30 go-gitea PRs are unclassifiable because their OpenAPI spec is
a Go text template, not a parseable document.

---

## Relation to Paper 1 System

This system runs **alongside** the Paper 1 single-repo QA pipeline, not in place of it.
A PR triggers both pipelines in parallel:

- Paper 1: single-repo quality gate (14 agents, local tests, risk scoring)
- Paper 3: cross-repo impact analysis (contract diff, downstream consumers, HITL)

Both verdicts are combined before the final merge decision.

---

## License

Apache License 2.0. See [LICENSE](LICENSE).

---

## Citation

If you use this work, please cite Paper 1 (the foundational system):

```bibtex
@misc{jadhav2026autonomous,
  title  = {Autonomous CI/CD Quality Assurance Using LangGraph Multi-Agent Orchestration
             and Risk-Proportionate Human-in-the-Loop Control},
  author = {Kavita Jadhav},
  year   = {2026},
  doi    = {10.5281/zenodo.20543872},
  url    = {https://doi.org/10.5281/zenodo.20543872}
}

@misc{jadhav2026beyondstatic,
  title  = {Beyond Static Gates: Closing the Detect–Fix–Learn Loop},
  author = {Kavita Jadhav},
  year   = {2026},
  doi    = {10.5281/zenodo.20613081},
  url    = {https://doi.org/10.5281/zenodo.20613081}
}
```

*Paper 3 citation will be added upon publication.*

---

*K11tech Microservice QA System · k11softwaresolutions@gmail.com · kavita.jadhav@k11softwaresolutions.com*
