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
scripts/run_transitive_eval.py
───────────────────────────────
Transitive consumer eval: 8 scenarios against k11-order-service as provider.
Exercises Feature 5 (depth-2 consumer detection via k11-analytics-service)
and DEPTH_DECAY=0.70 (confidence decay for hop_depth>=2).

Topology exercised:
  k11-payment-service   → k11-order-service   /api/v1/orders/{id}          GET  critical depth=1
  k11-notification-svc  → k11-order-service   /api/v1/orders/{id}          GET  medium   depth=1
  k11-analytics-service → k11-notification-svc /api/v1/notifications/{id}  GET  low      depth=2

Usage:
    DEPTH_DECAY=0.70 PYTHONIOENCODING=utf-8 python -m dotenv run -- \\
        python scripts/run_transitive_eval.py

Prerequisites:
    - Webhook server running:  uvicorn api.webhook:app --port 9001
    - GITHUB_TOKEN and ANTHROPIC_API_KEY set in .env
    - k11-analytics-service repo exists at K11-Software-Solutions/k11-analytics-service
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("transitive_eval")

ORG            = "K11-Software-Solutions"
WEBHOOK_URL    = "http://localhost:9001/webhook/github"
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "k11tech-webhook-secret-2026")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
DEPTH_DECAY    = float(os.getenv("DEPTH_DECAY", "1.0"))
RESULTS_PATH   = Path(__file__).parent.parent / "eval" / "results_transitive.json"

# ── Order service baseline spec ────────────────────────────────────────────────

_ORDER_BASE_PATHS = {
    "/api/v1/orders": {
        "get": {
            "summary": "List all orders", "operationId": "list_orders",
            "parameters": [{"name": "page", "in": "query", "required": False,
                            "schema": {"type": "integer", "default": 1}}],
            "responses": {"200": {"description": "List of orders",
                                  "content": {"application/json": {
                                      "schema": {"type": "array",
                                                 "items": {"$ref": "#/components/schemas/Order"}}}}}},
        },
        "post": {
            "summary": "Create a new order", "operationId": "create_order",
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/CreateOrder"}}}},
            "responses": {"201": {"description": "Order created",
                                  "content": {"application/json": {
                                      "schema": {"$ref": "#/components/schemas/Order"}}}},
                          "422": {"description": "User not found"}},
        },
    },
    "/api/v1/orders/{id}": {
        "get": {
            "summary": "Get an order by ID", "operationId": "get_order",
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Order found",
                                  "content": {"application/json": {
                                      "schema": {"$ref": "#/components/schemas/Order"}}}},
                          "404": {"description": "Order not found"}},
        },
        "patch": {
            "summary": "Update order status", "operationId": "update_order_status",
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "status", "in": "query", "required": True,
                 "schema": {"type": "string",
                            "enum": ["pending", "confirmed", "shipped", "delivered", "cancelled"]}},
            ],
            "responses": {"200": {"description": "Order updated",
                                  "content": {"application/json": {
                                      "schema": {"$ref": "#/components/schemas/Order"}}}},
                          "404": {"description": "Order not found"}},
        },
    },
}

_ORDER_BASE_SCHEMAS = {
    "Order": {
        "type": "object",
        "required": ["id", "user_id", "items", "total", "status"],
        "properties": {
            "id":      {"type": "string"},
            "user_id": {"type": "string"},
            "items":   {"type": "array", "items": {"type": "string"}},
            "total":   {"type": "number", "format": "float"},
            "status":  {"type": "string",
                        "enum": ["pending", "confirmed", "shipped", "delivered", "cancelled"]},
        },
    },
    "CreateOrder": {
        "type": "object",
        "required": ["user_id", "items", "total"],
        "properties": {
            "user_id": {"type": "string"},
            "items":   {"type": "array", "items": {"type": "string"}},
            "total":   {"type": "number", "format": "float"},
        },
    },
}


def _order_yaml(paths: dict, version: str, schemas: dict | None = None) -> str:
    import yaml
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "Order Service", "version": version},
        "paths": paths,
        "components": {"schemas": schemas or _ORDER_BASE_SCHEMAS},
    }
    return yaml.dump(doc, sort_keys=False, allow_unicode=True)


