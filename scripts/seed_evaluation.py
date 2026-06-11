#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/seed_evaluation.py
────────────────────────────
Sets up and runs the Paper 3 calibration evaluation study.

What it does
────────────
1. SEED GRAPH  — registers 4 K11 services and their consumer relationships
   in the dependency graph so the pipeline has the right topology.

2. CREATE PRs  — for each scenario in eval/scenarios.json, creates a
   GitHub branch with a modified openapi.yaml and opens a PR against
   K11-Software-Solutions/k11-user-service.

3. RUN PIPELINE — runs the full pipeline directly against each PR
   (no webhook needed — bypasses the merged-PR requirement).

4. RECORD GROUND TRUTH — writes the known expected verdict into
   calibration.db with gt_source="controlled", so plot_calibration.py
   can generate the paper's calibration curve immediately.

Usage
─────
    # Full run: seed graph + create PRs + run pipeline + record results
    python scripts/seed_evaluation.py

    # Seed dependency graph only
    python scripts/seed_evaluation.py --seed-graph-only

    # Create GitHub PRs only (no pipeline run)
    python scripts/seed_evaluation.py --create-prs-only

    # Run pipeline against existing PRs (skip PR creation)
    python scripts/seed_evaluation.py --run-only

    # Skip PR creation if branches already exist
    python scripts/seed_evaluation.py --skip-existing
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import uuid
import copy
import yaml

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
logger = logging.getLogger("seed_evaluation")

GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
GRAPH_STORE_DB  = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
CALIBRATION_DB  = os.getenv("CALIBRATION_DB", "calibration.db")
USER_SVC_REPO   = "K11-Software-Solutions/k11-user-service"
SCENARIOS_FILE  = os.path.join(_ROOT, "eval", "scenarios.json")

GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ── Dependency graph topology ─────────────────────────────────────────────────
#
#  k11-notification-svc ──→ k11-order-service ──→ k11-user-service
#  k11-payment-service  ──→ k11-order-service ──→ k11-user-service
#
#  When user-service changes:
#    k11-order-service     = direct consumer    (hop_depth=1)
#    k11-payment-service   = transitive depth-2 (via order-service)
#    k11-notification-svc  = transitive depth-2 (via order-service)

SERVICES = [
    {"name": "k11-user-service",    "repo": USER_SVC_REPO,                                   "team": "platform"},
    {"name": "k11-order-service",   "repo": "K11-Software-Solutions/k11-order-service",      "team": "commerce"},
    {"name": "k11-payment-service", "repo": "K11-Software-Solutions/k11-payment-service",    "team": "finance"},
    {"name": "k11-notification-svc","repo": "K11-Software-Solutions/k11-notification-svc",   "team": "platform"},
]

CONSUMPTION_EDGES = [
    # k11-order-service directly consumes user-service
    {
        "consumer": "k11-order-service",
        "provider": "k11-user-service",
        "endpoint_pattern": "/api/v2/users/{id}",
        "methods": ["GET"],
        "criticality": "high",
    },
    # k11-payment-service directly consumes order-service (making it TRANSITIVE for user-service)
    {
        "consumer": "k11-payment-service",
        "provider": "k11-order-service",
        "endpoint_pattern": "/api/v1/orders/{id}",
        "methods": ["GET"],
        "criticality": "critical",
    },
    # k11-notification-svc directly consumes order-service (also TRANSITIVE for user-service)
    {
        "consumer": "k11-notification-svc",
        "provider": "k11-order-service",
        "endpoint_pattern": "/api/v1/orders/{id}",
        "methods": ["GET", "PATCH"],
        "criticality": "medium",
    },
]


# ── Modified OpenAPI specs per scenario ───────────────────────────────────────

