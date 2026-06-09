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
agents/contract_extractor_agent.py
────────────────────────────────────
Phase 1 agent: Fetches contract files from the PR branch via GitHub MCP,
extracts structured ServiceContracts, diffs against the registry, and
populates the pipeline state with contract_diff and breaking_changes.
"""
from __future__ import annotations

import logging
from typing import Optional

from agents.base import BaseMicroserviceAgent
from analyzer.contract_extractor import detect_contract_files, extract_contract
from contracts.diff import breaking_changes_as_list, diff_contracts, serialise_diff
from contracts.models import ServiceContract

logger = logging.getLogger(__name__)


class ContractExtractorAgent(BaseMicroserviceAgent):
    """
    Extracts and diffs API contracts for a PR.

    Reads:   state["pr_diff"], state["repo_name"], state["pr_number"]
    Writes:  state["current_contract"], state["previous_contract"],
             state["contract_diff"], state["changed_endpoints"],
             state["breaking_changes"]
    """

    NAME = "contract_extractor_agent"

    async def execute(self, state: dict) -> dict:
        repo_name  = state["repo_name"]
        pr_number  = state["pr_number"]
        pr_diff    = state.get("pr_diff", "")
        service    = repo_name.split("/")[-1]   # "org/service-name" → "service-name"

        # ── Step 1: Find changed contract files in the PR diff ─────────────
        # If no diff text, ask GitHub API for the list of changed files directly
        changed_files = _extract_filenames_from_diff(pr_diff)
        if not changed_files and state.get("pr_number"):
            changed_files = await _github_pr_files(
                repo_name, state["pr_number"], self._log
            )
        contract_file_map = detect_contract_files(changed_files)

        if not contract_file_map:
            self._log.info(
                "No contract files changed in PR #%d (%s) — skipping",
                pr_number, repo_name,
            )
            return {
                "current_contract": None,
                "previous_contract": None,
                "contract_diff": None,
                "changed_endpoints": [],
                "breaking_changes": [],
            }

        # ── Step 2: Fetch file content for each contract file via GitHub MCP ─
        head_sha    = state.get("head_sha", "HEAD")
        base_sha    = state.get("base_sha", "")
        head_contract: Optional[ServiceContract] = None
        base_contract: Optional[ServiceContract] = None

        for fmt, paths in contract_file_map.items():
            for file_path in paths[:1]:  # take the first match per format
                try:
                    head_raw = await self._fetch_file(repo_name, file_path, ref=head_sha)
                    if head_raw:
                        head_contract = extract_contract(
                            file_path, head_raw, service, repo_name, head_sha
                        )
                    if base_sha:
                        base_raw = await self._fetch_file(repo_name, file_path, ref=base_sha)
                        if base_raw:
                            base_contract = extract_contract(
                                file_path, base_raw, service, repo_name, base_sha
                            )
                except Exception as exc:
                    self._log.warning("Failed to fetch %s: %s", file_path, exc)

        # ── Step 3: Fetch previous contract from registry if base not in diff ─
        if base_contract is None:
            # Try MCP first, then fall back to direct SQLite ContractRegistry
            try:
                registry_result = await self.call_mcp(
                    "contract_registry", "get_contract",
                    {"service_name": service},
                )
                if registry_result.get("contract"):
                    base_contract = _dict_to_contract(registry_result["contract"])
            except Exception:
                try:
                    import os
                    from contracts.registry import ContractRegistry
                    db = os.getenv("CONTRACT_REGISTRY_DB", "contract_registry.db")
                    async with ContractRegistry(db) as reg:
                        base_contract = await reg.get_contract(service)
                except Exception as exc:
                    self._log.warning("Registry lookup failed for %s: %s", service, exc)

        # ── Step 4: Diff ───────────────────────────────────────────────────
        diff = diff_contracts(base_contract, head_contract)
        if diff is None:
            return {
                "current_contract": None,
                "previous_contract": None,
                "contract_diff": None,
                "changed_endpoints": [],
                "breaking_changes": [],
            }

        changed_endpoints = list({c.endpoint for c in diff.changes})
        breaking_changes  = breaking_changes_as_list(diff)

        self._log.info(
            "Contract diff for %s: %d changes (%d breaking)",
            service, len(diff.changes), len(breaking_changes),
        )

        # ── Step 5: Store new contract in registry ─────────────────────────
        if head_contract:
            try:
                await self.call_mcp(
                    "contract_registry", "store_contract",
                    {"contract": _contract_to_dict(head_contract)},
                )
            except Exception as exc:
                self._log.warning("Failed to store contract in registry: %s", exc)

        return {
            "current_contract":  _contract_to_dict(head_contract) if head_contract else None,
            "previous_contract": _contract_to_dict(base_contract) if base_contract else None,
            "contract_diff":     serialise_diff(diff),
            "changed_endpoints": [{"endpoint": ep} for ep in changed_endpoints],
            "breaking_changes":  breaking_changes,
        }

    async def _fetch_file(self, repo: str, path: str, ref: str = "HEAD") -> str:
        """Fetch file content — GitHub MCP if available, else GitHub REST API."""
        # Try MCP first (if a client is registered)
        if self._mcp.get("github"):
            try:
                result = await self.call_mcp(
                    "github", "get_file_contents",
                    {"repo": repo, "path": path, "ref": ref},
                )
                return result.get("content", "")
            except Exception as exc:
                self._log.debug("MCP fetch failed, falling back to REST: %s", exc)

        # Fall back to GitHub REST API
        return await _github_rest_fetch(repo, path, ref, self._log)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _github_pr_files(repo: str, pr_number: int, log) -> list[str]:
    """Return list of filenames changed in a GitHub PR via REST API."""
    import httpx, os
    token = os.getenv("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers, params={"per_page": 100})
            resp.raise_for_status()
            return [f["filename"] for f in resp.json()]
    except Exception as exc:
        log.warning("Could not fetch PR file list for %s#%s: %s", repo, pr_number, exc)
        return []


async def _github_rest_fetch(repo: str, path: str, ref: str, log) -> str:
    """Fetch a file from GitHub using the REST API and GITHUB_TOKEN env var."""
    import base64
    import httpx
    import os

    token = os.getenv("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    params = {"ref": ref} if ref and ref != "HEAD" else {}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("encoding") == "base64":
                return base64.b64decode(data["content"]).decode("utf-8")
            return data.get("content", "")
    except Exception as exc:
        log.debug("GitHub REST fetch failed %s@%s %s: %s", repo, ref, path, exc)
        return ""


def _extract_filenames_from_diff(pr_diff: str) -> list[str]:
    """Extract changed file paths from a unified diff string."""
    import re
    return re.findall(r'^(?:\+\+\+|---)\s+(?:a/|b/)?(.+)$', pr_diff, re.MULTILINE)


def _contract_to_dict(c: ServiceContract) -> dict:
    return {
        "service_name": c.service_name,
        "repo":         c.repo,
        "format":       c.format.value,
        "version":      c.version,
        "sha":          c.sha,
        "recorded_at":  c.recorded_at.isoformat(),
        "endpoints": [
            {
                "path": ep.path, "method": ep.method,
                "summary": ep.summary, "params": ep.params,
                "request_body": ep.request_body, "responses": ep.responses,
            }
            for ep in c.endpoints
        ],
        "raw": c.raw,
    }


def _dict_to_contract(d: dict) -> ServiceContract:
    from datetime import datetime
    from contracts.models import ContractFormat, Endpoint, ServiceContract
    return ServiceContract(
        service_name=d["service_name"],
        repo=d.get("repo", ""),
        format=ContractFormat(d["format"]),
        version=d.get("version", ""),
        sha=d.get("sha", ""),
        recorded_at=datetime.fromisoformat(d["recorded_at"]),
        endpoints=[
            Endpoint(
                path=ep["path"], method=ep["method"],
                summary=ep.get("summary", ""),
                params=ep.get("params", []),
                request_body=ep.get("request_body"),
                responses=ep.get("responses", {}),
            )
            for ep in d.get("endpoints", [])
        ],
        raw=d.get("raw", {}),
    )
