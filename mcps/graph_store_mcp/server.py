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
mcps/graph_store_mcp/server.py
────────────────────────────────
FastMCP server exposing the GraphStore / DependencyGraph as MCP tools.

Tools:
  add_service         — Add a service node to the graph
  record_consumption  — Record that service A consumes service B's endpoint
  get_downstream      — Get all downstream consumers of a service
  get_impact_score    — Compute impact score for a set of changed endpoints
  export_graph        — Export full graph as node-link JSON
  get_direct_consumers — Get only direct (one-hop) consumers
  get_impact_radius   — Count of all affected downstream consumers

Start:
  uvicorn mcps.graph_store_mcp.server:app --port 8011
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastmcp import FastMCP

from graph.dependency_graph import ConsumptionEdge, ServiceNode
from graph.graph_store import GraphStore

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")

_store: Optional[GraphStore] = None

mcp = FastMCP("graph-store-mcp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store
    _store = GraphStore(db_path=DB_PATH)
    await _store.connect()
    logger.info("Graph Store MCP started (db=%s)", DB_PATH)
    yield
    await _store.close()
    logger.info("Graph Store MCP stopped")


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def add_service(
    name: str,
    repo: str,
    team: str = "",
    slack_channel: str = "",
) -> dict:
    """
    Add a service node to the dependency graph.

    Args:
        name:          Service name (must be unique)
        repo:          GitHub repo slug (e.g. "org/service-name")
        team:          Owning team name
        slack_channel: Team's Slack channel for notifications
    """
    node = ServiceNode(name=name, repo=repo, team=team, slack_channel=slack_channel)
    await _store.add_service(node)
    return {"status": "added", "service": name}


@mcp.tool()
async def record_consumption(
    consumer: str,
    provider: str,
    endpoint_pattern: str,
    methods: list[str] | None = None,
    criticality: str = "medium",
) -> dict:
    """
    Record that `consumer` depends on `provider`'s API.

    Args:
        consumer:         Service that calls the API
        provider:         Service that owns the API
        endpoint_pattern: Endpoint pattern consumed (e.g. "/api/v2/users/*")
        methods:          HTTP methods consumed (e.g. ["GET", "POST"])
        criticality:      Edge weight: low | medium | high | critical
    """
    edge = ConsumptionEdge(
        consumer=consumer,
        provider=provider,
        endpoint_pattern=endpoint_pattern,
        methods=methods or [],
        criticality=criticality,
    )
    await _store.record_consumption(edge)
    return {
        "status":   "recorded",
        "consumer": consumer,
        "provider": provider,
        "endpoint": endpoint_pattern,
    }


@mcp.tool()
async def get_downstream(provider: str) -> dict:
    """
    Get all downstream consumers of a provider service (direct + transitive).

    Args:
        provider: Service name to find consumers of
    """
    consumers = await _store.get_downstream(provider)
    return {"provider": provider, "consumers": consumers, "count": len(consumers)}


@mcp.tool()
async def get_direct_consumers(provider: str) -> dict:
    """
    Get only the direct (one-hop) consumers of a provider service.

    Args:
        provider: Service name
    """
    graph = await _store.load_graph()
    consumers = graph.direct_consumers(provider)
    return {"provider": provider, "direct_consumers": consumers, "count": len(consumers)}


@mcp.tool()
async def get_impact_radius(provider: str) -> dict:
    """
    Get the count of all downstream consumers affected by any change to provider.

    Args:
        provider: Service name
    """
    graph = await _store.load_graph()
    radius = graph.impact_radius(provider)
    return {"provider": provider, "impact_radius": radius}


@mcp.tool()
async def get_impact_score(
    provider: str,
    changed_endpoints: list[str],
) -> dict:
    """
    Compute weighted impact score [0.0, 1.0] for a set of changed endpoints.

    Args:
        provider:          Service that owns the changed endpoints
        changed_endpoints: List of changed endpoint keys (e.g. ["GET /api/v2/users/{id}"])
    """
    score = await _store.get_impact_score(provider, changed_endpoints)
    return {
        "provider":          provider,
        "changed_endpoints": changed_endpoints,
        "impact_score":      round(score, 4),
    }


@mcp.tool()
async def export_graph() -> dict:
    """Export the full dependency graph as a node-link JSON object."""
    return await _store.export_graph()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = mcp.get_asgi_app()