def _apply_patch(base_spec: dict, patch: dict) -> dict:
    """Return a deep-copied modified spec based on the patch descriptor."""
    spec = copy.deepcopy(base_spec)

    # Remove email field entirely from User schema
    if "remove_property" in patch:
        prop = patch["remove_property"]
        schemas = spec.get("components", {}).get("schemas", {})
        for schema in schemas.values():
            schema.get("properties", {}).pop(prop, None)
            required = schema.get("required", [])
            if prop in required:
                required.remove(prop)
        spec["info"]["version"] = "1.1.0"

    # Override required list on a schema
    if "components.schemas.User.required" in patch:
        spec["components"]["schemas"]["User"]["required"] = patch["components.schemas.User.required"]

    # Add required header to GET /api/v2/users/{id}
    if "add_required_header" in patch:
        header_name = patch["add_required_header"]
        path = spec["paths"].get("/api/v2/users/{id}", {})
        get_op = path.get("get", {})
        params = get_op.setdefault("parameters", [])
        # Keep existing path param, add header
        params.append({
            "name": header_name,
            "in": "header",
            "required": True,
            "schema": {"type": "string"},
            "description": "Service authentication token",
        })
        spec["info"]["version"] = "1.1.0"

    # Rename all /api/<old>/ paths to /api/<new>/  e.g. {"v2": "v3"}
    if "rename_paths" in patch:
        for old_marker, new_marker in patch["rename_paths"].items():
            new_paths = {}
            for path_key, path_val in spec.get("paths", {}).items():
                new_key = path_key.replace(f"/api/{old_marker}/", f"/api/{new_marker}/")
                new_paths[new_key] = path_val
            spec["paths"] = new_paths
        spec["info"]["version"] = "2.0.0"

    # Add optional property to User schema
    if "add_optional_property" in patch:
        prop = patch["add_optional_property"]
        spec["components"]["schemas"]["User"]["properties"][prop] = {
            "type": "string",
            "nullable": True,
            "description": f"Optional {prop.replace('_', ' ')}",
        }
        spec["info"]["version"] = "1.1.0"

    # Add new search endpoint
    if "add_endpoint" in patch:
        ep = patch["add_endpoint"]
        spec["paths"][ep] = {
            "get": {
                "summary": "Search users by name or email",
                "operationId": "search_users",
                "tags": ["users"],
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "default": 1}},
                ],
                "responses": {
                    "200": {
                        "description": "Matching users",
                        "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/User"}}}},
                    }
                },
            }
        }
        spec["info"]["version"] = "1.1.0"

    # Change enum values on a schema property
    if "change_enum" in patch:
        ce = patch["change_enum"]
        schema_props = spec["components"]["schemas"][ce["schema"]]["properties"]
        if ce["property"] in schema_props:
            schema_props[ce["property"]]["enum"] = ce["to"]
        spec["info"]["version"] = "1.1.0"

    return spec


# ── GitHub API helpers ────────────────────────────────────────────────────────

