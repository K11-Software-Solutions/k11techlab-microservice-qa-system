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
calibration/store.py
─────────────────────
Async SQLite store for the calibration study.

Schema
──────
calibration_log
  run_id        TEXT    — pipeline run UUID
  agent         TEXT    — e.g. "contract_compliance_agent:k11-payment-service"
  consumer      TEXT    — consumer service name
  confidence    REAL    — agent self-reported confidence [0.0, 1.0]
  verdict       TEXT    — COMPATIBLE | BREAKING | UNCERTAIN
  ground_truth  TEXT    — BREAKING | COMPATIBLE | UNKNOWN (filled later)
  gt_source     TEXT    — ci_failure | manual | proxy | pending
  repo_name     TEXT    — provider repo (e.g. "org/k11-user-service")
  pr_number     INTEGER
  hop_depth     INTEGER — 1 = direct, 2+ = transitive
  recorded_at   TEXT    — ISO-8601 timestamp of pipeline run
  gt_resolved_at TEXT   — ISO-8601 timestamp when ground truth was set
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

CALIBRATION_DB = os.getenv("CALIBRATION_DB", "calibration.db")


class CalibrationStore:
    """Async context-manager wrapper around the calibration SQLite database."""

    def __init__(self, db_path: str = CALIBRATION_DB) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> "CalibrationStore":
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()
        return self

    async def __aexit__(self, *_) -> None:
        if self._conn:
            await self._conn.close()

    async def _create_tables(self) -> None:
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS calibration_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT    NOT NULL,
                agent           TEXT    NOT NULL,
                consumer        TEXT    NOT NULL,
                confidence      REAL    NOT NULL,
                verdict         TEXT    NOT NULL,
                ground_truth    TEXT    NOT NULL DEFAULT 'UNKNOWN',
                gt_source       TEXT    NOT NULL DEFAULT 'pending',
                repo_name       TEXT    NOT NULL DEFAULT '',
                pr_number       INTEGER NOT NULL DEFAULT 0,
                hop_depth       INTEGER NOT NULL DEFAULT 1,
                recorded_at     TEXT    NOT NULL,
                gt_resolved_at  TEXT
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_id ON calibration_log(run_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gt ON calibration_log(ground_truth)"
        )
        await self._conn.commit()

    async def record_run(
        self,
        run_id: str,
        repo_name: str,
        pr_number: int,
        compliance_results: list[dict],
        agent_confidence_scores: dict[str, float],
        hop_depths: dict[str, int] | None = None,
    ) -> int:
        """
        Insert one row per consumer verdict from a completed pipeline run.
        Returns the number of rows inserted.
        """
        now = datetime.now(timezone.utc).isoformat()
        hop_depths = hop_depths or {}
        rows = []

        for result in compliance_results:
            consumer  = result.get("consumer", "")
            verdict   = result.get("verdict", "UNCERTAIN")
            agent_key = f"contract_compliance_agent:{consumer}"
            confidence = agent_confidence_scores.get(agent_key, result.get("confidence", 0.5))
            hop_depth  = hop_depths.get(consumer, 1)

            rows.append((
                run_id, agent_key, consumer, float(confidence), verdict,
                "UNKNOWN", "pending",
                repo_name, pr_number, hop_depth, now, None,
            ))

        if rows:
            await self._conn.executemany(
                """INSERT INTO calibration_log
                   (run_id, agent, consumer, confidence, verdict,
                    ground_truth, gt_source,
                    repo_name, pr_number, hop_depth, recorded_at, gt_resolved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            await self._conn.commit()

        logger.info("CalibrationStore: recorded %d rows for run %s", len(rows), run_id)
        return len(rows)

    async def resolve_ground_truth(
        self,
        run_id: str,
        consumer: str,
        ground_truth: str,
        gt_source: str,
    ) -> None:
        """Update ground truth for a specific (run_id, consumer) pair."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """UPDATE calibration_log
               SET ground_truth = ?, gt_source = ?, gt_resolved_at = ?
               WHERE run_id = ? AND consumer = ?""",
            (ground_truth, gt_source, now, run_id, consumer),
        )
        await self._conn.commit()

    async def resolve_run_ground_truth(
        self,
        run_id: str,
        verdicts: dict[str, str],
        gt_source: str,
    ) -> None:
        """Bulk-update ground truth for all consumers in a run."""
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (gt, gt_source, now, run_id, consumer)
            for consumer, gt in verdicts.items()
        ]
        await self._conn.executemany(
            """UPDATE calibration_log
               SET ground_truth = ?, gt_source = ?, gt_resolved_at = ?
               WHERE run_id = ? AND consumer = ?""",
            rows,
        )
        await self._conn.commit()

    async def get_resolved_rows(self) -> list[dict]:
        """Return all rows where ground truth has been resolved (not pending/UNKNOWN)."""
        async with self._conn.execute(
            """SELECT agent, consumer, confidence, verdict, ground_truth,
                      gt_source, repo_name, hop_depth, recorded_at
               FROM calibration_log
               WHERE ground_truth != 'UNKNOWN' AND gt_source != 'pending'
               ORDER BY recorded_at"""
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_pending_runs(self, older_than_hours: int = 24) -> list[dict]:
        """
        Return distinct (run_id, repo_name, pr_number) where ground truth
        is still pending and the run is old enough for CI to have completed.
        """
        async with self._conn.execute(
            """SELECT DISTINCT run_id, repo_name, pr_number, recorded_at
               FROM calibration_log
               WHERE gt_source = 'pending'
                 AND datetime(recorded_at) <= datetime('now', ? || ' hours')
               ORDER BY recorded_at""",
            (f"-{older_than_hours}",),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def stats(self) -> dict:
        """Summary counts for reporting."""
        async with self._conn.execute(
            "SELECT COUNT(*) as total FROM calibration_log"
        ) as cur:
            total = (await cur.fetchone())["total"]
        async with self._conn.execute(
            "SELECT COUNT(*) as resolved FROM calibration_log WHERE gt_source != 'pending'"
        ) as cur:
            resolved = (await cur.fetchone())["resolved"]
        return {"total": total, "resolved": resolved, "pending": total - resolved}
