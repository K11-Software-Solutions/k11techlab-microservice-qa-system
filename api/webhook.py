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
api/webhook.py
───────────────
FastAPI webhook server for the K11tech Microservice QA Pipeline.

Endpoints:
  POST /webhook/github        — Receives GitHub PR events
  POST /hitl/approve/{run_id} — Approves a paused cross-repo HITL pipeline
  POST /hitl/reject/{run_id}  — Rejects a paused cross-repo HITL pipeline
  GET  /runs/{run_id}         — Returns run status and final report
  POST /services/register     — Registers a service in the contract registry
  POST /graph/consumption     — Records a service consumption edge

The webhook dispatches to both:
  - Paper 1 pipeline (single-repo QA)  at PAPER1_PIPELINE_URL
  - Paper 3 pipeline (cross-repo impact) — this pipeline
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from typing import Optional

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from graph.dependency_graph import ConsumptionEdge, ServiceNode
from graph.graph_store import GraphStore
from pipeline.orchestrator import microservice_builder
from pipeline.state import initial_state

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GRAPH_STORE_DB        = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
PAPER1_PIPELINE_URL   = os.getenv("PAPER1_PIPELINE_URL", "")

# ── In-memory run store (replace with Redis/DB in production) ─────────────────
_runs: dict[str, dict] = {}

# ── LangGraph checkpointer for HITL support ───────────────────────────────────
_checkpointer = MemorySaver()
_pipeline = microservice_builder.compile(checkpointer=_checkpointer)

app = FastAPI(
    title="K11tech Microservice QA — Cross-Repo Impact Pipeline",
    version="1.0.0",
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class HITLDecision(BaseModel):
    decision: str   # "approve" | "reject"
    reviewer: str
    comment:  str = ""


class ServiceRegistration(BaseModel):
    name:          str
    repo:          str
    contract_path: str = ""
    team:          str = ""
    slack_channel: str = ""


class ConsumptionRecord(BaseModel):
    consumer:         str
    provider:         str
    endpoint_pattern: str
    methods:          list[str] = []
    criticality:      str = "medium"


# ── GitHub Webhook ────────────────────────────────────────────────────────────

@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event:     str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    body = await request.body()

    # Signature validation
    if GITHUB_WEBHOOK_SECRET:
        expected = "sha256=" + hmac.new(
            GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid signature")

    if x_github_event not in ("pull_request",):
        return {"status": "ignored", "event": x_github_event}

    import json
    payload = json.loads(body)
    action  = payload.get("action", "")

    if action not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "action": action}

    pr      = payload["pull_request"]
    run_id  = str(uuid.uuid4())
    repo    = payload["repository"]["full_name"]

    state = initial_state(
        run_id=run_id,
        pr_number=pr["number"],
        repo_name=repo,
        pr_diff="",   # fetched by ContractExtractorAgent via GitHub REST API
        base_branch=pr["base"]["ref"],
        triggered_by="github_webhook",
        head_sha=pr["head"]["sha"],
        base_sha=pr["base"]["sha"],
    )

    _runs[run_id] = {"status": "running", "state": state}
    background_tasks.add_task(_run_pipeline, run_id, state)

    # Also dispatch to Paper 1 pipeline if configured
    if PAPER1_PIPELINE_URL:
        background_tasks.add_task(_dispatch_paper1, payload)

    logger.info("Dispatched run %s for PR #%d in %s", run_id, pr["number"], repo)
    return {"run_id": run_id, "status": "dispatched", "pr": pr["number"], "repo": repo}


async def _run_pipeline(run_id: str, state: dict) -> None:
    """Background task: run the full pipeline and store results."""
    try:
        config = {"configurable": {"thread_id": run_id}}
        final_state = await _pipeline.ainvoke(state, config=config)
        _runs[run_id]["status"] = "completed"
        _runs[run_id]["state"]  = final_state
        logger.info("Run %s completed — verdict=%s",
                    run_id, (final_state.get("summary") or {}).get("overall_verdict", "N/A"))
    except Exception as exc:
        logger.exception("Run %s failed: %s", run_id, exc)
        _runs[run_id]["status"] = "failed"
        _runs[run_id]["error"]  = str(exc)


async def _dispatch_paper1(payload: dict) -> None:
    """Fire-and-forget dispatch to Paper 1 pipeline."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(PAPER1_PIPELINE_URL + "/webhook/github",
                              json=payload)
    except Exception as exc:
        logger.warning("Paper 1 dispatch failed: %s", exc)


# ── HITL endpoints ────────────────────────────────────────────────────────────

@app.post("/hitl/approve/{run_id}")
async def hitl_approve(run_id: str, body: HITLDecision):
    """Resume a paused pipeline with an APPROVE decision."""
    return await _resume_hitl(run_id, "approve", body.reviewer, body.comment)


@app.post("/hitl/reject/{run_id}")
async def hitl_reject(run_id: str, body: HITLDecision):
    """Resume a paused pipeline with a REJECT decision."""
    return await _resume_hitl(run_id, "reject", body.reviewer, body.comment)


async def _resume_hitl(run_id: str, decision: str, reviewer: str, comment: str) -> dict:
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    config = {"configurable": {"thread_id": run_id}}
    await _pipeline.aupdate_state(
        config,
        {
            "hitl_decision": decision,
            "hitl_reviewer": reviewer,
            "hitl_comment":  comment,
        },
        as_node="human_review_gate",
    )
    # Resume execution
    state = _runs[run_id]["state"]
    import asyncio
    asyncio.create_task(_run_pipeline(run_id, state))
    return {"run_id": run_id, "decision": decision, "status": "resumed"}


# ── Run status ────────────────────────────────────────────────────────────────

@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    run = _runs[run_id]
    return {
        "run_id":             run_id,
        "status":             run["status"],
        "summary":            run["state"].get("summary"),
        "final_report":       run["state"].get("final_report"),
        "uncertainty_score":  run["state"].get("uncertainty_score"),
        "uncertainty_verdict": run["state"].get("uncertainty_verdict"),
        "error":              run.get("error"),
    }


# ── Service & graph management ────────────────────────────────────────────────

@app.post("/services/register")
async def register_service(body: ServiceRegistration):
    """Register a service and add it to the dependency graph."""
    async with GraphStore(GRAPH_STORE_DB) as store:
        await store.add_service(ServiceNode(
            name=body.name,
            repo=body.repo,
            team=body.team,
            slack_channel=body.slack_channel,
        ))
    return {"status": "registered", "service": body.name}


@app.post("/graph/consumption")
async def record_consumption(body: ConsumptionRecord):
    """Record a service-to-service API consumption edge."""
    async with GraphStore(GRAPH_STORE_DB) as store:
        await store.record_consumption(ConsumptionEdge(
            consumer=body.consumer,
            provider=body.provider,
            endpoint_pattern=body.endpoint_pattern,
            methods=body.methods,
            criticality=body.criticality,
        ))
    return {"status": "recorded", "consumer": body.consumer, "provider": body.provider}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "k11techlab-microservice-qa-pipeline"}
