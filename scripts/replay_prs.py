#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/replay_prs.py
──────────────────────
Replay historical merged GitHub PRs through the pipeline to populate
calibration.db for the Paper 3 evaluation study.

How it works
────────────
1. Fetches merged PRs from one or more GitHub repos via the REST API.
2. For each PR, reconstructs the initial pipeline state (repo, pr_number,
   head_sha, base_sha, pr_diff) by fetching the PR diff from GitHub.
3. Runs the full pipeline (phases 1–4) against that historical state.
4. Calibration logging fires automatically (CALIBRATION_ENABLED=true).
5. Saves a replay manifest to replay_manifest.json for ground-truth
   resolution later.

Usage
─────
    # Replay up to 50 merged PRs from two repos
    python scripts/replay_prs.py \\
        --repos org/service-a org/service-b \\
        --limit 50 \\
        --output replay_manifest.json

    # Skip PRs older than 90 days (limits CI noise)
    python scripts/replay_prs.py \\
        --repos org/service-a \\
        --limit 30 \\
        --max-age-days 90

    # Dry run — list PRs without running pipeline
    python scripts/replay_prs.py \\
        --repos org/service-a \\
        --dry-run

Requirements
────────────
    GITHUB_TOKEN   — PAT with repo + read:org scope
    ANTHROPIC_API_KEY or OPENAI_API_KEY — for LLM agents
    GRAPH_STORE_DB must contain consumers registered for the provider services.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("replay_prs")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
CALIBRATION_DB = os.getenv("CALIBRATION_DB", "calibration.db")


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


# ── GitHub helpers ────────────────────────────────────────────────────────────

async def fetch_merged_prs(
    client: httpx.AsyncClient,
    repo: str,
    limit: int,
    max_age_days: int | None,
) -> list[dict]:
    """Return up to `limit` merged PRs from `repo`, newest first."""
    prs = []
    page = 1
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if max_age_days else None
    )

    while len(prs) < limit:
        url = f"https://api.github.com/repos/{repo}/pulls"
        resp = await client.get(url, headers=_gh_headers(), params={
            "state": "closed", "sort": "updated", "direction": "desc",
            "per_page": min(50, limit - len(prs)), "page": page,
        })
        if resp.status_code != 200:
            logger.error("GitHub API error %d for %s", resp.status_code, repo)
            break

        batch = resp.json()
        if not batch:
            break

        for pr in batch:
            if not pr.get("merged_at"):
                continue   # closed but not merged
            if cutoff:
                merged = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
                if merged < cutoff:
                    return prs   # older than cutoff — stop
            prs.append(pr)
            if len(prs) >= limit:
                break

        page += 1

    logger.info("Fetched %d merged PRs from %s", len(prs), repo)
    return prs


