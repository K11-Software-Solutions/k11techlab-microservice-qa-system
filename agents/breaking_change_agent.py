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
agents/breaking_change_agent.py
─────────────────────────────────
Phase 3 sub-agent: Detects whether a specific consumer's actual usage patterns
will break given the proposed contract change.

Dispatched in parallel via LangGraph Send API — one instance per consumer.
Complements ContractComplianceAgent with a deeper code-search analysis.
"""
from __future__ import annotations

import json
import logging
import re

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

BREAKING_CHANGE_SYSTEM = """\
You are an expert in distributed systems API compatibility.
You have been given:
  1. A list of breaking changes to a provider service's API contract
  2. Code snippets showing how a consumer service calls that API

Analyse whether any of the breaking changes will actually affect the consumer,
considering the specific code patterns observed.

Return ONLY valid JSON — no markdown fences:
{
  "verdict":    "BREAKING" | "COMPATIBLE" | "UNCERTAIN",
  "affected_changes": [
    {"endpoint": str, "change": str, "impact": str, "severity": "critical|high|medium|low"}
  ],
  "reasoning":  str,
  "confidence": float,
  "recommendation": str
}
"""


class BreakingChangeAgent:
    """
    Validates a consumer's code against breaking contract changes.
    More detailed than ContractComplianceAgent — uses actual usage patterns
    fetched from the consumer's repository.
    """

    NAME = "breaking_change_agent"

    def __init__(self, mcp_clients=None, llm=None) -> None:
        self._mcp = mcp_clients or {}
        self._llm = llm or ChatOpenAI(model="gpt-4o", temperature=0)
        self._log = logging.getLogger(f"agents.{self.NAME}")

    async def run(
        self,
        consumer: str,
        consumer_repo: str,
        breaking_changes: list[dict],
        contract_diff: dict,
    ) -> dict:
        """
        Analyse whether breaking_changes will affect `consumer`.

        Returns a dict compatible with pipeline state compliance_results.
        """
        if not breaking_changes:
            return {
                "consumer":   consumer,
                "verdict":    "COMPATIBLE",
                "violations": [],
                "reasoning":  "No breaking changes in this diff",
                "confidence": 1.0,
                "agent":      self.NAME,
            }

        # Fetch usage patterns from GitHub MCP
        usage_patterns = await self._fetch_usage_patterns(consumer_repo, breaking_changes)

        # LLM analysis
        prompt = (
            f"Consumer service: {consumer}\n"
            f"Consumer repo: {consumer_repo}\n\n"
            f"Breaking changes:\n{json.dumps(breaking_changes, indent=2)}\n\n"
            f"Consumer usage patterns (code snippets):\n"
            f"{json.dumps(usage_patterns, indent=2)}"
        )

        try:
            resp = await self._llm.ainvoke([
                {"role": "system", "content": BREAKING_CHANGE_SYSTEM},
                {"role": "user",   "content": prompt},
            ])
            raw = resp.content.strip()
            # Strip markdown fences if present
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
            data = json.loads(match.group(1).strip() if match else raw)

            return {
                "consumer":    consumer,
                "verdict":     data.get("verdict", "UNCERTAIN"),
                "violations":  [
                    {
                        "endpoint":  v.get("endpoint", ""),
                        "issue":     v.get("change", "") + " — " + v.get("impact", ""),
                        "severity":  v.get("severity", "medium"),
                    }
                    for v in data.get("affected_changes", [])
                ],
                "reasoning":      data.get("reasoning", ""),
                "confidence":     float(data.get("confidence", 0.5)),
                "recommendation": data.get("recommendation", ""),
                "agent":          self.NAME,
            }
        except Exception as exc:
            self._log.error("BreakingChangeAgent failed for %s: %s", consumer, exc)
            return {
                "consumer":   consumer,
                "verdict":    "UNCERTAIN",
                "violations": [],
                "reasoning":  f"Analysis failed: {exc}",
                "confidence": 0.0,
                "agent":      self.NAME,
                "error":      str(exc),
            }

    async def _fetch_usage_patterns(
        self,
        consumer_repo: str,
        breaking_changes: list[dict],
    ) -> dict:
        """
        Search the consumer repo for code that uses the changed endpoints.
        """
        patterns: dict = {"snippets": [], "search_terms": []}
        if not consumer_repo:
            return patterns

        github = self._mcp.get("github")
        if not github:
            return patterns

        for change in breaking_changes[:5]:   # limit to first 5 to control API usage
            endpoint = change.get("endpoint", "")
            # Extract path from "METHOD /path" format
            parts = endpoint.split(" ", 1)
            path  = parts[1] if len(parts) == 2 else endpoint

            search_term = path.rstrip("/").split("/")[-1]   # last path segment
            if not search_term or search_term in ("{id}", ":id"):
                search_term = path

            patterns["search_terms"].append(search_term)
            try:
                result = await github.call_tool("search_code", {
                    "query": f"{search_term} repo:{consumer_repo}",
                    "per_page": 3,
                })
                for item in result.get("items", []):
                    patterns["snippets"].append({
                        "file":    item.get("path", ""),
                        "matches": item.get("text_matches", []),
                    })
            except Exception as exc:
                self._log.debug("Code search failed for %s: %s", search_term, exc)

        return patterns
