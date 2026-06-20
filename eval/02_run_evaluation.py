# Copyright 2026 Kavita Jadhav / K11 Software Solutions LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/02_run_evaluation.py
──────────────────────────
Replays each scenario from the manifest through the full pipeline
(without the HITL gate, which is bypassed in evaluation mode).

For each scenario, records:
  - Pipeline verdict (BREAKING / COMPATIBLE / UNCERTAIN)
  - Impact score
  - List of flagged consumers with violations
  - Execution time

Results are written to eval/results.json for analysis in 03_analyse_results.py.

Usage:
    python eval/02_run_evaluation.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from contracts.registry import ContractRegistry
from graph.dependency_graph import DependencyGraph, ConsumptionEdge, ServiceNode
from graph.graph_store import GraphStore
from analyzer.change_detector import diff_endpoints
from analyzer.impact_scorer import score_impact
from agents.contract_compliance_agent import ContractComplianceAgent

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "scenarios_manifest.json")
RESULTS_PATH  = os.path.join(os.path.dirname(__file__), "results.json")

# Base SHA to diff against (seeded by 00_scaffold_services.py)
BASE_SHA = "abc1234"

# Realistic per-consumer usage patterns derived from the dependency graph topology.
# Each entry describes which endpoints and fields a consumer actually uses so the
# LLM compliance agent can make a definitive BREAKING vs COMPATIBLE determination.
CONSUMER_USAGE_PATTERNS: dict[str, dict] = {
    "order-service": {
        "repo": "k11techlab/order-service",
        "endpoints_called": [
            {
                "endpoint": "/api/v2/users/{id}",
                "methods": ["GET"],
                "fields_used": ["id", "email", "name"],
                "criticality": "high",
            }
        ],
        "notes": "Fetches user profile to attach to orders. Read-only; never calls POST /api/v2/users.",
    },
    "payment-service": {
        "repo": "k11techlab/payment-service",
        "endpoints_called": [
            {
                "endpoint": "/api/v2/users/{id}",
                "methods": ["GET"],
                "fields_used": ["id", "email"],
                "criticality": "critical",
            }
        ],
        "notes": "Retrieves user identity for payment verification. Read-only.",
    },
    "notification-svc": {
        "repo": "k11techlab/notification-svc",
        "endpoints_called": [
            {
                "endpoint": "/api/v2/users/{id}/contact",
                "methods": ["GET"],
                "fields_used": ["email", "phone"],
                "criticality": "medium",
            }
        ],
        "notes": "Fetches contact details for outbound notifications. Does NOT call /api/v2/users/{id} directly.",
    },
    "gateway-service": {
        "repo": "k11techlab/gateway-service",
        "endpoints_called": [
            {
                "endpoint": "/api/v2/users",
                "methods": ["GET", "POST"],
                "fields_used": ["id", "email", "name"],
                "criticality": "high",
            },
            {
                "endpoint": "/api/v2/users/{id}",
                "methods": ["GET"],
                "fields_used": ["id", "email", "name"],
                "criticality": "high",
            },
        ],
        "notes": "API gateway proxies all user-service endpoints including POST /api/v2/users for registration.",
    },
    "analytics-svc": {
        "repo": "k11techlab/analytics-svc",
        "endpoints_called": [],
        "notes": "Does NOT consume user-service. Only consumes order-service (/api/v1/orders).",
    },
}


async def run_scenario(
    scenario: dict,
    registry: ContractRegistry,
    graph: DependencyGraph,
) -> dict:
    """Run one scenario through the analysis pipeline."""
    t0 = time.perf_counter()

    service = scenario["service"]
    sha     = scenario["sha"]

    # Fetch base and head contracts
    base_contract = await registry.get_contract(service, sha=BASE_SHA)
    head_contract = await registry.get_contract(service, sha=sha)

    if base_contract is None or head_contract is None:
        return {
            **scenario,
            "pipeline_verdict":  "ERROR",
            "impact_score":      0.0,
            "consumers_flagged": [],
            "duration_s":        time.perf_counter() - t0,
            "error":             "Missing contract",
        }

    # Diff
    diff = diff_endpoints(base_contract, head_contract)
    changed_endpoints = [c.endpoint for c in diff.changes]

    # Impact score
    assessment = score_impact(diff, graph, service)

    # Consumer compliance (without real LLM — use lightweight mode)
    agent  = ContractComplianceAgent()
    breaking_changes = [
        {
            "change_type": c.change_type.value,
            "endpoint":    c.endpoint,
            "description": c.description,
            "severity":    c.severity,
        }
        for c in diff.breaking
    ]

    consumers_flagged: list[dict] = []
    consumers = graph.downstream_consumers(service)
    for consumer in consumers:
        if not breaking_changes:
            result_verdict = "COMPATIBLE"
            violations = []
        else:
            usage = CONSUMER_USAGE_PATTERNS.get(
                consumer,
                {"repo": f"k11techlab/{consumer}", "endpoints_called": [], "notes": "Unknown consumer."},
            )
            result = await agent.run(
                consumer=consumer,
                contract_diff={"breaking_changes": breaking_changes},
                usage_patterns=usage,
            )
            result_verdict = result.verdict
            violations     = result.violations

        if result_verdict in ("BREAKING", "UNCERTAIN"):
            consumers_flagged.append({
                "consumer":  consumer,
                "verdict":   result_verdict,
                "violations": violations,
            })

    # Compute pipeline verdict
    pipeline_verdict = "COMPATIBLE"
    if any(c["verdict"] == "BREAKING" for c in consumers_flagged):
        pipeline_verdict = "BREAKING"
    elif any(c["verdict"] == "UNCERTAIN" for c in consumers_flagged):
        pipeline_verdict = "UNCERTAIN"

    duration = time.perf_counter() - t0
    print(
        f"  {scenario['scenario_id']}: verdict={pipeline_verdict} "
        f"score={assessment.impact_score:.2f} "
        f"radius={assessment.impact_radius} "
        f"flagged={len(consumers_flagged)} "
        f"({duration:.1f}s)"
    )

    return {
        **scenario,
        "pipeline_verdict":    pipeline_verdict,
        "impact_score":        round(assessment.impact_score, 4),
        "impact_radius":       assessment.impact_radius,
        "breaking_changes":    len(diff.breaking),
        "consumers_flagged":   consumers_flagged,
        "hitl_triggered":      assessment.hitl_required,
        "duration_s":          round(duration, 3),
    }


async def main():
    print("=== K11tech Microservice QA — Evaluation Run ===\n")

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    registry_db = os.getenv("CONTRACT_REGISTRY_DB", "contract_registry.db")
    graph_db    = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")

    results: list[dict] = []
    async with ContractRegistry(registry_db) as registry, \
               GraphStore(graph_db) as store:
        graph = await store.load_graph()
        for scenario in manifest:
            result = await run_scenario(scenario, registry, graph)
            results.append(result)

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n✅ Evaluated {len(results)} scenarios.")
    print(f"   Results: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
