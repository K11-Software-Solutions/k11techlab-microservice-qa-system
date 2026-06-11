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
MicroservicePipelineState — shared TypedDict for the cross-repo impact pipeline.
"""
from __future__ import annotations
from typing import TypedDict, Optional, Annotated
import operator


def _merge_confidence_dicts(a: dict, b: dict) -> dict:
    return {**a, **b}


class MicroservicePipelineState(TypedDict):
    # ── Trigger inputs ───────────────────────────────────────────────────────
    run_id:         str
    pr_number:      int
    repo_name:      str       # service that raised the PR
    pr_diff:        str
    base_branch:    str
    head_sha:       Optional[str]   # PR head commit SHA
    base_sha:       Optional[str]   # PR base commit SHA

    # ── Phase 1: Contract extraction ─────────────────────────────────────────
    current_contract:   Optional[dict]   # extracted from PR branch
    previous_contract:  Optional[dict]   # from contract registry
    contract_diff:      Optional[dict]   # structured diff result
    changed_endpoints:  list[dict]       # list of changed endpoint specs
    breaking_changes:   list[dict]       # subset classified as breaking

    # ── Phase 2: Dependency graph ────────────────────────────────────────────
    dependency_graph:       Optional[dict]   # serialised graph snapshot
    downstream_consumers:   list[dict]       # services consuming changed endpoints
    impact_radius:          Optional[int]    # count of affected consumers
    impact_score:           Optional[float]  # weighted severity 0.0–1.0
    affected_services:      list[str]        # service names in impact radius

    # ── HITL gate ────────────────────────────────────────────────────────────
    hitl_required:   bool
    hitl_decision:   Optional[str]
    hitl_reviewer:   Optional[str]
    hitl_comment:    Optional[str]

    # ── Phase 3: Parallel consumer validation (reducers) ────────────────────
    compliance_results: Annotated[list[dict], operator.add]
    violations:         Annotated[list[dict], operator.add]
    errors:             Annotated[list[str],  operator.add]

    # ── Confidence propagation ───────────────────────────────────────────────
    agent_confidence_scores: Annotated[dict[str, float], _merge_confidence_dicts]
    uncertainty_score:   float           # 0.0 = certain, 1.0 = maximally uncertain
    uncertainty_verdict: str             # "LOW" | "MEDIUM" | "HIGH"
    effective_n_agents:  Optional[float] # Kish n_eff — None before first 10 runs (Feature 7)
    uncertainty_classifications: Optional[dict]  # consumer → {type, reason, confidence} (Feature 9)
    adjusted_compliance_results: Optional[list[dict]]  # post-downgrade snapshot; None if no adjustment
    drift_report: Optional[dict]                        # DriftReport.to_dict() for the provider service

    # ── Phase 4: Impact report ───────────────────────────────────────────────
    summary:        Optional[dict]
    final_report:   Optional[str]
    github_issues:  Optional[list]   # cross-repo issues filed
    slack_sent:     Optional[bool]

    # ── Pipeline metadata ────────────────────────────────────────────────────
    pipeline_version: str
    triggered_by:     str
    started_at:       str
    completed_at:     Optional[str]


def initial_state(
    run_id: str,
    pr_number: int,
    repo_name: str,
    pr_diff: str,
    base_branch: str = "main",
    triggered_by: str = "github_webhook",
    head_sha: Optional[str] = None,
    base_sha: Optional[str] = None,
) -> MicroservicePipelineState:
    from datetime import datetime, timezone
    return MicroservicePipelineState(
        run_id=run_id, pr_number=pr_number, repo_name=repo_name,
        pr_diff=pr_diff, base_branch=base_branch,
        head_sha=head_sha, base_sha=base_sha,
        current_contract=None, previous_contract=None,
        contract_diff=None, changed_endpoints=[], breaking_changes=[],
        dependency_graph=None, downstream_consumers=[],
        impact_radius=None, impact_score=None, affected_services=[],
        hitl_required=False, hitl_decision=None,
        hitl_reviewer=None, hitl_comment=None,
        compliance_results=[], violations=[], errors=[],
        agent_confidence_scores={}, uncertainty_score=0.0, uncertainty_verdict="LOW",
        effective_n_agents=None, adjusted_compliance_results=None, drift_report=None,
        uncertainty_classifications=None,
        summary=None, final_report=None,
        github_issues=None, slack_sent=None,
        pipeline_version="1.0.0", triggered_by=triggered_by,
        started_at=datetime.now(timezone.utc).isoformat(),
        completed_at=None,
    )
