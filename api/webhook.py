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

from calibration.store import CALIBRATION_DB, CalibrationStore
from graph.dependency_graph import ConsumptionEdge, ServiceNode
from graph.graph_store import GraphStore
from pipeline.orchestrator import microservice_builder
from pipeline.state import initial_state

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GITHUB_WEBHOOK_SECRET      = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GRAPH_STORE_DB             = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
PAPER1_PIPELINE_URL        = os.getenv("PAPER1_PIPELINE_URL", "")
CALIBRATION_ENABLED        = os.getenv("CALIBRATION_ENABLED", "true").lower() != "false"

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
        if CALIBRATION_ENABLED:
            import asyncio
            asyncio.create_task(_record_calibration(run_id, final_state))
    except Exception as exc:
        logger.exception("Run %s failed: %s", run_id, exc)
        _runs[run_id]["status"] = "failed"
        _runs[run_id]["error"]  = str(exc)


async def _record_calibration(run_id: str, final_state: dict) -> None:
    """Fire-and-forget: persist per-consumer confidence+verdict for calibration study."""
    try:
        results = (
            final_state.get("adjusted_compliance_results")
            or final_state.get("compliance_results", [])
        )
        if not results:
            return
        # Build hop_depth lookup from downstream_consumers state
        hop_depths = {
            c["consumer"]: c.get("hop_depth", 1)
            for c in final_state.get("downstream_consumers", [])
            if isinstance(c, dict)
        }
        async with CalibrationStore(CALIBRATION_DB) as store:
            await store.record_run(
                run_id=run_id,
                repo_name=final_state.get("repo_name", ""),
                pr_number=final_state.get("pr_number", 0),
                compliance_results=results,
                agent_confidence_scores=final_state.get("agent_confidence_scores", {}),
                hop_depths=hop_depths,
            )
    except Exception as exc:
        logger.warning("Calibration recording failed for run %s: %s", run_id, exc)


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
    adjusted = run["state"].get("adjusted_compliance_results")
    return {
        "run_id":              run_id,
        "status":              run["status"],
        "summary":             run["state"].get("summary"),
        "final_report":        run["state"].get("final_report"),
        "uncertainty_score":   run["state"].get("uncertainty_score"),
        "uncertainty_verdict":  run["state"].get("uncertainty_verdict"),
        "effective_n_agents":  run["state"].get("effective_n_agents"),
        "verdicts_downgraded": (
            sum(1 for r in adjusted if "[Downgraded COMPATIBLE" in r.get("reasoning", ""))
            if adjusted else 0
        ),
        "drift_report":        run["state"].get("drift_report"),
        "error":               run.get("error"),
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


# ── Feature 7: Agent correlation matrix ──────────────────────────────────────

@app.get("/calibration/agent-correlations")
async def agent_correlations():
    """
    Return the current inter-agent confidence correlation matrix estimated from
    historical calibration_log entries.

    Requires at least CORR_MIN_RUNS (default 10) runs with resolved ground truth
    to produce a result.  Before that threshold the system uses uniform weighting
    (independence assumption) and this endpoint returns status='insufficient_data'.

    Response fields:
      status             — 'ok' | 'insufficient_data'
      n_agents           — number of agents in the matrix
      n_runs             — number of runs used to estimate the matrix
      effective_n        — Kish effective sample size (1 ≤ n_eff ≤ n_agents)
      agents             — ordered list of agent names
      correlation_matrix — N×N Pearson correlation values
      precision_weights  — minimum-variance combination weights per agent
    """
    from calibration.correlation import AgentCorrelationMatrix, MIN_RUNS
    async with CalibrationStore(CALIBRATION_DB) as store:
        matrix = await AgentCorrelationMatrix.from_store(store)
    if matrix is None:
        return {
            "status":    "insufficient_data",
            "min_runs":  MIN_RUNS,
            "message":   (
                f"Need at least {MIN_RUNS} complete runs with resolved ground truth. "
                "Run  POST /calibration/resolve  or await CI ground-truth resolution."
            ),
        }
    return {"status": "ok", **matrix.to_dict()}


# ── Feature 8: Conformal HITL threshold ──────────────────────────────────────

@app.get("/calibration/conformal-threshold")
async def conformal_threshold(alpha: float = 0.10):
    """
    Return the split-conformal HITL uncertainty threshold and its coverage guarantee.

    The threshold τ is derived from the calibration set so that at most
    alpha + 1/(n_wrong+1) fraction of incorrect-verdict runs escape the HITL gate
    (marginal, finite-sample guarantee; Angelopoulos & Bates 2023).

    Query params:
      alpha  — target FNR level (default 0.10). Typical range 0.05–0.20.

    Response fields:
      status             — 'ok' | 'insufficient_data'
      alpha              — requested FNR target
      threshold          — τ (uncertainty_score >= τ triggers HITL)
      n_calibration      — total resolved runs in calibration set
      n_wrong            — runs with at least one incorrect verdict (used for fitting)
      fnr_upper_bound    — guaranteed worst-case FNR = alpha + 1/(n_wrong+1)
      coverage_guarantee — 1 - fnr_upper_bound (fraction of wrong runs caught)
      static_threshold   — current hand-tuned UNCERTAINTY_THRESHOLD for comparison
    """
    from calibration.conformal import ConformalHITLThreshold, MIN_WRONG_SAMPLES
    from pipeline.confidence import UNCERTAINTY_THRESHOLD

    if not (0.0 < alpha < 1.0):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="alpha must be in (0, 1)")

    async with CalibrationStore(CALIBRATION_DB) as store:
        run_data = await store.get_run_calibration_data()

    from pipeline.confidence import aggregate_agent_confidence
    pairs = [
        (aggregate_agent_confidence(run["agent_scores"]).uncertainty_score, run["any_wrong"])
        for run in run_data
    ]

    n_wrong = sum(1 for _, is_wrong in pairs if is_wrong)
    if n_wrong < MIN_WRONG_SAMPLES:
        return {
            "status": "insufficient_data",
            "min_wrong": MIN_WRONG_SAMPLES,
            "n_wrong": n_wrong,
            "message": (
                f"Need at least {MIN_WRONG_SAMPLES} runs with incorrect verdicts. "
                f"Currently have {n_wrong}. Pipeline uses static threshold "
                f"{UNCERTAINTY_THRESHOLD} until threshold activates."
            ),
            "static_threshold": UNCERTAINTY_THRESHOLD,
        }

    ct = ConformalHITLThreshold.fit(pairs, alpha=alpha)
    return {
        "status": "ok",
        "static_threshold": UNCERTAINTY_THRESHOLD,
        **ct.to_dict(),
    }