# ── Scenario definitions ───────────────────────────────────────────────────────

@dataclass
class Scenario:
    id:                 str
    branch:             str
    title:              str
    body:               str
    ground_truth:       str
    openapi_content:    str
    notes:              str = ""
    expected_consumers: list[str] = field(default_factory=list)


def _build_scenarios() -> list[Scenario]:
    import yaml, copy

    # T-01: Remove GET /api/v1/orders/{id} — BREAKING (all GET consumers at depth 1 & 2)
    p01 = copy.deepcopy(_ORDER_BASE_PATHS)
    del p01["/api/v1/orders/{id}"]["get"]
    if not p01["/api/v1/orders/{id}"]:
        del p01["/api/v1/orders/{id}"]

    t01 = Scenario(
        id="T-01", branch="eval/t01-remove-get-order-by-id",
        title="eval(T-01): Remove GET /api/v1/orders/{id}",
        body="Removes the order lookup endpoint. All consumers using GET /api/v1/orders/{id} will break.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service", "k11-notification-svc", "k11-analytics-service"],
        openapi_content=_order_yaml(p01, "2.0.0"),
        notes="Endpoint removal — all GET consumers depth-1 and depth-2 affected",
    )

    # T-02: Add required currency field to POST /api/v1/orders — BREAKING (payment-service only)
    t02_schemas = copy.deepcopy(_ORDER_BASE_SCHEMAS)
    t02_schemas["CreateOrder"]["required"] = ["user_id", "items", "total", "currency"]
    t02_schemas["CreateOrder"]["properties"]["currency"] = {
        "type": "string", "description": "ISO 4217 code, e.g. USD"}

    t02 = Scenario(
        id="T-02", branch="eval/t02-required-currency-post",
        title="eval(T-02): Add required 'currency' to POST /api/v1/orders",
        body="currency is now required when creating orders. GET-only consumers are unaffected.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service"],
        openapi_content=_order_yaml(copy.deepcopy(_ORDER_BASE_PATHS), "1.1.0", t02_schemas),
        notes="POST-only change — notification-svc and analytics-service excluded by method filter",
    )

    # T-03: Rename response field total → amount — BREAKING (GET consumers)
    t03_schemas = copy.deepcopy(_ORDER_BASE_SCHEMAS)
    t03_schemas["Order"]["required"] = ["id", "user_id", "items", "amount", "status"]
    props = t03_schemas["Order"]["properties"]
    props["amount"] = props.pop("total")
    props["amount"]["description"] = "Previously 'total'"

    t03 = Scenario(
        id="T-03", branch="eval/t03-rename-total-to-amount",
        title="eval(T-03): Rename Order response field 'total' to 'amount'",
        body="Renames the price field. Consumers reading 'total' will get null/undefined.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service", "k11-notification-svc", "k11-analytics-service"],
        openapi_content=_order_yaml(copy.deepcopy(_ORDER_BASE_PATHS), "2.0.0", t03_schemas),
        notes="Response field rename — silently breaks GET consumers at depth 1 and 2",
    )

    # T-04: Change GET /api/v1/orders/{id} response 200 → 202 — BREAKING
    p04 = copy.deepcopy(_ORDER_BASE_PATHS)
    p04["/api/v1/orders/{id}"]["get"]["responses"] = {
        "202": {"description": "Order found (async)"},
        "404": {"description": "Order not found"},
    }
    t04 = Scenario(
        id="T-04", branch="eval/t04-response-code-200-to-202",
        title="eval(T-04): Change GET /api/v1/orders/{id} response 200 → 202",
        body="Success code changed. Consumers checking for HTTP 200 will treat the response as an error.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service", "k11-notification-svc", "k11-analytics-service"],
        openapi_content=_order_yaml(p04, "1.1.0"),
        notes="Response code change — breaking for strict status-code checkers",
    )

    # T-05: Remove POST /api/v1/orders — BREAKING (payment-service only, method filter protects others)
    p05 = copy.deepcopy(_ORDER_BASE_PATHS)
    del p05["/api/v1/orders"]["post"]

    t05 = Scenario(
        id="T-05", branch="eval/t05-remove-post-orders",
        title="eval(T-05): Remove POST /api/v1/orders (order creation endpoint)",
        body="Order creation removed. GET-only consumers (notification, analytics) are unaffected.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service"],
        openapi_content=_order_yaml(p05, "2.0.0"),
        notes="POST endpoint removal — method filter excludes GET-only consumers",
    )

    # T-06: Add optional notes field to Order response — COMPATIBLE
    t06_schemas = copy.deepcopy(_ORDER_BASE_SCHEMAS)
    t06_schemas["Order"]["properties"]["notes"] = {
        "type": "string", "nullable": True,
        "description": "Optional delivery notes"}

    t06 = Scenario(
        id="T-06", branch="eval/t06-add-optional-notes",
        title="eval(T-06): Add optional 'notes' field to Order response",
        body="Purely additive — new optional field. No existing consumer parsing is affected.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content=_order_yaml(copy.deepcopy(_ORDER_BASE_PATHS), "1.1.0", t06_schemas),
        notes="Additive response field — all consumers should pass as COMPATIBLE",
    )

    # T-07: Add new GET /api/v1/orders/summary endpoint — COMPATIBLE
    p07 = copy.deepcopy(_ORDER_BASE_PATHS)
    p07["/api/v1/orders/summary"] = {
        "get": {
            "summary": "Order summary stats", "operationId": "order_summary",
            "responses": {"200": {"description": "Summary"}},
        }
    }
    t07 = Scenario(
        id="T-07", branch="eval/t07-add-summary-endpoint",
        title="eval(T-07): Add new GET /api/v1/orders/summary endpoint",
        body="New endpoint added. No existing endpoints changed. No consumer edge matches /summary.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content=_order_yaml(p07, "1.1.0"),
        notes="Additive endpoint — path matching excludes /summary from /orders/{id} consumers",
    )

    # T-08: Add optional discount_code to CreateOrder — COMPATIBLE
    t08_schemas = copy.deepcopy(_ORDER_BASE_SCHEMAS)
    t08_schemas["CreateOrder"]["properties"]["discount_code"] = {
        "type": "string", "nullable": True,
        "description": "Optional discount code"}

    t08 = Scenario(
        id="T-08", branch="eval/t08-add-optional-discount-code",
        title="eval(T-08): Add optional 'discount_code' to CreateOrder request body",
        body="Optional field added to POST body. Existing callers not sending discount_code are unaffected.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content=_order_yaml(copy.deepcopy(_ORDER_BASE_PATHS), "1.1.0", t08_schemas),
        notes="Optional POST field addition — COMPATIBLE for existing callers",
    )

    return [t01, t02, t03, t04, t05, t06, t07, t08]