async def gh_get(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(f"https://api.github.com{path}", headers=GH_HEADERS)
    r.raise_for_status()
    return r.json()


async def gh_post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    r = await client.post(f"https://api.github.com{path}", headers=GH_HEADERS, json=body)
    r.raise_for_status()
    return r.json()


async def gh_put(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    r = await client.put(f"https://api.github.com{path}", headers=GH_HEADERS, json=body)
    r.raise_for_status()
    return r.json()


async def get_main_sha(client: httpx.AsyncClient, repo: str) -> str:
    data = await gh_get(client, f"/repos/{repo}/git/ref/heads/main")
    return data["object"]["sha"]


async def get_file(client: httpx.AsyncClient, repo: str, path: str) -> tuple[str, str]:
    """Returns (content_str, sha)."""
    data = await gh_get(client, f"/repos/{repo}/contents/{path}")
    return base64.b64decode(data["content"]).decode(), data["sha"]


async def create_branch(client: httpx.AsyncClient, repo: str, branch: str, sha: str) -> bool:
    """Create branch. Returns False if already exists."""
    try:
        await gh_post(client, f"/repos/{repo}/git/refs", {
            "ref": f"refs/heads/{branch}", "sha": sha,
        })
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            logger.info("Branch %s already exists", branch)
            return False
        raise


async def update_file(
    client: httpx.AsyncClient, repo: str, path: str,
    content: str, message: str, branch: str, file_sha: str,
) -> None:
    await gh_put(client, f"/repos/{repo}/contents/{path}", {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": file_sha,
        "branch": branch,
    })


async def create_pr(
    client: httpx.AsyncClient, repo: str,
    title: str, head: str, body: str,
) -> dict:
    return await gh_post(client, f"/repos/{repo}/pulls", {
        "title": title, "head": head, "base": "main", "body": body,
    })


async def get_pr_number(client: httpx.AsyncClient, repo: str, head_branch: str) -> int | None:
    """Find an existing open PR for this branch."""
    owner = repo.split("/")[0]
    data = await gh_get(client, f"/repos/{repo}/pulls?state=open&head={owner}:{head_branch}")
    if data:
        return data[0]["number"]
    return None


# ── Step 1: Seed dependency graph ─────────────────────────────────────────────

async def seed_graph() -> None:
    from graph.dependency_graph import DependencyGraph, ServiceNode, ConsumptionEdge
    from graph.graph_store import GraphStore

    logger.info("Seeding dependency graph: %s", GRAPH_STORE_DB)
    async with GraphStore(GRAPH_STORE_DB) as store:
        for svc in SERVICES:
            await store.add_service(ServiceNode(
                name=svc["name"], repo=svc["repo"], team=svc["team"],
            ))
            logger.info("  registered service: %s", svc["name"])

        for edge in CONSUMPTION_EDGES:
            await store.record_consumption(ConsumptionEdge(
                consumer=edge["consumer"],
                provider=edge["provider"],
                endpoint_pattern=edge["endpoint_pattern"],
                methods=edge["methods"],
                criticality=edge["criticality"],
            ))
            logger.info("  edge: %s → %s (%s)",
                        edge["consumer"], edge["provider"], edge["endpoint_pattern"])

    logger.info("Graph seeded: %d services, %d edges", len(SERVICES), len(CONSUMPTION_EDGES))


# ── Step 2: Create GitHub PRs ─────────────────────────────────────────────────

async def create_scenario_prs(scenarios: list[dict], skip_existing: bool) -> dict[str, dict]:
    """
    Create one GitHub branch + PR per scenario.
    Returns {scenario_id: {pr_number, head_sha, base_sha}}.
    """
    pr_info: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch base spec once
        base_yaml_str, file_sha = await get_file(client, USER_SVC_REPO, "openapi.yaml")
        base_spec = yaml.safe_load(base_yaml_str)
        main_sha = await get_main_sha(client, USER_SVC_REPO)
        logger.info("Base spec fetched — main SHA: %s", main_sha[:10])

        for scenario in scenarios:
            sid    = scenario["id"]
            branch = scenario["branch"]
            title  = scenario["title"]

            logger.info("=== Scenario %s ===", sid)

            # Check if PR already exists
            existing_pr = await get_pr_number(client, USER_SVC_REPO, branch)
            if existing_pr and skip_existing:
                logger.info("  PR #%d already exists — skipping", existing_pr)
                # Still need head SHA for pipeline run
                ref_data = await gh_get(client, f"/repos/{USER_SVC_REPO}/git/ref/heads/{branch}")
                pr_info[sid] = {
                    "pr_number": existing_pr,
                    "head_sha":  ref_data["object"]["sha"],
                    "base_sha":  main_sha,
                }
                continue

            # Apply patch to generate modified spec
            modified_spec = _apply_patch(base_spec, scenario["openapi_patch"])
            modified_yaml = yaml.dump(modified_spec, default_flow_style=False, sort_keys=False)

            # Create branch
            created = await create_branch(client, USER_SVC_REPO, branch, main_sha)
            if not created and not skip_existing:
                logger.warning("  Branch exists and skip_existing=False — overwriting file")

            # Get branch-specific file SHA (may differ if branch existed)
            try:
                _, branch_file_sha = await get_file(
                    client, USER_SVC_REPO, f"openapi.yaml?ref={branch}"
                )
            except Exception:
                branch_file_sha = file_sha

            # Push modified spec to branch
            await update_file(
                client, USER_SVC_REPO, "openapi.yaml",
                modified_yaml, f"{title}", branch, branch_file_sha,
            )

            # Get head SHA after push
            ref_data = await gh_get(client, f"/repos/{USER_SVC_REPO}/git/ref/heads/{branch}")
            head_sha = ref_data["object"]["sha"]

            # Create PR
            body = (
                f"**Evaluation scenario:** `{sid}`\n\n"
                f"{scenario['description']}\n\n"
                f"**Expected verdict:** `{scenario['expected_verdict']}`\n"
                f"**Changed endpoints:** {', '.join(f'`{e}`' for e in scenario['changed_endpoints'])}\n\n"
                f"*This PR was created by `scripts/seed_evaluation.py` for the Paper 3 calibration study.*"
            )
            pr_data = await create_pr(client, USER_SVC_REPO, title, branch, body)
            pr_number = pr_data["number"]

            logger.info("  Created PR #%d: %s", pr_number, title)
            pr_info[sid] = {
                "pr_number": pr_number,
                "head_sha":  head_sha,
                "base_sha":  main_sha,
            }

    return pr_info


# ── Step 3: Run pipeline per scenario ────────────────────────────────────────

async def run_scenario(scenario: dict, pr_meta: dict) -> dict | None:
    """Run the full pipeline against one scenario and return final state."""
    from langgraph.checkpoint.memory import MemorySaver
    from pipeline.orchestrator import build_orchestrator
    from pipeline.state import initial_state

    sid        = scenario["id"]
    run_id     = str(uuid.uuid4())
    pr_number  = pr_meta["pr_number"]
    head_sha   = pr_meta["head_sha"]
    base_sha   = pr_meta["base_sha"]

    logger.info("Running pipeline — scenario=%s PR=#%d run_id=%s", sid, pr_number, run_id)

    state = initial_state(
        run_id=run_id,
        pr_number=pr_number,
        repo_name=USER_SVC_REPO,
        pr_diff="",
        base_branch="main",
        triggered_by="seed_evaluation",
        head_sha=head_sha,
        base_sha=base_sha,
    )

    checkpointer = MemorySaver()
    pipeline = build_orchestrator().compile(checkpointer=checkpointer)

    try:
        config = {"configurable": {"thread_id": run_id}}
        final_state = await pipeline.ainvoke(state, config=config)

        verdict  = (final_state.get("summary") or {}).get("overall_verdict", "N/A")
        n_consumers = len(
            final_state.get("adjusted_compliance_results")
            or final_state.get("compliance_results", [])
        )
        uncertainty = final_state.get("uncertainty_score", 0.0)
        logger.info(
            "  verdict=%s  consumers=%d  uncertainty=%.3f",
            verdict, n_consumers, uncertainty,
        )
        return {"run_id": run_id, "final_state": final_state, "scenario": scenario}

    except Exception as exc:
        logger.error("  Pipeline failed for %s: %s", sid, exc)
        return None


# ── Step 4: Record known ground truth ────────────────────────────────────────

async def record_calibration(run_result: dict) -> None:
    from calibration.store import CalibrationStore

    scenario    = run_result["scenario"]
    final_state = run_result["final_state"]
    run_id      = run_result["run_id"]

    results = (
        final_state.get("adjusted_compliance_results")
        or final_state.get("compliance_results", [])
    )
    if not results:
        logger.info("  No compliance results to record for %s", scenario["id"])
        return

    hop_depths = {
        c["consumer"]: c.get("hop_depth", 1)
        for c in final_state.get("downstream_consumers", [])
        if isinstance(c, dict)
    }

    async with CalibrationStore(CALIBRATION_DB) as store:
        await store.record_run(
            run_id=run_id,
            repo_name=USER_SVC_REPO,
            pr_number=scenario.get("pr_number", 0),
            compliance_results=results,
            agent_confidence_scores=final_state.get("agent_confidence_scores", {}),
            hop_depths=hop_depths,
        )

        # Write known ground truth immediately (controlled study — we know the answer)
        expected = scenario["expected_verdict"]
        consumer_verdicts = {r["consumer"]: expected for r in results}
        await store.resolve_run_ground_truth(run_id, consumer_verdicts, gt_source="controlled")

    logger.info(
        "  Calibration recorded: %d rows  gt=%s  source=controlled",
        len(results), expected,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> None:
    with open(SCENARIOS_FILE, encoding="utf-8") as f:
        scenarios = json.load(f)

    # Step 1 — Seed graph
    if not args.run_only and not args.create_prs_only:
        await seed_graph()
    elif args.seed_graph_only:
        await seed_graph()
        return

    if args.seed_graph_only:
        return

    # Step 2 — Create PRs
    pr_info: dict[str, dict] = {}
    if not args.run_only:
        pr_info = await create_scenario_prs(scenarios, skip_existing=args.skip_existing)
        print(f"\nCreated {len(pr_info)} PRs:")
        for sid, meta in pr_info.items():
            print(f"  {sid}: PR #{meta['pr_number']}")

    if args.create_prs_only:
        return

    # For run-only mode, look up existing PRs
    if args.run_only:
        async with httpx.AsyncClient(timeout=30) as client:
            main_sha = await get_main_sha(client, USER_SVC_REPO)
            for scenario in scenarios:
                branch = scenario["branch"]
                pr_number = await get_pr_number(client, USER_SVC_REPO, branch)
                if pr_number:
                    ref_data = await gh_get(client, f"/repos/{USER_SVC_REPO}/git/ref/heads/{branch}")
                    pr_info[scenario["id"]] = {
                        "pr_number": pr_number,
                        "head_sha":  ref_data["object"]["sha"],
                        "base_sha":  main_sha,
                    }
                else:
                    logger.warning("No open PR found for scenario %s branch %s", scenario["id"], branch)

    # Steps 3 + 4 — Run pipeline and record results
    results_summary = []
    for scenario in scenarios:
        sid = scenario["id"]
        if sid not in pr_info:
            logger.warning("Skipping %s — no PR info", sid)
            continue

        meta = pr_info[sid]
        scenario["pr_number"] = meta["pr_number"]
        run_result = await run_scenario(scenario, meta)
        if run_result:
            await record_calibration(run_result)
            results_summary.append({
                "scenario_id": sid,
                "expected": scenario["expected_verdict"],
                "run_id": run_result["run_id"],
                "pr_number": meta["pr_number"],
            })

    # Print summary
    print(f"\n{'='*65}")
    print(f"Evaluation seeding complete")
    print(f"{'='*65}")
    print(f"{'Scenario':<35} {'Expected':^12} {'PR':>6}")
    print("-" * 55)
    for r in results_summary:
        print(f"  {r['scenario_id']:<33} {r['expected']:^12} #{r['pr_number']}")
    print(f"\nCalibration DB: {CALIBRATION_DB}")
    print(f"Rows recorded:  {len(results_summary)} scenarios × consumers")
    print(f"\nNext step — generate calibration plot:")
    print(f"  python scripts/plot_calibration.py --gt-source controlled --output calibration.png")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the Paper 3 evaluation: graph + GitHub PRs + pipeline runs + calibration."
    )
    parser.add_argument("--seed-graph-only",  action="store_true", help="Only seed the dependency graph")
    parser.add_argument("--create-prs-only",  action="store_true", help="Only create GitHub PRs")
    parser.add_argument("--run-only",         action="store_true", help="Only run pipeline (PRs must exist)")
    parser.add_argument("--skip-existing",    action="store_true", help="Skip PR creation if branch exists")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
