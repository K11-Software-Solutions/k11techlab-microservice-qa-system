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
scripts/find_external_prs.py
──────────────────────────────
Discovers external open-source repos with OpenAPI specs and finds historical
PRs that modified those specs. Auto-classifies each PR with the B1 diff engine
and saves a dataset for manual review and Phase B evaluation.

Usage:
    # Discover and classify PRs (writes eval/phase_b_candidates.json)
    python scripts/find_external_prs.py --discover

    # Re-classify using latest diff engine (no GitHub API calls)
    python scripts/find_external_prs.py --reclassify

    # Print summary of candidate dataset
    python scripts/find_external_prs.py --summary

Output: eval/phase_b_candidates.json
  Each entry:
    - repo:        "owner/repo"
    - pr_number:   int
    - title:       PR title
    - merged_at:   ISO datetime
    - head_sha:    commit SHA at PR head
    - base_sha:    base branch commit SHA
    - spec_file:   path within repo (e.g. "openapi.yaml")
    - b1_verdict:  "BREAKING" | "COMPATIBLE" | "UNCERTAIN"
    - b1_changes:  list of change descriptions
    - gt_label:    null (fill in manually) | "BREAKING" | "COMPATIBLE"
    - notes:       str (free text for manual review)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("find_ext_prs")

GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
CANDIDATES_PATH = Path(__file__).parent.parent / "eval" / "phase_b_candidates.json"

# ── Curated target repos ───────────────────────────────────────────────────────
# Selected for: has OpenAPI/Swagger spec, active PR history, diverse API domains.
# spec_files lists paths to check (in order of preference).

TARGET_REPOS = [
    # Git forge APIs
    {"repo": "go-gitea/gitea",           "spec_files": ["templates/swagger/v1_json.tmpl", "routers/api/v1/swagger/options.go"],
     "openapi_path": None,   "domain": "git_forge"},
    # Identity / auth
    {"repo": "ory/hydra",                "spec_files": ["spec/api.json"],
     "openapi_path": "spec/api.json",    "domain": "auth"},
    {"repo": "ory/kratos",               "spec_files": ["spec/api.json"],
     "openapi_path": "spec/api.json",    "domain": "auth"},
    # Observability
    {"repo": "grafana/grafana",          "spec_files": ["public/api-merged.json"],
     "openapi_path": "public/api-merged.json", "domain": "observability"},
    # Container management
    {"repo": "portainer/portainer",      "spec_files": ["api/swagger.yaml", "api/http/handler/swagger.go"],
     "openapi_path": "api/swagger.yaml", "domain": "devops"},
    # Microservice demos (multiple services → real consumer topology)
    {"repo": "open-telemetry/opentelemetry-demo", "spec_files": ["src/productcatalogservice/openapi.yaml",
                                                                    "src/checkoutservice/openapi.yaml",
                                                                    "src/currencyservice/openapi.yaml"],
     "openapi_path": None,               "domain": "demo"},
    # PocketBase (BaaS with REST API)
    {"repo": "pocketbase/pocketbase",    "spec_files": ["apis/swagger.go"],
     "openapi_path": None,               "domain": "baas"},
    # Spring Boot microservice examples
    {"repo": "spring-petclinic/spring-petclinic-microservices",
     "spec_files": ["spring-petclinic-api-gateway/src/main/resources/api-gateway.yaml",
                    "spring-petclinic-vets-service/src/main/resources/vets.yaml"],
     "openapi_path": None,               "domain": "demo"},
    # Cloud-native API gateway
    {"repo": "traefik/traefik",          "spec_files": ["integration/fixtures/api/swagger.json"],
     "openapi_path": None,               "domain": "gateway"},
    # Microservice-aligned payment / banking demo
    {"repo": "apssouza22/java-microservice",
     "spec_files": ["user-service/src/main/resources/openapi.yaml",
                    "payment-service/src/main/resources/openapi.yaml"],
     "openapi_path": None,               "domain": "demo"},
]


# ── GitHub API helpers ─────────────────────────────────────────────────────────

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def gh_get(path: str, client: httpx.AsyncClient, params: dict | None = None) -> dict | list:
    r = await client.get(f"https://api.github.com{path}", headers=HEADERS, params=params or {})
    r.raise_for_status()
    return r.json()


async def find_spec_files(repo: str, candidate_paths: list[str], client: httpx.AsyncClient) -> list[str]:
    """Return which of the candidate_paths actually exist in the repo (via GitHub contents API)."""
    found = []
    for path in candidate_paths:
        try:
            await gh_get(f"/repos/{repo}/contents/{path}", client)
            found.append(path)
        except httpx.HTTPStatusError:
            pass
    return found


