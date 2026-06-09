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
mcps/contract_registry_mcp/server.py
──────────────────────────────────────
FastMCP server exposing the ContractRegistry as MCP tools.

Tools:
  register_service   — Register a new service with its repo and contract path
  store_contract     — Store a versioned contract snapshot
  get_contract       — Retrieve the current contract for a service
  get_contract_history — Get all versions of a service's contract
  search_consumers   — Find all services that consume a given endpoint
  list_services      — List all registered services

Start:
  uvicorn mcps.contract_registry_mcp.server:app --port 8010
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI
from fastmcp import FastMCP

from contracts.models import ContractFormat, Endpoint, ServiceContract
from contracts.registry import ContractRegistry

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("CONTRACT_REGISTRY_DB", "contract_registry.db")

_registry: Optional[ContractRegistry] = None

mcp = FastMCP("contract-registry-mcp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _registry
    _registry = ContractRegistry(db_path=DB_PATH)
    await _registry.connect()
    logger.info("Contract Registry MCP started (db=%s)", DB_PATH)
    yield
    await _registry.close()
    logger.info("Contract Registry MCP stopped")


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def register_service(
    name: str,
    repo: str,
    contract_path: str = "",
) -> dict:
    """
    Register a new service with the contract registry.

    Args:
        name:          Service name (e.g. "user-service")
        repo:          GitHub repo slug (e.g. "org/user-service")
        contract_path: Path to the contract file in the repo
    """
    await _registry.register_service(name, repo, contract_path)
    return {"status": "registered", "service": name, "repo": repo}


@mcp.tool()
async def store_contract(contract: dict) -> dict:
    """
    Store a versioned contract snapshot.

    Args:
        contract: ServiceContract dict with fields:
                  service_name, repo, format, version, sha,
                  recorded_at, endpoints, raw
    """
    sc = _dict_to_contract(contract)
    await _registry.store_contract(sc)
    return {
        "status":       "stored",
        "service":      sc.service_name,
        "sha":          sc.sha,
        "endpoints":    len(sc.endpoints),
    }


@mcp.tool()
async def get_contract(
    service_name: str,
    sha: Optional[str] = None,
) -> dict:
    """
    Retrieve a service's contract (latest if sha not specified).

    Args:
        service_name: Name of the service
        sha:          Specific git SHA to retrieve (optional)
    """
    contract = await _registry.get_contract(service_name, sha)
    if contract is None:
        return {"contract": None, "found": False}
    return {"contract": _contract_to_dict(contract), "found": True}


@mcp.tool()
async def get_contract_history(
    service_name: str,
    limit: int = 10,
) -> dict:
    """
    Get all stored contract versions for a service.

    Args:
        service_name: Name of the service
        limit:        Maximum number of versions to return
    """
    history = await _registry.get_contract_history(service_name, limit)
    return {
        "service":  service_name,
        "count":    len(history),
        "versions": [_contract_to_dict(c) for c in history],
    }


@mcp.tool()
async def search_consumers(endpoint_pattern: str) -> dict:
    """
    Find all services whose contracts reference a given endpoint pattern.

    Args:
        endpoint_pattern: Path segment or endpoint pattern (e.g. "/api/v2/users")
    """
    services = await _registry.search_consumers(endpoint_pattern)
    return {"endpoint_pattern": endpoint_pattern, "consumers": services}


@mcp.tool()
async def list_services() -> dict:
    """List all registered services."""
    services = await _registry.list_services()
    return {"services": services, "count": len(services)}


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = mcp.get_asgi_app()


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _contract_to_dict(c: ServiceContract) -> dict:
    return {
        "service_name": c.service_name,
        "repo":         c.repo,
        "format":       c.format.value,
        "version":      c.version,
        "sha":          c.sha,
        "recorded_at":  c.recorded_at.isoformat(),
        "endpoints": [
            {
                "path": ep.path, "method": ep.method,
                "summary": ep.summary, "params": ep.params,
                "request_body": ep.request_body, "responses": ep.responses,
            }
            for ep in c.endpoints
        ],
        "raw": c.raw,
    }


def _dict_to_contract(d: dict) -> ServiceContract:
    from datetime import datetime
    return ServiceContract(
        service_name=d["service_name"],
        repo=d.get("repo", ""),
        format=ContractFormat(d["format"]),
        version=d.get("version", ""),
        sha=d.get("sha", ""),
        recorded_at=datetime.fromisoformat(d.get("recorded_at", "2026-01-01T00:00:00")),
        endpoints=[
            Endpoint(
                path=ep["path"], method=ep["method"],
                summary=ep.get("summary", ""),
                params=ep.get("params", []),
                request_body=ep.get("request_body"),
                responses=ep.get("responses", {}),
            )
            for ep in d.get("endpoints", [])
        ],
        raw=d.get("raw", {}),
    )
