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
pipeline/confidence.py
───────────────────────
Confidence aggregation helpers for Phase 2/3 agents.

uncertainty_score  [0.0, 1.0]:  0 = maximally certain, 1 = maximally uncertain
uncertainty_verdict: "LOW" | "MEDIUM" | "HIGH"

HITL triggers when uncertainty_score >= UNCERTAINTY_THRESHOLD (default 0.35),
even if risk/impact scores are below their own thresholds — the
"I passed but I'm not sure" signal.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass


UNCERTAINTY_THRESHOLD = float(os.getenv("UNCERTAINTY_THRESHOLD", "0.35"))
# Weights: (agent_uncertainty, optional_metric_1, optional_metric_2)
_DEFAULT_WEIGHTS = tuple(
    float(w)
    for w in os.getenv("CONFIDENCE_WEIGHTS", "0.5,0.25,0.25").split(",")
)


@dataclass
class ConfidenceSummary:
    mean: float
    minimum: float
    variance: float
    uncertainty_score: float   # 1 - mean, adjusted for variance
    verdict: str               # LOW / MEDIUM / HIGH


def aggregate_agent_confidence(scores: dict[str, float]) -> ConfidenceSummary:
    """
    Given {agent_name: confidence_score}, compute aggregate uncertainty metrics.

    uncertainty_score = (1 - mean) * 0.7 + variance * 0.3
    This rises when mean confidence is low OR individual agents disagree.
    """
    vals = list(scores.values())
    if not vals:
        return ConfidenceSummary(
            mean=0.0, minimum=0.0, variance=0.0,
            uncertainty_score=1.0, verdict="HIGH",
        )

    mean     = statistics.mean(vals)
    minimum  = min(vals)
    variance = statistics.variance(vals) if len(vals) > 1 else 0.0

    uncertainty_score = round((1 - mean) * 0.7 + variance * 0.3, 4)
    uncertainty_score = min(1.0, max(0.0, uncertainty_score))

    if uncertainty_score < 0.20:
        verdict = "LOW"
    elif uncertainty_score < UNCERTAINTY_THRESHOLD:
        verdict = "MEDIUM"
    else:
        verdict = "HIGH"

    return ConfidenceSummary(
        mean=round(mean, 4),
        minimum=round(minimum, 4),
        variance=round(variance, 4),
        uncertainty_score=uncertainty_score,
        verdict=verdict,
    )


CONFIDENCE_DOWNGRADE_THRESHOLD = float(os.getenv("CONFIDENCE_DOWNGRADE_THRESHOLD", "0.50"))


def downgrade_compatible_verdicts(
    compliance_results: list[dict],
    agent_confidence_scores: dict[str, float],
    uncertainty_verdict: str,
) -> tuple[list[dict], int]:
    """
    When uncertainty_verdict is HIGH, downgrade each COMPATIBLE verdict whose
    per-agent confidence score is below CONFIDENCE_DOWNGRADE_THRESHOLD to UNCERTAIN.

    This prevents a low-confidence "all clear" from silently bypassing the HITL gate.
    Only fires when the overall pipeline uncertainty is HIGH — does not touch verdicts
    when the system is reasonably confident.

    Returns (adjusted_results, downgrade_count).
    """
    if uncertainty_verdict != "HIGH":
        return compliance_results, 0

    adjusted = []
    downgrade_count = 0
    for r in compliance_results:
        if r.get("verdict") == "COMPATIBLE":
            consumer   = r.get("consumer", "")
            score_key  = f"contract_compliance_agent:{consumer}"
            confidence = agent_confidence_scores.get(score_key, 1.0)
            if confidence < CONFIDENCE_DOWNGRADE_THRESHOLD:
                r = {
                    **r,
                    "verdict":   "UNCERTAIN",
                    "reasoning": (
                        f"[Downgraded COMPATIBLE → UNCERTAIN — agent confidence "
                        f"{confidence:.2f} < threshold {CONFIDENCE_DOWNGRADE_THRESHOLD}] "
                        + r.get("reasoning", "")
                    ),
                }
                downgrade_count += 1
        adjusted.append(r)

    return adjusted, downgrade_count


def apply_drift_floor(
    uncertainty_score: float,
    drift_report: dict | None,
) -> float:
    """
    Raise uncertainty_score to the drift floor when contract velocity is HIGH/CRITICAL.

    High-velocity services are statistically more likely to introduce breaking
    changes. Ensuring a minimum uncertainty score means the pipeline treats them
    with appropriate caution even when the current diff looks clean.
    """
    if not drift_report:
        return uncertainty_score
    floor = drift_report.get("uncertainty_floor", 0.0)
    return max(uncertainty_score, floor)


def compute_deterministic_confidence(
    graph_loaded: bool,
    consumer_count: int,
    error_occurred: bool,
) -> float:
    """
    Deterministic confidence for non-LLM agents (e.g. DependencyGraphAgent).

    Confidence is based on whether the graph loaded cleanly and whether
    the traversal produced a meaningful result.
    """
    if error_occurred:
        return 0.40
    if not graph_loaded:
        return 0.50
    if consumer_count == 0:
        return 0.85   # definitive "no consumers" — high confidence
    return 0.92       # graph loaded and consumers found
