# -*- coding: utf-8 -*-
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
"""
Integration tests for the cross-repo impact pipeline.
These tests run the full Phase 1 + Phase 2 flow without the LLM agents
(Phase 3 is mocked to avoid API calls in CI).
"""
import asyncio
import pytest
import pytest_asyncio
import yaml
from unittest.mock import AsyncMock, patch

from contracts.registry import ContractRegistry
from graph.dependency_graph import ConsumptionEdge, ServiceNode
from graph.graph_store import GraphStore
from analyzer.contract_extractor import extract_openapi_contract
from analyzer.change_detector import diff_endpoints
from analyzer.impact_scorer import score_impact
from contracts.diff import diff_contracts

# ── Fixtures ──────────────────────────────────────────────────────────────────

OPENAPI_BASE = {
    "openapi": "3.0.0",
    "info": {"title": "User Service", "version": "2.0.0"},
    "paths": {
        "/api/v2/users/{id}": {
            "get": {"summary": "Get user",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/v2/users": {
            "post": {
                "summary": "Create user",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["email"],
                    "properties": {"email": {"type": "string"}},
                }}}},
                "responses": {"201": {"description": "Created"}},
            },
        },
    },
}

OPENAPI_HEAD_BREAKING = {
    "openapi": "3.0.0",
    "info": {"title": "User Service", "version": "3.0.0"},
    "paths": {
        # /api/v2/users/{id} REMOVED
        "/api/v2/users": {
            "post": {
                "summary": "Create user",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["email", "phone"],
                    "properties": {
                        "email": {"type": "string"},
                        "phone": {"type": "string"},
                    },
                }}}},
                "responses": {"201": {"description": "Created"}},
            },
        },
    },
}


@pytest_asyncio.fixture
async def populated_stores(tmp_path):
    """Fixture: set up in-memory registry + graph with test topology."""
    registry = ContractRegistry(":memory:")
    await registry.connect()

    store = GraphStore(":memory:")
    await store.connect()

    # Register services
    for svc in ("user-service", "order-service", "payment-service", "notification-svc"):
        await registry.register_service(svc, f"k11techlab/{svc}")
        await store.add_service(ServiceNode(name=svc, repo=f"k11techlab/{svc}",
                                            team="test", slack_channel="#test"))

    # Graph edges
    await store.record_consumption(ConsumptionEdge(
        "order-service", "user-service", "/api/v2/users/{id}", ["GET"], "high"))
    await store.record_consumption(ConsumptionEdge(
        "payment-service", "user-service", "/api/v2/users/{id}", ["GET"], "critical"))
    await store.record_consumption(ConsumptionEdge(
        "notification-svc", "user-service", "/api/v2/users/{id}/contact", ["GET"], "medium"))

    # Store base contract
    base_contract = extract_openapi_contract(
        yaml.dump(OPENAPI_BASE), "user-service", "k11techlab/user-service", "abc"
    )
    await registry.store_contract(base_contract)

    yield registry, store

    await registry.close()
    await store.close()


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_breaking_diff_detected(populated_stores):
    registry, store = populated_stores
    base = await registry.get_contract("user-service")
    head = extract_openapi_contract(
        yaml.dump(OPENAPI_HEAD_BREAKING), "user-service", "k11techlab/user-service", "xyz"
    )
    diff = diff_endpoints(base, head)
    assert diff.has_breaking_changes
    assert any(c.is_breaking for c in diff.changes)


@pytest.mark.asyncio
async def test_impact_score_nonzero_for_breaking(populated_stores):
    registry, store = populated_stores
    base = await registry.get_contract("user-service")
    head = extract_openapi_contract(
        yaml.dump(OPENAPI_HEAD_BREAKING), "user-service", "k11techlab/user-service", "xyz"
    )
    diff = diff_endpoints(base, head)
    graph = await store.load_graph()
    assessment = score_impact(diff, graph, "user-service")

    assert assessment.impact_radius >= 2
    assert assessment.impact_score > 0.0
    assert "order-service" in assessment.affected_services
    assert "payment-service" in assessment.affected_services


@pytest.mark.asyncio
async def test_hitl_triggered_for_high_impact(populated_stores):
    registry, store = populated_stores
    base = await registry.get_contract("user-service")
    head = extract_openapi_contract(
        yaml.dump(OPENAPI_HEAD_BREAKING), "user-service", "k11techlab/user-service", "xyz"
    )
    diff = diff_endpoints(base, head)
    graph = await store.load_graph()
    assessment = score_impact(diff, graph, "user-service")

    # Should trigger HITL — we have 2 critical/high consumers + breaking changes
    # Either impact_score threshold or breaking_consumer_count should fire
    assert assessment.hitl_required


@pytest.mark.asyncio
async def test_non_breaking_change_no_hitl(populated_stores):
    registry, store = populated_stores
    base = await registry.get_contract("user-service")

    # Non-breaking: just an optional field added
    non_breaking = {
        "openapi": "3.0.0",
        "info": {"title": "User Service", "version": "2.1.0"},
        "paths": {
            "/api/v2/users/{id}": {
                "get": {"summary": "Get user", "responses": {"200": {"description": "OK"}}},
            },
            "/api/v2/users": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "required": ["email"],
                        "properties": {
                            "email":  {"type": "string"},
                            "avatar": {"type": "string"},  # new optional
                        },
                    }}}},
                    "responses": {"201": {"description": "Created"}},
                },
            },
        },
    }
    head = extract_openapi_contract(
        yaml.dump(non_breaking), "user-service", "k11techlab/user-service", "xyz2"
    )
    diff = diff_endpoints(base, head)
    graph = await store.load_graph()
    assessment = score_impact(diff, graph, "user-service")

    assert not diff.has_breaking_changes
    assert not assessment.hitl_required


@pytest.mark.asyncio
async def test_contract_registry_roundtrip(populated_stores):
    registry, _ = populated_stores
    contract = await registry.get_contract("user-service")
    assert contract is not None
    assert contract.service_name == "user-service"
    assert len(contract.endpoints) > 0


@pytest.mark.asyncio
async def test_graph_downstream_consumers(populated_stores):
    _, store = populated_stores
    consumers = await store.get_downstream("user-service")
    assert "order-service" in consumers
    assert "payment-service" in consumers