async def get_prs_touching_file(
    repo: str,
    file_path: str,
    client: httpx.AsyncClient,
    limit: int = 30,
) -> list[dict]:
    """
    Find merged PRs that modified `file_path` in `repo`.
    Uses GitHub commit search + PR association.
    Returns list of {pr_number, title, merged_at, head_sha, base_sha}.
    """
    results = []
    page = 1
    while len(results) < limit:
        try:
            commits = await gh_get(
                f"/repos/{repo}/commits",
                client,
                params={"path": file_path, "per_page": 30, "page": page},
            )
        except httpx.HTTPStatusError:
            break
        if not isinstance(commits, list) or not commits:
            break

        for commit in commits:
            sha = commit["sha"]
            # Find PRs associated with this commit
            try:
                pr_list = await gh_get(
                    f"/repos/{repo}/commits/{sha}/pulls", client
                )
                if not isinstance(pr_list, list):
                    continue
                for pr in pr_list:
                    if pr.get("state") == "closed" and pr.get("merged_at"):
                        results.append({
                            "pr_number": pr["number"],
                            "title":     pr["title"],
                            "merged_at": pr["merged_at"],
                            "head_sha":  pr["head"]["sha"],
                            "base_sha":  pr["base"]["sha"],
                        })
            except httpx.HTTPStatusError:
                pass

            if len(results) >= limit:
                break

        page += 1
        if len(commits) < 30:
            break

    # Deduplicate by PR number
    seen = set()
    unique = []
    for r in results:
        if r["pr_number"] not in seen:
            seen.add(r["pr_number"])
            unique.append(r)
    return unique[:limit]


