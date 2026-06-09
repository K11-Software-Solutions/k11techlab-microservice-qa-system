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
scripts/register_real_services.py
───────────────────────────────────
One-time setup: register your real GitHub microservices in the contract
registry and dependency graph.

Edit the SERVICES, EDGES, and CONTRACT_PATHS sections below, then run:

    python scripts/register_real_services.py

After this, start the webhook server:

    uvicorn api.webhook:app --reload --port 9001

Then expose it publicly (for GitHub to reach it):

    ngrok http 9001
    # Copy the https URL → use as webhook URL in GitHub

Register the webhook in each GitHub repo:
    Settings → Webhooks → Add webhook
    Payload URL: https://<ngrok-id>.ngrok.io/webhook/github
    Content type: application/json
    Secret: your GITHUB_WEBHOOK_SECRET from .env
    Events: Pull requests
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from contracts.registry import ContractRegistry
from graph.dependency_graph import ConsumptionEdge, ServiceNode
from graph.graph_store import GraphStore
from analyzer.contract_extractor import extract_contract

# ─────────────────────────────────────────────────────────────────────────────
# EDIT THIS SECTION WITH YOUR REAL SERVICES
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_ORG = os.getenv("GITHUB_ORG", "K11-Software-Solutions")

SERVICES = [
    ServiceNode(name="k11-user-service",     repo="K11-Software-Solutions/k11-user-service",
                team="platform",  slack_channel="#team-platform"),
    ServiceNode(name="k11-order-service",    repo="K11-Software-Solutions/k11-order-service",
                team="commerce",  slack_channel="#team-commerce"),
    ServiceNode(name="k11-payment-service",  repo="K11-Software-Solutions/k11-payment-service",
                team="payments",  slack_channel="#team-payments"),
    ServiceNode(name="k11-notification-svc", repo="K11-Software-Solutions/k11-notification-svc",
                team="comms",     slack_channel="#team-comms"),
]

EDGES = [
    # order-service calls user-service to validate user on order creation
    ConsumptionEdge("k11-order-service",    "k11-user-service",    "/api/v2/users/{id}",         ["GET"],  "high"),
    # payment-service calls user-service for identity verification
    ConsumptionEdge("k11-payment-service",  "k11-user-service",    "/api/v2/users/{id}",         ["GET"],  "critical"),
    # payment-service calls order-service to validate the order
    ConsumptionEdge("k11-payment-service",  "k11-order-service",   "/api/v1/orders/{id}",        ["GET"],  "critical"),
    # notification-svc calls user-service for contact info
    ConsumptionEdge("k11-notification-svc", "k11-user-service",    "/api/v2/users/{id}/contact", ["GET"],  "medium"),
]

CONTRACT_PATHS = {
    "k11-user-service":     "openapi.yaml",
    "k11-order-service":    "openapi.yaml",
    "k11-payment-service":  "openapi.yaml",
    "k11-notification-svc": "openapi.yaml",
}

# ─────────────────────────────────────────────────────────────────────────────


async def register():
    print("=== K11tech Microservice QA — Real Service Registration ===\n")

    graph_db    = os.getenv("GRAPH_STORE_DB",      "dependency_graph.db")
    registry_db = os.getenv("CONTRACT_REGISTRY_DB", "contract_registry.db")

    # ── 1. Register services and edges in the dependency graph ────────────
    print(f"Building dependency graph ({graph_db})...")
    async with GraphStore(graph_db) as store:
        for node in SERVICES:
            await store.add_service(node)
            print(f"  + service: {node.name}  ({node.repo})")
        for edge in EDGES:
            await store.record_consumption(edge)
            print(f"  + edge:    {edge.consumer} → {edge.provider}  {edge.endpoint_pattern}")

    # ── 2. Register services and fetch baseline contracts from GitHub ─────
    print(f"\nSeeding contract registry ({registry_db})...")
    async with ContractRegistry(registry_db) as registry:
        for node in SERVICES:
            await registry.register_service(node.name, node.repo)
            print(f"  + registered: {node.name}")

        for node in SERVICES:
            spec_path = CONTRACT_PATHS.get(node.name)
            if not spec_path:
                print(f"  ! no CONTRACT_PATHS entry for {node.name} — skipping baseline")
                continue

            raw = await _fetch_from_github(node.repo, spec_path)
            if not raw:
                print(f"  ! could not fetch {spec_path} from {node.repo} — check GITHUB_TOKEN and path")
                continue

            contract = extract_contract(spec_path, raw, node.name, node.repo, "HEAD")
            if contract:
                await registry.store_contract(contract)
                print(f"  + baseline contract: {node.name}  v{contract.version}  ({len(contract.endpoints)} endpoints)")
            else:
                print(f"  ! failed to parse contract for {node.name}")

    print("\n✅ Registration complete.")
    print(f"   Services: {len(SERVICES)}")
    print(f"   Edges:    {len(EDGES)}")
    print("\nNext steps:")
    print("  1. uvicorn api.webhook:app --reload --port 9001")
    print("  2. ngrok http 9001")
    print("  3. Add webhook in GitHub repo Settings → Webhooks")


async def _fetch_from_github(repo: str, path: str, ref: str = "HEAD") -> str:
    import base64
    import httpx

    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("  ! GITHUB_TOKEN not set — cannot fetch from GitHub")
        return ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if data.get("encoding") == "base64":
                return base64.b64decode(data["content"]).decode("utf-8")
            return data.get("content", "")
    except Exception as exc:
        print(f"  ! GitHub fetch error ({repo}/{path}): {exc}")
        return ""


if __name__ == "__main__":
    asyncio.run(register())
