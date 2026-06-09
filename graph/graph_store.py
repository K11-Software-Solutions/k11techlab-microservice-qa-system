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
graph/graph_store.py
─────────────────────
Persistent SQLite-backed store for the DependencyGraph.

The graph is serialised to node-link JSON on each mutation and stored
in a single-row table. Reads deserialise back to a live NetworkX DiGraph.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from graph.dependency_graph import ConsumptionEdge, DependencyGraph, ServiceNode

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS graph_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_json  TEXT NOT NULL,
    saved_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class GraphStore:
    """
    Async SQLite store wrapping a DependencyGraph.

    Usage:
        async with GraphStore("dependency.db") as store:
            await store.add_service(ServiceNode(...))
            await store.record_consumption(ConsumptionEdge(...))
            graph = await store.load_graph()
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._graph: Optional[DependencyGraph] = None

    async def __aenter__(self) -> "GraphStore":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._db.commit()
        self._graph = await self._load_or_create()
        logger.info("GraphStore connected: %s", self._db_path)

    async def close(self) -> None:
        if self._graph is not None:
            await self._persist()
        if self._db:
            await self._db.close()
            self._db = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def add_service(self, node: ServiceNode) -> None:
        self._graph.add_service(node)
        await self._persist()
        logger.info("GraphStore: added service node %s", node.name)

    async def record_consumption(self, edge: ConsumptionEdge) -> None:
        self._graph.add_consumption(edge)
        await self._persist()
        logger.info(
            "GraphStore: recorded %s → %s (%s)",
            edge.consumer, edge.provider, edge.endpoint_pattern,
        )

    async def load_graph(self) -> DependencyGraph:
        return self._graph

    async def get_downstream(self, provider: str) -> list[str]:
        return self._graph.downstream_consumers(provider)

    async def get_impact_score(
        self, provider: str, changed_endpoints: list[str]
    ) -> float:
        return self._graph.impact_score(provider, changed_endpoints)

    async def export_graph(self) -> dict:
        return self._graph.to_dict()

    # ── Persistence ────────────────────────────────────────────────────────────

    async def _persist(self) -> None:
        graph_json = json.dumps(self._graph.to_dict())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO graph_snapshots(graph_json, saved_at) VALUES (?, ?)",
            (graph_json, now),
        )
        # Keep meta pointer to latest snapshot
        await self._db.execute(
            "INSERT INTO graph_meta(key, value) VALUES ('latest_snapshot_time', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now,),
        )
        await self._db.commit()

    async def _load_or_create(self) -> DependencyGraph:
        async with self._db.execute(
            "SELECT graph_json FROM graph_snapshots ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row:
            logger.debug("GraphStore: restoring persisted graph")
            return DependencyGraph.from_dict(json.loads(row["graph_json"]))
        logger.debug("GraphStore: starting with empty graph")
        return DependencyGraph()