# ── GitHub helpers (copied from run_phase_a_eval.py) ───────────────────────────

async def gh_get(path: str, client: httpx.AsyncClient) -> dict:
    r = await client.get(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json"},
    )
    r.raise_for_status()
    return r.json()


async def gh_post(path: str, data: dict, client: httpx.AsyncClient) -> dict:
    r = await client.post(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        content=json.dumps(data).encode(),
    )
    r.raise_for_status()
    return r.json()


async def gh_put(path: str, data: dict, client: httpx.AsyncClient) -> dict:
    r = await client.put(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        content=json.dumps(data).encode(),
    )
    r.raise_for_status()
    return r.json()


async def get_main_sha(repo: str, client: httpx.AsyncClient) -> tuple[str, str]:
    ref_data  = await gh_get(f"/repos/{ORG}/{repo}/git/ref/heads/main", client)
    file_data = await gh_get(f"/repos/{ORG}/{repo}/contents/openapi.yaml?ref=main", client)
    return ref_data["object"]["sha"], file_data["sha"]


async def create_branch(repo: str, branch: str, tip_sha: str, client: httpx.AsyncClient) -> None:
    try:
        await gh_post(f"/repos/{ORG}/{repo}/git/refs",
                      {"ref": f"refs/heads/{branch}", "sha": tip_sha}, client)
    except Exception:
        pass  # branch already exists