# ── Calibration endpoints ─────────────────────────────────────────────────────

class ManualGroundTruth(BaseModel):
    run_id:            str
    consumer_verdicts: dict[str, str]   # {"k11-payment-service": "BREAKING", ...}


@app.get("/calibration/stats")
async def calibration_stats():
    """Return summary counts from the calibration log."""
    async with CalibrationStore(CALIBRATION_DB) as store:
        return await store.stats()


@app.post("/calibration/resolve")
async def calibration_resolve(body: ManualGroundTruth):
    """Manually supply ground-truth labels for a run (gt_source=manual)."""
    from calibration.ground_truth import resolve_manual
    async with CalibrationStore(CALIBRATION_DB) as store:
        await resolve_manual(store, body.run_id, body.consumer_verdicts)
    return {"run_id": body.run_id, "resolved": len(body.consumer_verdicts)}


@app.post("/calibration/fetch-ci-ground-truth")
async def calibration_fetch_ci(background_tasks: BackgroundTasks):
    """
    Trigger background CI-failure ground-truth resolution for all pending runs.
    Returns immediately; resolution happens asynchronously.
    """
    from calibration.ground_truth import fetch_ci_ground_truth
    async def _run():
        async with CalibrationStore(CALIBRATION_DB) as store:
            counts = await fetch_ci_ground_truth(store)
            logger.info("CI ground-truth fetch complete: %s", counts)
    background_tasks.add_task(_run)
    return {"status": "started"}


# ── Feature 6: Adaptive Confidence Recalibration ──────────────────────────────

class RecalibrateRequest(BaseModel):
    model_type: str = "isotonic"   # 'isotonic' | 'platt'
    gt_source:  str | None = None  # optional filter (e.g. 'controlled', 'manual')


@app.post("/calibration/recalibrate")
async def calibration_recalibrate(body: RecalibrateRequest = RecalibrateRequest()):
    """
    Fit per-agent isotonic regression (or Platt scaling) models from all
    accumulated labelled data (resolved ground truth + HITL reviewer decisions).

    Returns before/after ECE per agent. Models are persisted in calibration.db
    and loaded at inference time via GET /calibration/calibrated-confidence.

    This is the Feature 6 online learning endpoint: call it after collecting
    a batch of HITL decisions to update agent confidence calibration.
    """
    from calibration.recalibration import RecalibrationEngine
    engine = RecalibrationEngine(model_type=body.model_type)
    async with CalibrationStore(CALIBRATION_DB) as store:
        results = await engine.fit_all(store)
    return {
        "model_type": body.model_type,
        "agents": [
            {
                "agent":      r.agent,
                "status":     r.status,
                "n_samples":  r.n_samples,
                "before_ece": round(r.before_ece, 4) if r.before_ece is not None else None,
                "after_ece":  round(r.after_ece,  4) if r.after_ece  is not None else None,
                "message":    r.message,
            }
            for r in results
        ],
    }


@app.get("/calibration/calibrated-confidence")
async def calibration_transform(agent: str, raw: float, model_type: str = "isotonic"):
    """
    Apply the stored recalibration model for `agent` to `raw` confidence.
    Returns the calibrated confidence value.

    Example:
        GET /calibration/calibrated-confidence?agent=contract_compliance_agent:k11-payment-service&raw=0.92
        → {"agent": "...", "raw": 0.92, "calibrated": 0.71, "model_type": "isotonic"}
    """
    from calibration.recalibration import RecalibrationEngine
    engine = RecalibrationEngine(model_type=model_type)
    async with CalibrationStore(CALIBRATION_DB) as store:
        loaded = await engine.load_all(store)
    calibrated = engine.transform(agent, raw)
    return {
        "agent":      agent,
        "raw":        raw,
        "calibrated": round(calibrated, 4),
        "model_type": model_type,
        "model_loaded": engine.has_model(agent),
    }


@app.get("/calibration/models")
async def calibration_models():
    """List all stored recalibration models with before/after ECE."""
    async with CalibrationStore(CALIBRATION_DB) as store:
        models = await store.list_models()
    return {"models": models}
