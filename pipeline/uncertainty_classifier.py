# -*- coding: utf-8 -*-
"""
pipeline/uncertainty_classifier.py
────────────────────────────────────
Feature 9 — Uncertainty Source Classification.

Distinguishes two structurally different causes of low agent confidence:

  DATA_UNCERTAINTY  — The contract evidence is genuinely ambiguous (aleatoric).
                      Multiple valid interpretations exist; a domain expert could
                      reach either verdict. The uncertainty is in the data, not
                      the agent's knowledge.

  SCOPE_UNCERTAINTY — The agent is outside its evaluation domain (epistemic).
                      The technology, protocol, or usage pattern is unfamiliar;
                      the agent lacks the knowledge to assess this case confidently
                      regardless of evidence quality.

Classification is performed via a secondary LLM prompt that takes the original
agent reasoning as input — no re-analysis of the contract diff is required.
The classifier fires only for consumer results whose confidence falls below
UNCERTAINTY_THRESHOLD, so it adds at most one extra LLM call per low-confidence
consumer per pipeline run.

Failures are non-blocking: a failed or malformed classification returns
UncertaintyType.UNCLASSIFIED and never prevents the HITL gate from firing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ── Taxonomy ──────────────────────────────────────────────────────────────────

class UncertaintyType(str, Enum):
    DATA        = "DATA_UNCERTAINTY"    # aleatoric — ambiguous evidence
    SCOPE       = "SCOPE_UNCERTAINTY"   # epistemic — outside agent domain
    UNCLASSIFIED = "UNCLASSIFIED"       # classification failed or not attempted


@dataclass
class UncertaintyClassification:
    consumer:      str
    unc_type:      UncertaintyType = UncertaintyType.UNCLASSIFIED
    reason:        str = ""
    confidence:    float = 0.0   # the original agent confidence that triggered classification

    def to_dict(self) -> dict:
        return {
            "consumer":   self.consumer,
            "type":       self.unc_type.value,
            "reason":     self.reason,
            "confidence": self.confidence,
        }


# ── Secondary classification prompt ───────────────────────────────────────────

_CLASSIFICATION_SYSTEM = """\
You are classifying the SOURCE of uncertainty in an API compatibility assessment.
Your output must match the exact format specified — nothing else."""

_CLASSIFICATION_PROMPT = """\
An AI agent assessed contract compliance for consumer service '{consumer}' and \
reported verdict={verdict} with confidence={confidence:.2f}.

The agent's reasoning was:
{reasoning}

Classify the PRIMARY source of this low confidence as EXACTLY ONE of:

DATA_UNCERTAINTY — The contract change evidence is genuinely ambiguous.
  Multiple valid interpretations exist; a reasonable expert could reach either
  conclusion. The uncertainty is intrinsic to the change, not the agent's knowledge.
  Examples: a field removal where some consumer code paths may handle absence
  gracefully; a type narrowing that may or may not affect the consumer's usage.

SCOPE_UNCERTAINTY — The agent lacks domain knowledge to assess this case.
  The technology, protocol, or usage pattern is outside the agent's evaluation
  domain. The uncertainty is in what the agent knows, not in the data itself.
  Examples: binary wire format changes, consumer-side framework internals,
  gRPC streaming semantics, database-specific protocol behaviour.

Respond with EXACTLY this format — no preamble, no markdown:
TYPE: DATA_UNCERTAINTY
REASON: <one sentence>

or

TYPE: SCOPE_UNCERTAINTY
REASON: <one sentence>
"""


def _parse_classification(
    consumer: str,
    original_confidence: float,
    raw: str,
) -> UncertaintyClassification:
    """Parse TYPE/REASON lines from LLM response. Returns UNCLASSIFIED on failure."""
    type_match   = re.search(r"TYPE\s*:\s*(DATA_UNCERTAINTY|SCOPE_UNCERTAINTY)", raw, re.I)
    reason_match = re.search(r"REASON\s*:\s*(.+)", raw, re.I)

    if not type_match:
        logger.debug("Uncertainty classifier: could not parse TYPE from response for %s", consumer)
        return UncertaintyClassification(
            consumer=consumer, unc_type=UncertaintyType.UNCLASSIFIED, confidence=original_confidence,
        )

    raw_type = type_match.group(1).upper()
    unc_type = UncertaintyType.DATA if raw_type == "DATA_UNCERTAINTY" else UncertaintyType.SCOPE
    reason = reason_match.group(1).strip() if reason_match else ""

    return UncertaintyClassification(
        consumer=consumer, unc_type=unc_type, reason=reason, confidence=original_confidence,
    )


async def classify_uncertainty(
    result: Any,   # ComplianceResult — avoid circular import
    llm: Any,
) -> UncertaintyClassification:
    """
    Run the secondary classification prompt for a single low-confidence result.
    Returns UncertaintyClassification (UNCLASSIFIED on any failure).
    """
    prompt = _CLASSIFICATION_PROMPT.format(
        consumer=result.consumer,
        verdict=result.verdict,
        confidence=result.confidence,
        reasoning=result.reasoning or "(no reasoning provided)",
    )
    try:
        resp = await llm.ainvoke([
            {"role": "system",  "content": _CLASSIFICATION_SYSTEM},
            {"role": "user",    "content": prompt},
        ])
        return _parse_classification(result.consumer, result.confidence, resp.content)
    except Exception as exc:
        logger.warning("Uncertainty classification failed for %s: %s", result.consumer, exc)
        return UncertaintyClassification(
            consumer=result.consumer,
            unc_type=UncertaintyType.UNCLASSIFIED,
            confidence=result.confidence,
        )


async def classify_all_low_confidence(
    compliance_results: list[Any],   # list[ComplianceResult]
    threshold: float,
    llm: Any,
) -> dict[str, UncertaintyClassification]:
    """
    Classify uncertainty source for every consumer whose confidence < threshold.
    Returns a dict keyed by consumer name.
    Runs classifications concurrently.
    """
    import asyncio

    low_confidence = [r for r in compliance_results if r.confidence < threshold]
    if not low_confidence:
        return {}

    tasks = [classify_uncertainty(r, llm) for r in low_confidence]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    classifications: dict[str, UncertaintyClassification] = {}
    for r, result in zip(low_confidence, results):
        if isinstance(result, Exception):
            logger.warning("Classification gather exception for %s: %s", r.consumer, result)
            classifications[r.consumer] = UncertaintyClassification(
                consumer=r.consumer,
                unc_type=UncertaintyType.UNCLASSIFIED,
                confidence=r.confidence,
            )
        else:
            classifications[r.consumer] = result
            logger.info(
                "Uncertainty classified: consumer=%s type=%s confidence=%.2f",
                r.consumer, result.unc_type.value, r.confidence,
            )
    return classifications