async def fetch_spec_at_sha(
    repo: str,
    spec_path: str,
    sha: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Fetch raw content of a file at a specific commit SHA."""
    try:
        data = await gh_get(
            f"/repos/{repo}/contents/{spec_path}",
            client,
            params={"ref": sha},
        )
        if isinstance(data, dict) and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


# ── B1 diff classification ─────────────────────────────────────────────────────

def b1_classify(
    repo: str,
    spec_path: str,
    head_raw: str | None,
    base_raw: str | None,
) -> tuple[str, list[str]]:
    """Run the diff engine and return (verdict, list_of_change_descriptions)."""
    from analyzer.contract_extractor import extract_contract
    from contracts.diff import diff_contracts

    if not head_raw or not base_raw:
        return "UNCERTAIN", ["Could not fetch spec at one or both SHAs"]

    svc = repo.split("/")[-1]
    try:
        head = extract_contract(spec_path, head_raw, svc, repo, "head")
        base = extract_contract(spec_path, base_raw, svc, repo, "base")
        if head is None or base is None:
            return "UNCERTAIN", ["Unsupported spec format"]
        diff = diff_contracts(base, head)
        if diff is None:
            return "UNCERTAIN", ["Service name mismatch in diff"]
        changes = [c.description for c in diff.changes]
        verdict = "BREAKING" if diff.breaking else "COMPATIBLE"
        return verdict, changes
    except Exception as exc:
        return "UNCERTAIN", [f"Diff error: {exc}"]


# ── Discovery loop ─────────────────────────────────────────────────────────────

async def discover(max_prs_per_repo: int = 15) -> None:
    """Search all target repos for PRs that modified their OpenAPI spec."""
    CANDIDATES_PATH.parent.mkdir(exist_ok=True)
    existing = {}
    if CANDIDATES_PATH.exists():
        for entry in json.loads(CANDIDATES_PATH.read_text()):
            key = f"{entry['repo']}#{entry['pr_number']}#{entry['spec_file']}"
            existing[key] = entry

    candidates: list[dict] = list(existing.values())
    new_count = 0

    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        for target in TARGET_REPOS:
            repo = target["repo"]
            candidate_paths = target["spec_files"]
            domain = target["domain"]

            log.info("Processing %s (%s)...", repo, domain)

            # Find which spec files exist
            if target.get("openapi_path"):
                spec_files = [target["openapi_path"]]
            else:
                spec_files = await find_spec_files(repo, candidate_paths, client)

            if not spec_files:
                log.warning("  No spec files found in %s — skipping", repo)
                continue
            log.info("  Found spec files: %s", spec_files)

            for spec_file in spec_files[:2]:   # at most 2 spec files per repo
                prs = await get_prs_touching_file(repo, spec_file, client, limit=max_prs_per_repo)
                log.info("  %s → %d PRs touching %s", repo, len(prs), spec_file)

                for pr in prs:
                    key = f"{repo}#{pr['pr_number']}#{spec_file}"
                    if key in existing:
                        continue   # already classified

                    # Fetch both versions of the spec
                    head_raw, base_raw = await asyncio.gather(
                        fetch_spec_at_sha(repo, spec_file, pr["head_sha"], client),
                        fetch_spec_at_sha(repo, spec_file, pr["base_sha"], client),
                    )
                    verdict, changes = b1_classify(repo, spec_file, head_raw, base_raw)

                    entry = {
                        "repo":        repo,
                        "domain":      domain,
                        "pr_number":   pr["pr_number"],
                        "title":       pr["title"],
                        "merged_at":   pr["merged_at"],
                        "head_sha":    pr["head_sha"],
                        "base_sha":    pr["base_sha"],
                        "spec_file":   spec_file,
                        "b1_verdict":  verdict,
                        "b1_changes":  changes[:10],   # cap for readability
                        "gt_label":    None,           # fill in manually
                        "notes":       "",
                    }
                    candidates.append(entry)
                    existing[key] = entry
                    new_count += 1
                    log.info("    PR #%d (%s) → B1=%s (%d changes)",
                             pr["pr_number"], pr["title"][:60], verdict, len(changes))

    CANDIDATES_PATH.write_text(json.dumps(candidates, indent=2))
    breaking = sum(1 for c in candidates if c["b1_verdict"] == "BREAKING")
    compatible = sum(1 for c in candidates if c["b1_verdict"] == "COMPATIBLE")
    log.info("Done. %d total candidates (%d new) — %d BREAKING, %d COMPATIBLE, %d UNCERTAIN",
             len(candidates), new_count, breaking, compatible,
             len(candidates) - breaking - compatible)
    log.info("Saved to %s", CANDIDATES_PATH)
    log.info("")
    log.info("Next: manually fill in 'gt_label' for a sample of candidates,")
    log.info("then run: python scripts/run_phase_b_eval.py")


async def reclassify() -> None:
    """Re-run B1 on all candidates using the latest diff engine."""
    if not CANDIDATES_PATH.exists():
        sys.exit("No candidates file found — run --discover first")
    candidates = json.loads(CANDIDATES_PATH.read_text())
    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        for entry in candidates:
            head_raw = await fetch_spec_at_sha(
                entry["repo"], entry["spec_file"], entry["head_sha"], client)
            base_raw = await fetch_spec_at_sha(
                entry["repo"], entry["spec_file"], entry["base_sha"], client)
            verdict, changes = b1_classify(
                entry["repo"], entry["spec_file"], head_raw, base_raw)
            entry["b1_verdict"] = verdict
            entry["b1_changes"] = changes[:10]
    CANDIDATES_PATH.write_text(json.dumps(candidates, indent=2))
    log.info("Reclassified %d candidates", len(candidates))


def summary() -> None:
    if not CANDIDATES_PATH.exists():
        sys.exit("No candidates file found — run --discover first")
    candidates = json.loads(CANDIDATES_PATH.read_text())

    total = len(candidates)
    breaking = sum(1 for c in candidates if c["b1_verdict"] == "BREAKING")
    compatible = sum(1 for c in candidates if c["b1_verdict"] == "COMPATIBLE")
    uncertain = total - breaking - compatible
    labelled = sum(1 for c in candidates if c.get("gt_label"))

    print(f"\nPhase B Candidate Dataset — {CANDIDATES_PATH}")
    print(f"  Total PRs:        {total}")
    print(f"  B1 BREAKING:      {breaking}  ({100*breaking//total if total else 0}%)")
    print(f"  B1 COMPATIBLE:    {compatible}  ({100*compatible//total if total else 0}%)")
    print(f"  B1 UNCERTAIN:     {uncertain}  ({100*uncertain//total if total else 0}%)")
    print(f"  Manually labelled:{labelled} / {total}")
    print()

    by_repo: dict[str, dict] = {}
    for c in candidates:
        r = c["repo"]
        if r not in by_repo:
            by_repo[r] = {"total": 0, "breaking": 0, "compatible": 0}
        by_repo[r]["total"] += 1
        if c["b1_verdict"] == "BREAKING":
            by_repo[r]["breaking"] += 1
        elif c["b1_verdict"] == "COMPATIBLE":
            by_repo[r]["compatible"] += 1

    print(f"  {'Repo':<45} {'PRs':>5} {'BREAK':>7} {'COMPAT':>8}")
    print("  " + "-" * 68)
    for repo, d in sorted(by_repo.items()):
        print(f"  {repo:<45} {d['total']:>5} {d['breaking']:>7} {d['compatible']:>8}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover",    action="store_true",
                    help="Search GitHub for PRs with OpenAPI changes")
    ap.add_argument("--reclassify",  action="store_true",
                    help="Re-run B1 diff on existing candidates")
    ap.add_argument("--summary",     action="store_true",
                    help="Print dataset summary")
    ap.add_argument("--max-prs",     type=int, default=15,
                    help="Max PRs to collect per repo (default 15)")
    args = ap.parse_args()

    if args.summary:
        summary()
    elif args.reclassify:
        asyncio.run(reclassify())
    else:
        if not GITHUB_TOKEN:
            sys.exit("GITHUB_TOKEN not set — check .env")
        asyncio.run(discover(args.max_prs))


if __name__ == "__main__":
    main()
