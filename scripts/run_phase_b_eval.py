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
scripts/run_phase_b_eval.py
────────────────────────────
Phase B: evaluates the pipeline against manually-labelled external-repo PRs.

Prerequisites:
  1. Run find_external_prs.py --discover  to populate eval/phase_b_candidates.json
  2. Manually fill in "gt_label" for at least 20 candidates
  3. A running pipeline server:  uvicorn api.webhook:app --port 9001

Usage:
    # Evaluate against all labelled candidates
    python scripts/run_phase_b_eval.py

    # Dry-run: diff only (B1 vs ground truth), no server required
    python scripts/run_phase_b_eval.py --dry-run

    # Limit to a specific domain or repo
    python scripts/run_phase_b_eval.py --domain auth
    python scripts/run_phase_b_eval.py --repo ory/hydra

    # Save results to a custom path
    python scripts/run_phase_b_eval.py --out eval/results_phase_b_custom.json

Output: eval/results_phase_b.json (or --out path)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phase_b_eval")

GITHUB_TOKEN     = os.getenv("GITHUB_TOKEN", "")
WEBHOOK_SECRET   = os.getenv("GITHUB_WEBHOOK_SECRET", "k11tech-webhook-secret-2026")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "http://localhost:9001/webhook/github")
CANDIDATES_PATH  = Path(__file__).parent.parent / "eval" / "phase_b_candidates.json"
DEFAULT_OUT_PATH = Path(__file__).parent.parent / "eval" / "results_phase_b.json"

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ── Webhook delivery ───────────────────────────────────────────────────────────

def _hmac_sig(body: bytes, secret: str) -> str:
    import hashlib
    import hmac
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send_webhook(
    candidate: dict,
    client: httpx.AsyncClient,
    poll_interval: float = 3.0,
    poll_timeout: float = 120.0,
) -> dict:
    """
    Simulate a GitHub pull_request webhook for this candidate.
    Dispatches to /webhook/github, then polls /runs/{run_id} until done.
    Returns the final run dict, or an error dict.
    """
    repo = candidate["repo"]
    org, repo_name = repo.split("/", 1)

    payload = {
        "action": "opened",   # webhook only accepts opened/synchronize/reopened
        "pull_request": {
            "number": candidate["pr_number"],
            "title":  candidate["title"],
            "merged": True,
            "merged_at": candidate["merged_at"],
            "head": {"sha": candidate["head_sha"], "ref": "phase-b-eval",
                     "repo": {"full_name": repo}},
            "base": {"sha": candidate["base_sha"], "ref": "main",
                     "repo": {"full_name": repo}},
            "html_url": f"https://github.com/{repo}/pull/{candidate['pr_number']}",
        },
        "repository": {
            "full_name": repo,
            "name": repo_name,
            "owner": {"login": org},
        },
    }

    body = json.dumps(payload).encode()
    sig  = _hmac_sig(body, WEBHOOK_SECRET)

    try:
        resp = await client.post(
            WEBHOOK_URL,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": sig,
            },
            timeout=30,
        )
        resp.raise_for_status()
        dispatch = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}

    run_id = dispatch.get("run_id")
    if not run_id:
        return {"error": f"No run_id in dispatch response: {dispatch}"}

    # Poll until status is completed/failed
    deadline = time.time() + poll_timeout
    base_url = WEBHOOK_URL.rsplit("/webhook", 1)[0]
    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            r = await client.get(f"{base_url}/runs/{run_id}", timeout=15)
            r.raise_for_status()
            run = r.json()
            if run.get("status") in ("completed", "failed"):
                return run
        except Exception:
            pass   # keep polling on transient errors

    return {"error": f"Timeout waiting for run {run_id} after {poll_timeout}s"}


# ── B1 diff classification (same as find_external_prs.py) ─────────────────────

