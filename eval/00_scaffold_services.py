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
eval/00_scaffold_services.py
──────────────────────────────
Creates a realistic test microservice topology for evaluation:

  user-service       (provides /api/v2/users/*)
  order-service      (consumes user-service, provides /api/v1/orders/*)
  notification-svc   (consumes user-service)
  payment-service    (consumes user-service + order-service)
  analytics-service  (consumes order-service)
  gateway-service    (consumes all services)

Registers all services in the Contract Registry and Dependency Graph MCPs.
Run once before evaluation experiments.

Usage:
    python eval/00_scaffold_services.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts.models import ContractFormat, Endpoint, ServiceContract
from contracts.registry import ContractRegistry
from graph.dependency_graph import ConsumptionEdge, ServiceNode
from graph.graph_store import GraphStore
from datetime import datetime, timezone

# ── Service topology ──────────────────────────────────────────────────────────

SERVICES = [
    ServiceNode(name="user-service",     repo="k11techlab/user-service",
                team="platform",          slack_channel="#team-platform"),
    ServiceNode(name="order-service",    repo="k11techlab/order-service",
                team="commerce",          slack_channel="#team-commerce"),
    ServiceNode(name="notification-svc", repo="k11techlab/notification-svc",
                team="comms",             slack_channel="#team-comms"),
    ServiceNode(name="payment-service",  repo="k11techlab/payment-service",
                team="payments",          slack_channel="#team-payments"),
    ServiceNode(name="analytics-svc",    repo="k11techlab/analytics-svc",
                team="data",              slack_channel="#team-data"),
    ServiceNode(name="gateway-service",  repo="k11techlab/gateway-service",
                team="platform",          slack_channel="#team-platform"),
]

EDGES = [
    ConsumptionEdge("order-service",    "user-service",  "/api/v2/users/{id}",
                    ["GET"],            "high"),
    ConsumptionEdge("notification-svc", "user-service",  "/api/v2/users/{id}/contact",
                    ["GET"],            "medium"),
    ConsumptionEdge("payment-service",  "user-service",  "/api/v2/users/{id}",
                    ["GET"],            "critical"),
    ConsumptionEdge("payment-service",  "order-service", "/api/v1/orders/{id}",
                    ["GET", "PATCH"],   "critical"),
    ConsumptionEdge("analytics-svc",    "order-service", "/api/v1/orders",
                    ["GET"],            "low"),
    ConsumptionEdge("gateway-service",  "user-service",  "/api/v2/users",
                    ["GET", "POST"],    "high"),
    ConsumptionEdge("gateway-service",  "order-service", "/api/v1/orders",
                    ["GET", "POST"],    "high"),
    ConsumptionEdge("gateway-service",  "payment-service", "/api/v1/payments",
                    ["POST"],           "critical"),
]

# ── Initial contracts ─────────────────────────────────────────────────────────

USER_SERVICE_V1_OPENAPI = {
    "openapi": "3.0.0",
    "info":    {"title": "User Service", "version": "2.1.0"},
    "paths":   {
        "/api/v2/users": {
            "get": {
                "summary": "List users",
                "parameters": [{"name": "page", "in": "query", "required": False,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "summary": "Create user",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["email", "name"],
                        "properties": {
                            "email": {"type": "string"},
                            "name":  {"type": "string"},
                        },
                    }}},
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/api/v2/users/{id}": {
            "get": {
                "summary": "Get user by ID",
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}, "404": {"description": "Not Found"}},
            },
        },
        "/api/v2/users/{id}/contact": {
            "get": {
                "summary": "Get user contact info",
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}},
            },
        },
    },
}


def _make_contract(service_name: str, repo: str, raw: dict, sha: str) -> ServiceContract:
    from analyzer.contract_extractor import extract_openapi_contract
    import yaml
    return extract_openapi_contract(
        yaml.dump(raw), service_name, repo, sha
    )


async def scaffold():
    print("=== K11tech Microservice QA — Service Scaffolding ===\n")

    # ── Register in Dependency Graph ──────────────────────────────────────
    graph_db = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
    print(f"Building dependency graph ({graph_db})...")
    async with GraphStore(graph_db) as store:
        for node in SERVICES:
            await store.add_service(node)
            print(f"  ✓ Added service: {node.name}")
        for edge in EDGES:
            await store.record_consumption(edge)
            print(f"  ✓ Recorded: {edge.consumer} → {edge.provider} ({edge.endpoint_pattern})")

    # ── Store initial contracts in Contract Registry ───────────────────────
    registry_db = os.getenv("CONTRACT_REGISTRY_DB", "contract_registry.db")
    print(f"\nSeeding contract registry ({registry_db})...")
    async with ContractRegistry(registry_db) as registry:
        for node in SERVICES:
            await registry.register_service(node.name, node.repo)
            print(f"  ✓ Registered: {node.name}")

        # Store user-service initial contract
        contract = _make_contract(
            "user-service", "k11techlab/user-service",
            USER_SERVICE_V1_OPENAPI, "abc1234"
        )
        await registry.store_contract(contract)
        print(f"  ✓ Stored contract: user-service v2.1.0 ({len(contract.endpoints)} endpoints)")

    print("\n✅ Scaffolding complete.")
    print(f"   Services: {len(SERVICES)}")
    print(f"   Edges:    {len(EDGES)}")


if __name__ == "__main__":
    asyncio.run(scaffold())