async def get_branch_file_sha(repo: str, branch: str, client: httpx.AsyncClient) -> str | None:
    try:
        r = await client.get(
            f"https://api.github.com/repos/{ORG}/{repo}/contents/openapi.yaml",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            params={"ref": branch},
        )
        r.raise_for_status()
        return r.json()["sha"]
    except Exception:
        return None


async def update_file(repo: str, branch: str, content: str, file_sha: str,
                      message: str, client: httpx.AsyncClient) -> str:
    data = await gh_put(f"/repos/{ORG}/{repo}/contents/openapi.yaml", {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": file_sha,
        "branch": branch,
    }, client)
    return data["commit"]["sha"]


async def create_pr(repo: str, branch: str, title: str, body: str,
                    client: httpx.AsyncClient) -> tuple[int, str, str]:
    try:
        data = await gh_post(f"/repos/{ORG}/{repo}/pulls", {
            "title": title, "body": body,
            "head": branch, "base": "main",
        }, client)
        return data["number"], data["head"]["sha"], data["base"]["sha"]
    except Exception:
        prs = await gh_get(f"/repos/{ORG}/{repo}/pulls?head={ORG}:{branch}&state=open", client)
        if prs:
            return prs[0]["number"], prs[0]["head"]["sha"], prs[0]["base"]["sha"]
        raise


def trigger_webhook(repo: str, pr_number: int, head_sha: str, base_sha: str,
                    base_ref: str) -> str:
    payload = json.dumps({
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "title": "eval PR",
            "body": "",
            "head": {"sha": head_sha, "ref": "eval-branch"},
            "base": {"sha": base_sha, "ref": base_ref},
        },
        "repository": {"full_name": f"{ORG}/{repo}"},
    }, separators=(',', ':')).encode('utf-8')

    sig = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()

    req = urllib.request.Request(
        WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "X-GitHub-Event": "pull_request",
                 "X-Hub-Signature-256": sig},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["run_id"]