async def fetch_spec_at_sha(
    repo: str, spec_path: str, sha: str, client: httpx.AsyncClient
) -> str | None:
    try:
        r = await client.get(
            f"https://api.github.com/repos/{repo}/contents/{spec_path}",
            headers=GITHUB_HEADERS,
            params={"ref": sha},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def b1_classify(repo: str, spec_path: str, head_raw: str | None, base_raw: str | None) -> tuple[str, list[str]]:
    from analyzer.contract_extractor import extract_contract
    from contracts.diff import diff_contracts

    if not head_raw or not base_raw:
        return "UNCERTAIN", ["Could not fetch spec"]
    svc = repo.split("/")[-1]
    try:
        head = extract_contract(spec_path, head_raw, svc, repo, "head")
        base = extract_contract(spec_path, base_raw, svc, repo, "base")
        if not head or not base:
            return "UNCERTAIN", ["Unsupported spec format"]
        diff = diff_contracts(base, head)
        if diff is None:
            return "UNCERTAIN", ["Service name mismatch"]
        return ("BREAKING" if diff.breaking else "COMPATIBLE"), [c.description for c in diff.changes]
    except Exception as exc:
        return "UNCERTAIN", [f"Diff error: {exc}"]


# ── Result extraction ─────────────────────────────────────────────────────────

def extract_verdict(resp: dict) -> str:
    """Pull the final pipeline verdict from the /runs/{run_id} response."""
    if resp.get("error"):          # only truthy errors, not null
        return "ERROR"
    # /runs/{run_id} shape: {status, summary: {overall_verdict, ...}, ...}
    summary = resp.get("summary") or {}
    verdict = summary.get("overall_verdict", "")
    if verdict in ("BREAKING", "COMPATIBLE", "UNCERTAIN"):
        return verdict
    # Pipeline completed but no summary → no changes detected / no consumers → COMPATIBLE
    if resp.get("status") == "completed":
        return "COMPATIBLE"
    return "UNKNOWN"


# ── Metrics calculation ────────────────────────────────────────────────────────

def compute_metrics(results: list[dict], pipeline_key: str = "b3_verdict") -> dict:
    labelled = [r for r in results if r.get("gt_label") and r[pipeline_key] != "ERROR"]
    tp = fp = tn = fn = uncertain = 0
    for r in labelled:
        gt  = r["gt_label"]
        pred = r[pipeline_key]
        if pred == "UNCERTAIN":
            uncertain += 1
            continue
        if gt == "BREAKING" and pred == "BREAKING":
            tp += 1
        elif gt == "COMPATIBLE" and pred == "BREAKING":
            fp += 1
        elif gt == "COMPATIBLE" and pred == "COMPATIBLE":
            tn += 1
        elif gt == "BREAKING" and pred == "COMPATIBLE":
            fn += 1

    decidable = tp + fp + tn + fn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall    = tp / (tp + fn) if (tp + fn) else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    accuracy  = (tp + tn) / decidable if decidable else 0.0

    return {
        "total": len(results),
        "labelled": len(labelled),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn, "uncertain": uncertain,
        "precision": round(precision, 4),
        "recall":    round(recall, 4),
        "f1":        round(f1, 4),
        "accuracy":  round(accuracy, 4),
    }


# ── Main eval loop ─────────────────────────────────────────────────────────────

async def run_eval(
    candidates: list[dict],
    dry_run: bool,
    out_path: Path,
) -> None:
    results: list[dict] = []
    errors = 0

    async with httpx.AsyncClient(timeout=30, headers=GITHUB_HEADERS) as gh_client:
        async with httpx.AsyncClient(timeout=120) as wh_client:
            for i, cand in enumerate(candidates, 1):
                log.info(
                    "[%d/%d] %s PR#%d (%s) gt=%s",
                    i, len(candidates),
                    cand["repo"], cand["pr_number"],
                    cand["title"][:60],
                    cand.get("gt_label") or "unlabelled",
                )

                # ── B1 diff (always computed, no server needed) ────────────
                head_raw, base_raw = await asyncio.gather(
                    fetch_spec_at_sha(cand["repo"], cand["spec_file"], cand["head_sha"], gh_client),
                    fetch_spec_at_sha(cand["repo"], cand["spec_file"], cand["base_sha"], gh_client),
                )
                b1_verdict, b1_changes = b1_classify(
                    cand["repo"], cand["spec_file"], head_raw, base_raw)

                result: dict[str, Any] = {
                    "repo":       cand["repo"],
                    "domain":     cand.get("domain", ""),
                    "pr_number":  cand["pr_number"],
                    "title":      cand["title"],
                    "spec_file":  cand["spec_file"],
                    "gt_label":   cand.get("gt_label"),
                    "b1_verdict": b1_verdict,
                    "b1_changes": b1_changes[:5],
                    "b3_verdict": "SKIPPED",
                    "b3_summary": {},
                    "latency_s":  0.0,
                }

                if not dry_run and cand.get("gt_label"):
                    t0 = time.time()
                    resp = await send_webhook(cand, wh_client)
                    elapsed = round(time.time() - t0, 2)
                    verdict = extract_verdict(resp)
                    result["b3_verdict"]  = verdict
                    result["b3_summary"]  = resp.get("summary") or {}
                    result["latency_s"]   = elapsed
                    result["raw_response"] = {k: v for k, v in resp.items() if k != "summary"}
                    if verdict in ("ERROR", "UNKNOWN"):
                        errors += 1
                        log.warning("    → Error/Unknown verdict: %s", resp.get("error") or resp)
                    else:
                        log.info("    → B1=%s  B3=%s  (%.1fs)", b1_verdict, verdict, elapsed)

                results.append(result)

    # ── Compute metrics ────────────────────────────────────────────────────────
    labelled = [r for r in results if r.get("gt_label")]
    b1_metrics = compute_metrics(results, "b1_verdict")
    b3_metrics = compute_metrics(results, "b3_verdict") if not dry_run else {}

    # Per-domain breakdown
    domains: dict[str, list] = {}
    for r in labelled:
        d = r.get("domain", "unknown")
        domains.setdefault(d, []).append(r)
    domain_metrics = {d: compute_metrics(rows, "b3_verdict" if not dry_run else "b1_verdict")
                      for d, rows in domains.items()}

    output = {
        "phase": "B",
        "dry_run": dry_run,
        "total_candidates": len(results),
        "labelled": len(labelled),
        "errors": errors,
        "metrics": {
            "b1_diff_only": b1_metrics,
            **({"b3_full_pipeline": b3_metrics} if not dry_run else {}),
        },
        "domain_metrics": domain_metrics,
        "results": results,
    }

    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    log.info("Results saved to %s", out_path)

    # Print summary
    print()
    print("=" * 70)
    print(f"Phase B Eval — {'DRY RUN (B1 only)' if dry_run else 'FULL PIPELINE (B3)'}")
    print(f"  Total candidates: {len(results)}  |  Labelled: {len(labelled)}  |  Errors: {errors}")
    print()
    for name, m in output["metrics"].items():
        print(f"  {name.upper()}")
        print(f"    P={m['precision']*100:.1f}%  R={m['recall']*100:.1f}%  F1={m['f1']*100:.1f}%  Acc={m['accuracy']*100:.1f}%")
        print(f"    TP={m['tp']}  FP={m['fp']}  TN={m['tn']}  FN={m['fn']}  Unc={m['uncertain']}")
    if domain_metrics:
        print()
        key = "b3_verdict" if not dry_run else "b1_verdict"
        print(f"  DOMAIN BREAKDOWN (by {key})")
        for dom, m in sorted(domain_metrics.items()):
            print(f"    {dom:<20} P={m['precision']*100:.0f}%  R={m['recall']*100:.0f}%  F1={m['f1']*100:.0f}%  n={m['labelled']}")
    print("=" * 70)


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase B eval runner")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Skip webhook calls; compute B1 metrics only")
    ap.add_argument("--domain",   default="",
                    help="Filter to a specific domain (e.g. auth, demo)")
    ap.add_argument("--repo",     default="",
                    help="Filter to a specific repo (e.g. ory/hydra)")
    ap.add_argument("--out",      default="",
                    help="Output path (default: eval/results_phase_b.json)")
    ap.add_argument("--limit",    type=int, default=0,
                    help="Max candidates to evaluate (0 = all)")
    args = ap.parse_args()

    if not CANDIDATES_PATH.exists():
        sys.exit(f"Candidates file not found: {CANDIDATES_PATH}\nRun: python scripts/find_external_prs.py --discover")

    candidates = json.loads(CANDIDATES_PATH.read_text())

    # Filter
    if args.domain:
        candidates = [c for c in candidates if c.get("domain") == args.domain]
    if args.repo:
        candidates = [c for c in candidates if c["repo"] == args.repo]
    if not args.dry_run:
        # Only run labelled candidates through the pipeline (saves API/compute costs)
        labelled = [c for c in candidates if c.get("gt_label")]
        unlabelled = len(candidates) - len(labelled)
        if unlabelled:
            log.info("Skipping %d unlabelled candidates (no gt_label)", unlabelled)
        candidates = labelled

    if args.limit:
        candidates = candidates[:args.limit]

    if not candidates:
        sys.exit("No candidates match the filter criteria")

    out_path = Path(args.out) if args.out else DEFAULT_OUT_PATH
    log.info("Evaluating %d candidates → %s", len(candidates), out_path)

    asyncio.run(run_eval(candidates, args.dry_run, out_path))


if __name__ == "__main__":
    main()
