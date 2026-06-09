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
contracts/registry.py
──────────────────────
In-process contract registry backed by SQLite (aiosqlite).

Used directly by the Contract Registry MCP server and in tests.
Stores versioned ServiceContract snapshots keyed by (service_name, sha).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from contracts.models import (
    ContractFormat,
    Endpoint,
    ServiceContract,
)

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS services (
    name         TEXT PRIMARY KEY,
    repo         TEXT NOT NULL,
    contract_path TEXT,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name TEXT NOT NULL,
    sha          TEXT NOT NULL,
    version      TEXT NOT NULL,
    format       TEXT NOT NULL,
    endpoints_json TEXT NOT NULL,
    raw_json     TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    UNIQUE(service_name, sha)
);

CREATE INDEX IF NOT EXISTS idx_contracts_service ON contracts(service_name);
CREATE INDEX IF NOT EXISTS idx_contracts_service_sha ON contracts(service_name, sha);
"""


class ContractRegistry:
    """Async SQLite-backed contract storage."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> "ContractRegistry":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._db.commit()
        logger.info("ContractRegistry connected: %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Service registration ──────────────────────────────────────────────────

    async def register_service(
        self,
        name: str,
        repo: str,
        contract_path: str = "",
    ) -> None:
        await self._db.execute(
            """INSERT INTO services(name, repo, contract_path, registered_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET repo=excluded.repo,
                   contract_path=excluded.contract_path""",
            (name, repo, contract_path, datetime.now(timezone.utc).isoformat()),
        )
        await self._db.commit()
        logger.info("Registered service: %s (%s)", name, repo)

    async def list_services(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM services") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Contract storage ──────────────────────────────────────────────────────

    async def store_contract(self, contract: ServiceContract) -> None:
        endpoints_json = json.dumps([
            {
                "path": ep.path, "method": ep.method,
                "summary": ep.summary, "params": ep.params,
                "request_body": ep.request_body, "responses": ep.responses,
            }
            for ep in contract.endpoints
        ])
        raw_json = json.dumps(contract.raw)
        await self._db.execute(
            """INSERT INTO contracts
               (service_name, sha, version, format, endpoints_json, raw_json, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(service_name, sha) DO UPDATE SET
                   version=excluded.version,
                   endpoints_json=excluded.endpoints_json,
                   raw_json=excluded.raw_json""",
            (
                contract.service_name, contract.sha, contract.version,
                contract.format.value, endpoints_json, raw_json,
                contract.recorded_at.isoformat(),
            ),
        )
        await self._db.commit()
        logger.info(
            "Stored contract: %s@%s (format=%s, endpoints=%d)",
            contract.service_name, contract.sha[:8],
            contract.format.value, len(contract.endpoints),
        )

    async def get_contract(
        self,
        service_name: str,
        sha: Optional[str] = None,
    ) -> Optional[ServiceContract]:
        """
        Retrieve a contract by service name.
        If sha is None, returns the most recent version.
        """
        if sha:
            query = (
                "SELECT * FROM contracts WHERE service_name=? AND sha=?",
                (service_name, sha),
            )
        else:
            query = (
                "SELECT * FROM contracts WHERE service_name=? ORDER BY id DESC LIMIT 1",
                (service_name,),
            )
        async with self._db.execute(*query) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return _row_to_contract(dict(row))

    async def get_contract_history(
        self,
        service_name: str,
        limit: int = 10,
    ) -> list[ServiceContract]:
        async with self._db.execute(
            "SELECT * FROM contracts WHERE service_name=? ORDER BY id DESC LIMIT ?",
            (service_name, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_contract(dict(r)) for r in rows]

    async def search_consumers(self, endpoint_pattern: str) -> list[str]:
        """
        Find services whose stored contracts consume a given endpoint pattern.
        Simple substring match on the stored raw JSON.
        """
        async with self._db.execute(
            "SELECT DISTINCT service_name FROM contracts WHERE raw_json LIKE ?",
            (f"%{endpoint_pattern}%",),
        ) as cur:
            rows = await cur.fetchall()
        return [r["service_name"] for r in rows]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_contract(row: dict) -> ServiceContract:
    endpoints_data = json.loads(row["endpoints_json"])
    endpoints = [
        Endpoint(
            path=ep["path"],
            method=ep["method"],
            summary=ep.get("summary", ""),
            params=ep.get("params", []),
            request_body=ep.get("request_body"),
            responses=ep.get("responses", {}),
        )
        for ep in endpoints_data
    ]
    return ServiceContract(
        service_name=row["service_name"],
        repo="",  # not stored in contracts table; fetch from services if needed
        format=ContractFormat(row["format"]),
        version=row["version"],
        sha=row["sha"],
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
        endpoints=endpoints,
        raw=json.loads(row["raw_json"]),
    )