def poll_run(run_id: str, timeout: int = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        with urllib.request.urlopen(
            f"http://localhost:9001/runs/{run_id}", timeout=10
        ) as resp:
            run = json.loads(resp.read())
        if run["status"] != "running":
            return run
    return {"status": "timeout", "run_id": run_id, "summary": None}


# ── Topology setup ─────────────────────────────────────────────────────────────

async def setup_topology() -> None:
    """Register analytics-service node and both transitive edges in the graph store."""
    from graph.dependency_graph import ConsumptionEdge, ServiceNode
    from graph.graph_store import GraphStore
    db = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
    async with GraphStore(db) as store:
        await store.add_service(ServiceNode(
            name="k11-analytics-service",
            repo="K11-Software-Solutions/k11-analytics-service",
            team="analytics",
            slack_channel="#team-analytics",
        ))
        # payment-service also creates orders via POST (order creation edge)
        await store.record_consumption(ConsumptionEdge(
            consumer="k11-payment-service",
            provider="k11-order-service",
            endpoint_pattern="/api/v1/orders",
            methods=["POST"],
            criticality="critical",
        ))
        # depth-1: notification-svc is a direct consumer of order-service
        await store.record_consumption(ConsumptionEdge(
            consumer="k11-notification-svc",
            provider="k11-order-service",
            endpoint_pattern="/api/v1/orders/{id}",
            methods=["GET"],
            criticality="medium",
        ))
        # depth-2 path: analytics → notification-svc → order-service
        await store.record_consumption(ConsumptionEdge(
            consumer="k11-analytics-service",
            provider="k11-notification-svc",
            endpoint_pattern="/api/v1/notifications/{id}",
            methods=["GET"],
            criticality="low",
        ))
    log.info(
        "Topology: payment(d1 GET+POST) + notification(d1) + analytics(d2) → order-service  "
        "[DEPTH_DECAY=%.2f]", DEPTH_DECAY
    )


# ── Single scenario runner ─────────────────────────────────────────────────────

async def run_scenario(scenario: Scenario, client: httpx.AsyncClient) -> dict:
    log.info("=" * 60)
    log.info("Running %s — %s", scenario.id, scenario.title)

    repo = "k11-order-service"

    tip_sha, main_file_sha = await get_main_sha(repo, client)
    await create_branch(repo, scenario.branch, tip_sha, client)

    branch_file_sha = await get_branch_file_sha(repo, scenario.branch, client)
    file_sha = branch_file_sha if branch_file_sha else main_file_sha

    commit_sha = await update_file(
        repo, scenario.branch, scenario.openapi_content, file_sha,
        f"{scenario.id}: {scenario.title}", client,
    )
    log.info("%s — pushed %s (head=%s)", scenario.id, scenario.branch, commit_sha[:8])

    pr_number, _pr_head_sha, base_sha = await create_pr(
        repo, scenario.branch, scenario.title, scenario.body, client
    )
    head_sha = commit_sha
    log.info("%s — PR #%d (head=%s base=%s)", scenario.id, pr_number, head_sha[:8], base_sha[:8])

    t_start = time.time()
    run_id = trigger_webhook(repo, pr_number, head_sha, base_sha, "main")
    result = poll_run(run_id)
    elapsed = round(time.time() - t_start, 1)

    summary = result.get("summary") or {}
    if result["status"] == "timeout":
        verdict = "TIMEOUT"
    elif result["status"] == "completed" and not summary:
        verdict = "COMPATIBLE"
    else:
        verdict = summary.get("overall_verdict", "ERROR")

    breaking_consumers  = summary.get("breaking_consumers", 0)
    uncertain_consumers = summary.get("uncertain_consumers", 0)
    compatible_consumers = summary.get("compatible_consumers", 0)
    impact_score        = summary.get("impact_score", 0.0)
    impact_radius       = summary.get("impact_radius", 0)
    hitl                = summary.get("hitl_required", False) if summary else False
    correct             = verdict == scenario.ground_truth

    # Extract per-consumer details if the pipeline returns them
    compliance_results = summary.get("compliance_results", [])
    consumer_detail = []
    for cr in compliance_results:
        consumer_detail.append({
            "consumer":          cr.get("consumer", ""),
            "verdict":           cr.get("verdict", ""),
            "confidence":        cr.get("confidence", None),
            "confidence_decayed": cr.get("confidence_decayed", None),
            "hop_depth":         cr.get("hop_depth", None),
        })

    log.info(
        "%s — verdict=%s gt=%s correct=%s  breaking=%d uncertain=%d compatible=%d  "
        "score=%.3f radius=%d hitl=%s  time=%.1fs",
        scenario.id, verdict, scenario.ground_truth, correct,
        breaking_consumers, uncertain_consumers, compatible_consumers,
        impact_score, impact_radius, hitl, elapsed,
    )
    if consumer_detail:
        for cd in consumer_detail:
            decayed = f" (decayed={cd['confidence_decayed']:.3f})" if cd.get('confidence_decayed') else ""
            log.info("  consumer=%s verdict=%s conf=%s%s depth=%s",
                     cd['consumer'], cd['verdict'], cd.get('confidence'), decayed, cd.get('hop_depth'))

    return {
        "scenario_id":         scenario.id,
        "ground_truth":        scenario.ground_truth,
        "expected_consumers":  scenario.expected_consumers,
        "notes":               scenario.notes,
        "verdict":             verdict,
        "correct":             correct,
        "breaking_consumers":  breaking_consumers,
        "uncertain_consumers": uncertain_consumers,
        "compatible_consumers": compatible_consumers,
        "impact_score":        impact_score,
        "impact_radius":       impact_radius,
        "hitl_triggered":      hitl,
        "elapsed_s":           elapsed,
        "run_id":              run_id,
        "consumer_detail":     consumer_detail,
        "depth_decay":         DEPTH_DECAY,
    }


# ── Metrics ────────────────────────────────────────────────────────────────────

def compute_metrics(results: list[dict]) -> dict:
    pairs = [(r["verdict"], r["ground_truth"]) for r in results]
    tp = sum(1 for v, gt in pairs if v == "BREAKING"   and gt == "BREAKING")
    fp = sum(1 for v, gt in pairs if v == "BREAKING"   and gt == "COMPATIBLE")
    tn = sum(1 for v, gt in pairs if v == "COMPATIBLE" and gt == "COMPATIBLE")
    fn = sum(1 for v, gt in pairs if v == "COMPATIBLE" and gt == "BREAKING")
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0
    rec  = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
    hitl = sum(1 for r in results if r.get("hitl_triggered"))

    # Depth-2 consumer detection: count scenarios where analytics-service appeared
    analytics_detected = sum(
        1 for r in results
        if any(cd.get("consumer") == "k11-analytics-service"
               for cd in r.get("consumer_detail", []))
    )

    # Average confidence decay delta (only where analytics appears)
    decay_deltas = []
    for r in results:
        for cd in r.get("consumer_detail", []):
            if (cd.get("consumer") == "k11-analytics-service"
                    and cd.get("confidence") is not None
                    and cd.get("confidence_decayed") is not None):
                decay_deltas.append(cd["confidence_decayed"] - cd["confidence"])

    return {
        "total_scenarios":      len(results),
        "correct":              sum(1 for r in results if r["correct"]),
        "accuracy":             round(sum(1 for r in results if r["correct"]) / len(results), 3) if results else 0,
        "precision":            round(prec, 3),
        "recall":               round(rec, 3),
        "f1":                   round(f1, 3),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "hitl_triggered":       hitl,
        "depth_decay":          DEPTH_DECAY,
        "analytics_detected_in": analytics_detected,
        "mean_decay_delta":     round(sum(decay_deltas) / len(decay_deltas), 4) if decay_deltas else None,
        "latency_mean_s":       round(sum(r["elapsed_s"] for r in results) / len(results), 1) if results else 0,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not GITHUB_TOKEN:
        sys.exit("GITHUB_TOKEN not set — check .env")

    log.info("DEPTH_DECAY=%.2f", DEPTH_DECAY)
    log.info("Setting up topology (depth-2 analytics edge)...")
    await setup_topology()

    scenarios = _build_scenarios()
    results   = []
    RESULTS_PATH.parent.mkdir(exist_ok=True)

    async with httpx.AsyncClient(timeout=30) as client:
        for scenario in scenarios:
            try:
                result = await run_scenario(scenario, client)
                results.append(result)
                RESULTS_PATH.write_text(json.dumps({"results": results}, indent=2))
                log.info("Saved %d/%d", len(results), len(scenarios))
            except Exception as exc:
                log.error("Scenario %s failed: %s", scenario.id, exc, exc_info=True)
                results.append({
                    "scenario_id": scenario.id,
                    "ground_truth": scenario.ground_truth,
                    "verdict": "ERROR", "correct": False, "error": str(exc),
                })

    metrics = compute_metrics(results)
    RESULTS_PATH.write_text(json.dumps({"results": results, "metrics": metrics}, indent=2))

    log.info("\n%s", "=" * 60)
    log.info("TRANSITIVE EVAL COMPLETE  (DEPTH_DECAY=%.2f)", DEPTH_DECAY)
    log.info("=" * 60)
    log.info("Scenarios: %d  Correct: %d  Acc=%.1f%%",
             metrics["total_scenarios"], metrics["correct"], metrics["accuracy"] * 100)
    log.info("P=%.1f%%  R=%.1f%%  F1=%.1f%%",
             metrics["precision"] * 100, metrics["recall"] * 100, metrics["f1"] * 100)
    log.info("HITL triggered: %d", metrics["hitl_triggered"])
    log.info("analytics-service detected in: %d scenarios", metrics["analytics_detected_in"])
    if metrics["mean_decay_delta"] is not None:
        log.info("Mean DEPTH_DECAY confidence delta: %.4f", metrics["mean_decay_delta"])
    log.info("Results → %s", RESULTS_PATH)


if __name__ == "__main__":
    asyncio.run(main())
