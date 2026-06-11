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
        # HITL outcome labels — one row per (run_id, consumer) reviewer decision
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS hitl_outcomes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT    NOT NULL,
                consumer        TEXT    NOT NULL,
                hitl_decision   TEXT    NOT NULL,  -- 'approve' | 'override' | 'reject'
                reviewer        TEXT    NOT NULL DEFAULT 'unknown',
                comment         TEXT    NOT NULL DEFAULT '',
                recorded_at     TEXT    NOT NULL,
                UNIQUE(run_id, consumer)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hitl_run ON hitl_outcomes(run_id)"
        )
        # Serialised per-agent recalibration models (isotonic or Platt)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS calibration_models (
                agent           TEXT    NOT NULL,
                model_type      TEXT    NOT NULL,  -- 'isotonic' | 'platt'
                model_data      BLOB    NOT NULL,  -- pickled sklearn object
                n_samples       INTEGER NOT NULL DEFAULT 0,
                before_ece      REAL,
                after_ece       REAL,
                trained_at      TEXT    NOT NULL,
                PRIMARY KEY (agent, model_type)
            )
        """)
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

    async def record_hitl_outcome(
        self,
        run_id: str,
        consumer: str,
        hitl_decision: str,
        reviewer: str = "unknown",
        comment: str = "",
    ) -> None:
        """
        Record a reviewer's HITL decision for one consumer in a run.

        hitl_decision:
          'approve'  — reviewer confirms the change is (or may be) breaking;
                       BREAKING verdicts for this consumer are treated as correct.
          'override' — reviewer says the change is actually compatible;
                       BREAKING verdicts are treated as incorrect (false positive).
          'reject'   — reviewer rejected the whole PR at the run level; treated
                       same as 'approve' for per-consumer labelling.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO hitl_outcomes
               (run_id, consumer, hitl_decision, reviewer, comment, recorded_at)
               VALUES (?,?,?,?,?,?)""",
            (run_id, consumer, hitl_decision, reviewer, comment, now),
        )
        await self._conn.commit()
        logger.info(
            "HITL outcome recorded: run=%s consumer=%s decision=%s reviewer=%s",
            run_id, consumer, hitl_decision, reviewer,
        )

    async def get_hitl_labels(self) -> list[dict]:
        """
        Return all HITL-labelled rows joined with calibration_log confidence scores.
        Used as training data for recalibration when explicit ground truth is sparse.

        Label mapping:
          approve/reject → correct = 1  (BREAKING verdict was appropriate)
          override       → correct = 0  (BREAKING verdict was a false positive)
        """
        async with self._conn.execute(
            """SELECT cl.agent, cl.consumer, cl.confidence, cl.verdict,
                      ho.hitl_decision,
                      CASE ho.hitl_decision
                          WHEN 'override' THEN 0 ELSE 1
                      END AS correct
               FROM hitl_outcomes ho
               JOIN calibration_log cl
                 ON cl.run_id = ho.run_id AND cl.consumer = ho.consumer"""
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def save_model(
        self,
        agent: str,
        model_type: str,
        model_data: bytes,
        n_samples: int,
        before_ece: float | None,
        after_ece: float | None,
    ) -> None:
        """Persist a serialised sklearn calibration model for an agent."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO calibration_models
               (agent, model_type, model_data, n_samples, before_ece, after_ece, trained_at)
               VALUES (?,?,?,?,?,?,?)""",
            (agent, model_type, model_data, n_samples, before_ece, after_ece, now),
        )
        await self._conn.commit()
        logger.info(
            "Calibration model saved: agent=%s type=%s n=%d ece %.4f→%.4f",
            agent, model_type, n_samples,
            before_ece or 0.0, after_ece or 0.0,
        )

    async def load_model(self, agent: str, model_type: str = "isotonic") -> bytes | None:
        """Return pickled model bytes or None if no model exists for this agent."""
        async with self._conn.execute(
            "SELECT model_data FROM calibration_models WHERE agent=? AND model_type=?",
            (agent, model_type),
        ) as cursor:
            row = await cursor.fetchone()
        return row["model_data"] if row else None

    async def list_models(self) -> list[dict]:
        """Summary of all stored calibration models."""
        async with self._conn.execute(
            """SELECT agent, model_type, n_samples, before_ece, after_ece, trained_at
               FROM calibration_models ORDER BY agent, model_type"""
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_agent_scores_matrix(self) -> list[dict]:
        """
        Return one entry per run: {run_id, agent_scores: {agent: confidence}}.

        Used by AgentCorrelationMatrix to build the N×N covariance matrix from
        historical calibration_log entries.  Only rows with a resolved ground
        truth are included so noisy/unvalidated runs don't corrupt the estimate.
        """
        async with self._conn.execute(
            """SELECT run_id, agent, confidence
               FROM calibration_log
               WHERE ground_truth != 'UNKNOWN' AND gt_source != 'pending'
               ORDER BY run_id, agent"""
        ) as cursor:
            rows = await cursor.fetchall()

        # Pivot: {run_id: {agent: confidence}}
        runs: dict[str, dict[str, float]] = {}
        for row in rows:
            runs.setdefault(row["run_id"], {})[row["agent"]] = float(row["confidence"])

        return [
            {"run_id": run_id, "agent_scores": agent_scores}
            for run_id, agent_scores in runs.items()
        ]

    async def get_run_calibration_data(self) -> list[dict]:
        """
        Return one entry per resolved run for conformal threshold fitting (Feature 8).

        Each entry: {run_id, agent_scores: {agent: confidence}, any_wrong: bool}

        any_wrong = True when at least one consumer in the run received a verdict
        that did not match its resolved ground truth.
        """
        async with self._conn.execute(
            """SELECT run_id, agent, confidence, verdict, ground_truth
               FROM calibration_log
               WHERE ground_truth != 'UNKNOWN' AND gt_source != 'pending'
               ORDER BY run_id, agent"""
        ) as cursor:
            rows = await cursor.fetchall()

        runs: dict[str, dict] = {}
        for row in rows:
            run_id = row["run_id"]
            if run_id not in runs:
                runs[run_id] = {"agent_scores": {}, "any_wrong": False}
            runs[run_id]["agent_scores"][row["agent"]] = float(row["confidence"])
            if row["verdict"] != row["ground_truth"]:
                runs[run_id]["any_wrong"] = True

        return [
            {"run_id": rid, "agent_scores": d["agent_scores"], "any_wrong": d["any_wrong"]}
            for rid, d in runs.items()
        ]

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