async def fetch_pr_diff(client: httpx.AsyncClient, repo: str, pr_number: int) -> str:
    """Fetch the unified diff for a PR."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    resp = await client.get(url, headers={
        **_gh_headers(), "Accept": "application/vnd.github.diff",
    })
    if resp.status_code == 200:
        return resp.text
    logger.warning("Could not fetch diff for PR #%d in %s: %s", pr_number, repo, resp.status_code)
    return ""


# ── Pipeline runner ───────────────────────────────────────────────────────────

async def run_pipeline_for_pr(pr: dict, repo: str, dry_run: bool) -> dict | None:
    """
    Build initial state from a GitHub PR dict and run the full pipeline.
    Returns the final state dict or None on failure.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from pipeline.orchestrator import build_orchestrator
    from pipeline.state import initial_state

    pr_number  = pr["number"]
    head_sha   = pr["head"]["sha"]
    base_sha   = pr["base"]["sha"]
    base_branch = pr["base"]["ref"]
    run_id     = str(uuid.uuid4())

    logger.info("PR #%d %s — run_id=%s", pr_number, repo, run_id)

    if dry_run:
        return {"run_id": run_id, "pr_number": pr_number, "repo": repo, "dry_run": True}

    async with httpx.AsyncClient(timeout=30) as client:
        pr_diff = await fetch_pr_diff(client, repo, pr_number)

    state = initial_state(
        run_id=run_id,
        pr_number=pr_number,
        repo_name=repo,
        pr_diff=pr_diff,
        base_branch=base_branch,
        triggered_by="replay_script",
        head_sha=head_sha,
        base_sha=base_sha,
    )

    checkpointer = MemorySaver()
    pipeline = build_orchestrator().compile(checkpointer=checkpointer)

    try:
        config = {"configurable": {"thread_id": run_id}}
        final_state = await pipeline.ainvoke(state, config=config)

        # Calibration recording (same logic as webhook._record_calibration)
        from calibration.store import CalibrationStore
        results = (
            final_state.get("adjusted_compliance_results")
            or final_state.get("compliance_results", [])
        )
        if results:
            hop_depths = {
                c["consumer"]: c.get("hop_depth", 1)
                for c in final_state.get("downstream_consumers", [])
                if isinstance(c, dict)
            }
            async with CalibrationStore(CALIBRATION_DB) as store:
                await store.record_run(
                    run_id=run_id,
                    repo_name=repo,
                    pr_number=pr_number,
                    compliance_results=results,
                    agent_confidence_scores=final_state.get("agent_confidence_scores", {}),
                    hop_depths=hop_depths,
                )

        verdict = (final_state.get("summary") or {}).get("overall_verdict", "N/A")
        logger.info("PR #%d %s → verdict=%s consumers=%d",
                    pr_number, repo, verdict, len(results))

        return {
            "run_id":      run_id,
            "pr_number":   pr_number,
            "repo":        repo,
            "merged_at":   pr.get("merged_at"),
            "head_sha":    head_sha,
            "verdict":     verdict,
            "consumers":   len(results),
        }

    except Exception as exc:
        logger.error("Pipeline failed for PR #%d %s: %s", pr_number, repo, exc)
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> None:
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set — API rate limits will apply (60 req/hr)")

    manifest = []
    total_consumers = 0
    skipped = 0

    async with httpx.AsyncClient(timeout=30) as client:
        for repo in args.repos:
            logger.info("=== Fetching PRs from %s ===", repo)
            prs = await fetch_merged_prs(client, repo, args.limit, args.max_age_days)

            if args.dry_run:
                for pr in prs:
                    print(f"  PR #{pr['number']:5d}  merged={pr['merged_at'][:10]}  {pr['title'][:60]}")
                continue

            for i, pr in enumerate(prs, 1):
                logger.info("[%d/%d] Processing PR #%d", i, len(prs), pr["number"])
                result = await run_pipeline_for_pr(pr, repo, dry_run=False)
                if result:
                    manifest.append(result)
                    total_consumers += result.get("consumers", 0)
                else:
                    skipped += 1

                # Respect GitHub secondary rate limit between runs
                if i < len(prs):
                    await asyncio.sleep(args.delay)

    if args.dry_run:
        return

    # Save manifest
    out = args.output or "replay_manifest.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Replay complete")
    print(f"  PRs processed    : {len(manifest)}")
    print(f"  PRs skipped      : {skipped}")
    print(f"  Consumer rows    : {total_consumers}")
    print(f"  Calibration DB   : {CALIBRATION_DB}")
    print(f"  Manifest saved   : {out}")
    print(f"\nNext steps:")
    print(f"  1. Resolve ground truth:")
    print(f"     POST /calibration/fetch-ci-ground-truth")
    print(f"     — or —")
    print(f"     python scripts/plot_calibration.py --fetch-gt")
    print(f"  2. Generate calibration plot:")
    print(f"     python scripts/plot_calibration.py --output calibration.png")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay historical GitHub PRs through the pipeline for calibration study."
    )
    parser.add_argument(
        "--repos", nargs="+", required=True,
        metavar="ORG/REPO",
        help="GitHub repos to replay PRs from (e.g. org/service-a org/service-b)",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Maximum PRs to replay per repo (default: 50)",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=None,
        help="Skip PRs older than N days (default: no limit)",
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Seconds to wait between pipeline runs (default: 2.0)",
    )
    parser.add_argument(
        "--output", default="replay_manifest.json",
        help="Path for the replay manifest JSON (default: replay_manifest.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List PRs without running the pipeline",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
