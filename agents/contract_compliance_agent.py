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
agents/contract_compliance_agent.py
─────────────────────────────────────
ContractComplianceAgent — validates a proposed contract change against
a downstream consumer's current usage patterns.

One agent instance per consumer service, dispatched in parallel via
LangGraph Send API (same pattern as Phase 2 in Paper 1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import os

logger = logging.getLogger(__name__)


_ADAPTIVE_THINKING_MODELS = {"claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6"}


def _default_llm():
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    model = os.getenv("LLM_MODEL", "claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        # Opus 4.7+ removes temperature/top_p (adaptive thinking only) — omit it
        kwargs = {} if model in _ADAPTIVE_THINKING_MODELS else {"temperature": 0}
        return ChatAnthropic(model=model, **kwargs)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=model, temperature=0)

COMPLIANCE_SYSTEM = """\
You are an API compatibility expert. Given a contract diff and a consumer service's
usage patterns, determine whether the contract change will break the consumer.

Return ONLY valid JSON:
{
  "verdict": "COMPATIBLE" | "BREAKING" | "UNCERTAIN",
  "violations": [{"endpoint": str, "issue": str, "severity": "critical|high|medium|low"}],
  "reasoning": str,
  "confidence": float
}
"""


@dataclass
class ComplianceResult:
    consumer:    str
    verdict:     str           # COMPATIBLE | BREAKING | UNCERTAIN
    violations:  list[dict]   = field(default_factory=list)
    reasoning:   str           = ""
    confidence:  float         = 0.0
    error:       str           = ""

    @property
    def is_breaking(self) -> bool:
        return self.verdict == "BREAKING"


class ContractComplianceAgent:
    """
    Validates one consumer's compatibility with a proposed contract change.
    Instantiated per consumer and dispatched in parallel.
    """

    NAME = "contract_compliance_agent"

    def __init__(self, mcp_clients=None, llm=None) -> None:
        self._mcp = mcp_clients or {}
        self._llm = llm or _default_llm()
        self._log = logging.getLogger(f"agents.{self.NAME}")

    async def run(self, consumer: str, contract_diff: dict, usage_patterns: dict) -> ComplianceResult:
        """
        Check whether `contract_diff` breaks `consumer` given its `usage_patterns`.
        """
        import json, re
        prompt = (
            f"Consumer service: {consumer}\n\n"
            f"Contract diff (breaking changes only):\n{json.dumps(contract_diff, indent=2)}\n\n"
            f"Consumer usage patterns:\n{json.dumps(usage_patterns, indent=2)}"
        )
        try:
            resp = await self._llm.ainvoke([
                {"role": "system", "content": COMPLIANCE_SYSTEM},
                {"role": "user",   "content": prompt},
            ])
            raw = resp.content.strip()
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
            if match:
                raw = match.group(1).strip()
            data = json.loads(raw)
            return ComplianceResult(
                consumer=consumer,
                verdict=data.get("verdict", "UNCERTAIN"),
                violations=data.get("violations", []),
                reasoning=data.get("reasoning", ""),
                confidence=float(data.get("confidence", 0.5)),
            )
        except Exception as exc:
            self._log.error("Compliance check for %s failed: %s", consumer, exc)
            return ComplianceResult(
                consumer=consumer, verdict="UNCERTAIN",
                error=str(exc), confidence=0.0,
            )
