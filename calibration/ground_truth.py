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
calibration/ground_truth.py
─────────────────────────────
Fetches ground-truth labels for calibration by checking whether consumer
CI pipelines failed after a provider PR merged.

Strategy
────────
For each (run_id, repo_name, pr_number) still pending in the calibration
store, we:

  1. Fetch the PR merge commit SHA and merged_at timestamp via GitHub API.
  2. For every consumer registered in the dependency graph for that provider,
     check if the consumer's default branch had a failed CI run in the
     GT_WINDOW_HOURS after the provider merged.
  3. Label consumer as BREAKING if any CI run failed; COMPATIBLE otherwise.

This is the "CI failure signal" approach — automatable and reproducible.
The window is intentionally generous (default 48 h) to capture delayed
integration failures.

Manual override
───────────────
`resolve_manual(store, run_id, consumer_verdicts)` allows a researcher
to supply hand-labelled verdicts, recorded with gt_source="manual".
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from calibration.store import CalibrationStore
from graph.graph_store import GraphStore

logger = logging.getLogger(__name__)

GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
GRAPH_STORE_DB = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
GT_WINDOW_HOURS = int(os.getenv("GT_WINDOW_HOURS", "48"))


def _gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


async def _fetch_pr_merge_info(client: httpx.AsyncClient, repo: str, pr_number: int) -> dict | None:
    """Return {merged_at, merge_commit_sha} or None if not merged."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    resp = await client.get(url, headers=_gh_headers())
    if resp.status_code != 200:
        logger.warning("GitHub PR fetch failed: %s %s", resp.status_code, url)
        return None
    data = resp.json()
    if not data.get("merged_at"):
        return None
    return {"merged_at": data["merged_at"], "merge_commit_sha": data.get("merge_commit_sha", "")}


async def _get_default_branch(client: httpx.AsyncClient, repo: str) -> str:
    url = f"https://api.github.com/repos/{repo}"
    resp = await client.get(url, headers=_gh_headers())
    if resp.status_code == 200:
        return resp.json().get("default_branch", "main")
    return "main"


async def _had_ci_failure(
    client: httpx.AsyncClient,
    repo: str,
    after: datetime,
    before: datetime,
) -> bool:
    """
    Return True if any workflow run on the default branch of `repo` failed
    between `after` and `before`.
    """
    branch = await _get_default_branch(client, repo)
    # GitHub API: list workflow runs, filter by branch and created window
    url = f"https://api.github.com/repos/{repo}/actions/runs"
    params = {
        "branch": branch,
        "status": "failure",
        "created": f">{after.isoformat()}",
        "per_page": 10,
    }
    resp = await client.get(url, headers=_gh_headers(), params=params)
    if resp.status_code != 200:
        logger.warning("GitHub Actions fetch failed: %s %s", resp.status_code, url)
        return False

    runs = resp.json().get("workflow_runs", [])
    # Filter to runs within the window
    for run in runs:
        run_time_str = run.get("created_at", "")
        if not run_time_str:
            continue
        run_time = datetime.fromisoformat(run_time_str.replace("Z", "+00:00"))
        if after <= run_time <= before:
            logger.info(
                "CI failure found in %s: run #%d at %s",
                repo, run["id"], run_time_str,
            )
            return True
    return False


async def fetch_ci_ground_truth(
    store: CalibrationStore,
    graph_store_db: str = GRAPH_STORE_DB,
    window_hours: int = GT_WINDOW_HOURS,
    older_than_hours: int = 24,
) -> dict[str, int]:
    """
    Resolve pending calibration rows by checking consumer CI failure.

    Returns counts: {"resolved": N, "skipped": M, "errors": K}
    """
    pending = await store.get_pending_runs(older_than_hours=older_than_hours)
    if not pending:
        logger.info("No pending calibration runs to resolve.")
        return {"resolved": 0, "skipped": 0, "errors": 0}

    counts = {"resolved": 0, "skipped": 0, "errors": 0}

    async with httpx.AsyncClient(timeout=30) as client:
        async with GraphStore(graph_store_db) as gs:
            for pending_run in pending:
                run_id    = pending_run["run_id"]
                repo      = pending_run["repo_name"]
                pr_number = pending_run["pr_number"]

                try:
                    merge_info = await _fetch_pr_merge_info(client, repo, pr_number)
                    if not merge_info:
                        logger.info("PR #%d in %s not yet merged — skipping", pr_number, repo)
                        counts["skipped"] += 1
                        continue

                    merged_at = datetime.fromisoformat(
                        merge_info["merged_at"].replace("Z", "+00:00")
                    )
                    window_end = merged_at + timedelta(hours=window_hours)

                    # Get all consumers registered for this provider
                    provider = repo.split("/")[-1]
                    consumers = await gs.get_consumers_for_provider(provider)

                    verdicts: dict[str, str] = {}
                    for consumer_repo in consumers:
                        had_failure = await _had_ci_failure(
                            client, consumer_repo, merged_at, window_end
                        )
                        verdicts[consumer_repo.split("/")[-1]] = (
                            "BREAKING" if had_failure else "COMPATIBLE"
                        )

                    await store.resolve_run_ground_truth(run_id, verdicts, gt_source="ci_failure")
                    counts["resolved"] += len(verdicts)
                    logger.info(
                        "Resolved %d consumers for run %s (PR #%d %s)",
                        len(verdicts), run_id, pr_number, repo,
                    )

                except Exception as exc:
                    logger.error("Error resolving run %s: %s", run_id, exc)
                    counts["errors"] += 1

    return counts


async def resolve_manual(
    store: CalibrationStore,
    run_id: str,
    consumer_verdicts: dict[str, str],
) -> None:
    """
    Manually supply ground-truth labels for a run.

    consumer_verdicts: {"k11-payment-service": "BREAKING", "k11-order-service": "COMPATIBLE"}
    """
    await store.resolve_run_ground_truth(run_id, consumer_verdicts, gt_source="manual")
    logger.info("Manual ground truth set for run %s: %d consumers", run_id, len(consumer_verdicts))
