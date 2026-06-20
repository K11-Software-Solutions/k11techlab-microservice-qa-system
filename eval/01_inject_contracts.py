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
eval/01_inject_contracts.py
─────────────────────────────
Injects a set of synthetic contract changes into the registry to simulate
real-world PR scenarios for evaluation.

Injects both:
  - Breaking changes (endpoint removal, field type changes, required fields added)
  - Non-breaking changes (new optional fields, new endpoints)

Each scenario is tagged with ground-truth labels for RQ1/RQ2 evaluation.

Usage:
    python eval/01_inject_contracts.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts.registry import ContractRegistry
from analyzer.contract_extractor import extract_openapi_contract
import yaml

SCENARIOS = [
    # (scenario_id, service, sha, description, openapi_dict, ground_truth_breaking)
    (
        "SC-001",
        "user-service",
        "breaking_001",
        "Remove /api/v2/users/{id} — breaks order-service, payment-service, gateway",
        {
            "openapi": "3.0.0",
            "info": {"title": "User Service", "version": "2.2.0"},
            "paths": {
                "/api/v2/users": {
                    "get": {
                        "summary": "List users",
                        "responses": {"200": {"description": "OK"}},
                    },
                },
                # /api/v2/users/{id} REMOVED
                "/api/v2/users/{id}/contact": {
                    "get": {"summary": "Get user contact", "responses": {"200": {"description": "OK"}}},
                },
            },
        },
        True,
    ),
    (
        "SC-002",
        "user-service",
        "breaking_002",
        "Add required field 'phone' to POST /api/v2/users — breaks consumers that create users",
        {
            "openapi": "3.0.0",
            "info": {"title": "User Service", "version": "2.2.1"},
            "paths": {
                "/api/v2/users": {
                    "post": {
                        "summary": "Create user",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {
                                "type": "object",
                                "required": ["email", "name", "phone"],  # phone added as required
                                "properties": {
                                    "email": {"type": "string"},
                                    "name":  {"type": "string"},
                                    "phone": {"type": "string"},
                                },
                            }}},
                        },
                        "responses": {"201": {"description": "Created"}},
                    },
                },
                "/api/v2/users/{id}": {
                    "get": {"summary": "Get user", "responses": {"200": {"description": "OK"}}},
                },
                "/api/v2/users/{id}/contact": {
                    "get": {"summary": "Get contact", "responses": {"200": {"description": "OK"}}},
                },
            },
        },
        True,
    ),
    (
        "SC-003",
        "user-service",
        "non_breaking_001",
        "Add optional 'avatar_url' field — non-breaking addition",
        {
            "openapi": "3.0.0",
            "info": {"title": "User Service", "version": "2.2.2"},
            "paths": {
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
                                    "email":      {"type": "string"},
                                    "name":       {"type": "string"},
                                    "avatar_url": {"type": "string"},  # NEW optional field
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
                "/api/v2/users/{id}/preferences": {  # NEW endpoint
                    "get": {"summary": "Get preferences", "responses": {"200": {"description": "OK"}}},
                },
            },
        },
        False,
    ),
    (
        "SC-004",
        "user-service",
        "breaking_003",
        "Change 'id' field type from string to integer — breaks all consumers",
        {
            "openapi": "3.0.0",
            "info": {"title": "User Service", "version": "2.3.0"},
            "paths": {
                "/api/v2/users": {
                    "get": {"summary": "List users", "responses": {"200": {"description": "OK"}}},
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
                                    "id":    {"type": "integer"},  # changed from string
                                },
                            }}},
                        },
                        "responses": {"201": {"description": "Created"}},
                    },
                },
                "/api/v2/users/{id}": {
                    "get": {"summary": "Get user", "responses": {"200": {"description": "OK"}}},
                },
                "/api/v2/users/{id}/contact": {
                    "get": {"summary": "Get contact", "responses": {"200": {"description": "OK"}}},
                },
            },
        },
        True,
    ),
]


async def inject():
    print("=== K11tech Microservice QA — Contract Injection ===\n")

    registry_db = os.getenv("CONTRACT_REGISTRY_DB", "contract_registry.db")
    manifest: list[dict] = []

    async with ContractRegistry(registry_db) as registry:
        for scenario_id, service, sha, description, openapi_dict, is_breaking in SCENARIOS:
            contract = extract_openapi_contract(
                yaml.dump(openapi_dict), service, f"k11techlab/{service}", sha
            )
            await registry.store_contract(contract)
            manifest.append({
                "scenario_id":  scenario_id,
                "service":      service,
                "sha":          sha,
                "description":  description,
                "version":      contract.version,
                "endpoints":    len(contract.endpoints),
                "is_breaking":  is_breaking,
            })
            status = "BREAKING" if is_breaking else "NON-BREAKING"
            print(f"  ✓ [{status}] {scenario_id}: {description[:60]}...")

    # Write manifest for evaluation harness
    manifest_path = os.path.join(os.path.dirname(__file__), "scenarios_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n✅ Injected {len(SCENARIOS)} contract scenarios.")
    print(f"   Manifest: {manifest_path}")


if __name__ == "__main__":
    asyncio.run(inject())
