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
analyzer/impact_scorer.py
──────────────────────────
Scores the impact radius of a contract diff across the dependency graph.

Impact score [0.0, 1.0]:
  Combines consumer count, edge criticality, and breaking change severity.
  Used to decide whether HITL escalation is needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from contracts.models import BreakingChangeType, ContractDiff
from graph.dependency_graph import DependencyGraph

logger = logging.getLogger(__name__)

# Weights for breaking change severity
_SEVERITY_WEIGHTS = {
    "critical": 1.0,
    "high":     0.75,
    "medium":   0.50,
    "low":      0.25,
}

# Weights for edge criticality labels
_CRITICALITY_WEIGHTS = {
    "critical": 1.0,
    "high":     0.75,
    "medium":   0.50,
    "low":      0.25,
}

# Thresholds
IMPACT_HITL_THRESHOLD:          float = 0.60
BREAKING_CONSUMER_HITL_COUNT:   int   = 2


@dataclass
class ImpactAssessment:
    """Full impact assessment for a contract diff + dependency graph."""
    provider:           str
    impact_radius:      int                     # total affected consumers
    impact_score:       float                   # weighted severity [0, 1]
    affected_services:  list[str]               = field(default_factory=list)
    critical_consumers: list[str]               = field(default_factory=list)
    breaking_change_count: int                  = 0
    hitl_required:      bool                    = False
    hitl_reason:        str                     = ""

    def to_dict(self) -> dict:
        return {
            "provider":             self.provider,
            "impact_radius":        self.impact_radius,
            "impact_score":         round(self.impact_score, 4),
            "affected_services":    self.affected_services,
            "critical_consumers":   self.critical_consumers,
            "breaking_change_count": self.breaking_change_count,
            "hitl_required":        self.hitl_required,
            "hitl_reason":          self.hitl_reason,
        }


def score_impact(
    diff: ContractDiff,
    graph: DependencyGraph,
    provider: str,
) -> ImpactAssessment:
    """
    Compute a full ImpactAssessment for a contract diff.

    Args:
        diff:     The ContractDiff from change_detector.diff_endpoints()
        graph:    The live DependencyGraph
        provider: The service that owns the changed contract
    """
    breaking_changes  = diff.breaking
    changed_endpoints = [c.endpoint for c in diff.changes]

    # Compute graph-based impact score
    raw_score        = graph.impact_score(provider, changed_endpoints)
    affected         = graph.downstream_consumers(provider)
    impact_radius    = len(affected)

    # Boost score based on breaking change severity
    if breaking_changes:
        max_severity = max(
            _SEVERITY_WEIGHTS.get(c.severity, 0.5)
            for c in breaking_changes
        )
        # Blend: 70% graph score + 30% worst-case severity
        impact_score = min(1.0, raw_score * 0.7 + max_severity * 0.3)
    else:
        impact_score = raw_score * 0.5  # non-breaking changes get reduced weight

    # Identify critical consumers (high/critical edges touching changed endpoints)
    critical_consumers: list[str] = []
    for consumer in affected:
        edges = graph._g.edges(consumer, data=True)
        for _, target, data in edges:
            if target == provider:
                ep = data.get("endpoint_pattern", "")
                touches = any(ep in changed or changed in ep for changed in changed_endpoints)
                if touches and data.get("criticality") in ("critical", "high"):
                    critical_consumers.append(consumer)
                    break

    # HITL decision
    hitl_required = False
    hitl_reason   = ""
    if impact_score >= IMPACT_HITL_THRESHOLD:
        hitl_required = True
        hitl_reason   = (
            f"Impact score {impact_score:.2f} >= threshold {IMPACT_HITL_THRESHOLD}"
        )
    elif len(breaking_changes) > 0 and impact_radius >= BREAKING_CONSUMER_HITL_COUNT:
        hitl_required = True
        hitl_reason   = (
            f"{len(breaking_changes)} breaking change(s) affect "
            f"{impact_radius} consumers (threshold: {BREAKING_CONSUMER_HITL_COUNT})"
        )

    logger.info(
        "Impact assessment — provider=%s radius=%d score=%.2f hitl=%s",
        provider, impact_radius, impact_score, hitl_required,
    )

    return ImpactAssessment(
        provider=provider,
        impact_radius=impact_radius,
        impact_score=impact_score,
        affected_services=affected,
        critical_consumers=critical_consumers,
        breaking_change_count=len(breaking_changes),
        hitl_required=hitl_required,
        hitl_reason=hitl_reason,
    )
