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
agents/impact_report_agent.py
───────────────────────────────
Phase 4 agent: Aggregates compliance results, generates the cross-repo
impact report, files GitHub issues in affected repos, and notifies teams.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from agents.base import BaseMicroserviceAgent

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {
    "critical": ":red_circle:",
    "high":     ":orange_circle:",
    "medium":   ":yellow_circle:",
    "low":      ":white_circle:",
}


class ImpactReportAgent(BaseMicroserviceAgent):
    """
    Generates the final cross-repo impact report.

    Reads:   state["compliance_results"], state["violations"],
             state["downstream_consumers"], state["impact_score"],
             state["breaking_changes"], state["repo_name"],
             state["pr_number"], state["run_id"]
    Writes:  state["summary"], state["final_report"],
             state["github_issues"], state["slack_sent"],
             state["completed_at"]
    """

    NAME = "impact_report_agent"

    async def execute(self, state: dict) -> dict:
        run_id     = state["run_id"]
        repo_name  = state["repo_name"]
        pr_number  = state["pr_number"]
        provider   = repo_name.split("/")[-1]

        compliance_results = state.get("compliance_results", [])
        violations         = state.get("violations", [])
        consumers          = state.get("downstream_consumers", [])
        impact_score       = state.get("impact_score", 0.0) or 0.0
        breaking_changes   = state.get("breaking_changes", [])

        # ── Aggregate ──────────────────────────────────────────────────────
        breaking_consumers = [r for r in compliance_results if r.get("verdict") == "BREAKING"]
        compatible         = [r for r in compliance_results if r.get("verdict") == "COMPATIBLE"]
        uncertain          = [r for r in compliance_results if r.get("verdict") == "UNCERTAIN"]

        summary = {
            "run_id":             run_id,
            "provider":           provider,
            "pr_number":          pr_number,
            "impact_score":       round(impact_score, 3),
            "impact_radius":      state.get("impact_radius", 0),
            "breaking_consumers": len(breaking_consumers),
            "compatible_consumers": len(compatible),
            "uncertain_consumers":  len(uncertain),
            "total_violations":   len(violations),
            "breaking_changes":   len(breaking_changes),
            "overall_verdict":    _compute_verdict(breaking_consumers, uncertain),
        }

        # ── Render markdown report ─────────────────────────────────────────
        report = _render_report(summary, breaking_consumers, compatible, uncertain,
                                breaking_changes, consumers)

        # ── File GitHub issues ─────────────────────────────────────────────
        github_issues: list[dict] = []
        for result in breaking_consumers:
            consumer_info = next(
                (c for c in consumers if c["consumer"] == result["consumer"]),
                {},
            )
            issue = await self._file_github_issue(
                provider_repo=repo_name,
                consumer_repo=consumer_info.get("repo", ""),
                consumer=result["consumer"],
                violations=result.get("violations", []),
                pr_number=pr_number,
                run_id=run_id,
            )
            if issue:
                github_issues.append(issue)

        # ── Slack notifications ────────────────────────────────────────────
        slack_sent = False
        if breaking_consumers or (impact_score >= 0.4 and consumers):
            slack_sent = await self._notify_slack(summary, breaking_consumers, consumers, pr_number)

        self._log.info(
            "Impact report complete — verdict=%s breaking=%d compatible=%d",
            summary["overall_verdict"], len(breaking_consumers), len(compatible),
        )

        return {
            "summary":      summary,
            "final_report": report,
            "github_issues": github_issues,
            "slack_sent":   slack_sent,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _file_github_issue(
        self,
        provider_repo: str,
        consumer_repo: str,
        consumer: str,
        violations: list[dict],
        pr_number: int,
        run_id: str,
    ) -> dict | None:
        issue_body = _render_issue_body(
            consumer=consumer,
            consumer_repo=consumer_repo,
            violations=violations,
            provider_repo=provider_repo,
            pr_number=pr_number,
            run_id=run_id,
        )
        title = (
            f"[Cross-Repo Impact] PR #{pr_number} in {provider_repo} "
            f"breaks {consumer}"
        )
        issues = {}
        for target_repo in filter(None, {provider_repo, consumer_repo}):
            url = await _github_create_issue(
                target_repo, title, issue_body,
                ["cross-repo-impact", "breaking-change", "automated"],
                self._log,
            )
            if url:
                issues[target_repo] = url
                self._log.info("Filed issue in %s: %s", target_repo, url)
        return issues if issues else None

    async def _notify_slack(
        self,
        summary: dict,
        breaking: list[dict],
        consumers: list[dict],
        pr_number: int,
    ) -> bool:
        # Notify the PR channel and each affected team's channel
        channels: set[str] = {"#cross-repo-impact"}
        for consumer in consumers:
            ch = consumer.get("slack_channel", "")
            if ch:
                channels.add(ch)

        verdict_emoji = ":red_circle:" if breaking else ":large_yellow_circle:"
        text = (
            f"{verdict_emoji} *Cross-Repo Impact Report* — PR #{pr_number}\n"
            f"Provider: `{summary['provider']}` | "
            f"Impact score: `{summary['impact_score']:.2f}` | "
            f"Radius: `{summary['impact_radius']}`\n"
            f"Breaking consumers: *{summary['breaking_consumers']}* | "
            f"Compatible: {summary['compatible_consumers']} | "
            f"Uncertain: {summary['uncertain_consumers']}\n"
            f"Run ID: `{summary['run_id']}`"
        )
        success = await _slack_post(text, list(channels), self._log)
        return success


# ── Rendering helpers ─────────────────────────────────────────────────────────

async def _github_create_issue(
    repo: str, title: str, body: str, labels: list[str], log
) -> str:
    """Create a GitHub issue via REST API. Returns the issue URL or empty string."""
    import httpx, os
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        log.warning("GITHUB_TOKEN not set — cannot file issue in %s", repo)
        return ""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{repo}/issues"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers,
                                     json={"title": title, "body": body, "labels": labels})
            resp.raise_for_status()
            return resp.json().get("html_url", "")
    except Exception as exc:
        log.warning("GitHub issue creation failed in %s: %s", repo, exc)
        return ""


async def _slack_post(text: str, channels: list[str], log) -> bool:
    """Post a Slack message via incoming webhook (SLACK_WEBHOOK_URL) or MCP fallback."""
    import httpx, os
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        log.warning("SLACK_WEBHOOK_URL not set — Slack notification skipped")
        return False
    try:
        payload = {"text": text, "channel": channels[0] if channels else "#cross-repo-impact"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Slack notification failed: %s", exc)
        return False


def _compute_verdict(breaking: list, uncertain: list) -> str:
    if breaking:
        return "BREAKING"
    if uncertain:
        return "UNCERTAIN"
    return "COMPATIBLE"


def _render_report(
    summary: dict,
    breaking: list,
    compatible: list,
    uncertain: list,
    breaking_changes: list[dict],
    consumers: list[dict],
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Cross-Repo Impact Report",
        f"",
        f"**Run ID:** `{summary['run_id']}`  ",
        f"**Provider:** `{summary['provider']}`  ",
        f"**PR:** #{summary['pr_number']}  ",
        f"**Generated:** {ts}  ",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Overall Verdict | **{summary['overall_verdict']}** |",
        f"| Impact Score | `{summary['impact_score']:.3f}` |",
        f"| Impact Radius | {summary['impact_radius']} services |",
        f"| Breaking Consumers | {summary['breaking_consumers']} |",
        f"| Compatible Consumers | {summary['compatible_consumers']} |",
        f"| Uncertain Consumers | {summary['uncertain_consumers']} |",
        f"| Breaking Contract Changes | {summary['breaking_changes']} |",
        f"| Total Violations | {summary['total_violations']} |",
        f"",
    ]

    if breaking_changes:
        lines += [
            "## Breaking Contract Changes",
            "",
        ]
        for c in breaking_changes:
            sev = c.get("severity", "medium").upper()
            lines.append(f"- **[{sev}]** `{c['endpoint']}` — {c['description']}")
        lines.append("")

    if breaking:
        lines += ["## Breaking Consumers", ""]
        for r in breaking:
            lines.append(f"### {r['consumer']}")
            lines.append(f"> {r.get('reasoning', '')}")
            for v in r.get("violations", []):
                emoji = _SEVERITY_EMOJI.get(v.get("severity", "medium"), "")
                lines.append(f"- {emoji} `{v['endpoint']}` — {v['issue']}")
            lines.append("")

    if compatible:
        lines += ["## Compatible Consumers", ""]
        for r in compatible:
            lines.append(f"- ✅ `{r['consumer']}` — {r.get('reasoning', 'No issues detected')}")
        lines.append("")

    if uncertain:
        lines += ["## Uncertain — Manual Review Recommended", ""]
        for r in uncertain:
            lines.append(f"- ⚠️ `{r['consumer']}` — {r.get('reasoning', 'Confidence too low')}")
        lines.append("")

    return "\n".join(lines)


def _render_issue_body(
    consumer: str,
    consumer_repo: str,
    violations: list[dict],
    provider_repo: str,
    pr_number: int,
    run_id: str,
) -> str:
    viol_lines = "\n".join(
        f"- **[{v.get('severity','?').upper()}]** `{v.get('endpoint','')}` — {v.get('issue','')}"
        for v in violations
    ) or "See run report for details."

    return (
        f"## Cross-Repository Breaking Change Detected\n\n"
        f"**Provider:** `{provider_repo}` (PR #{pr_number})\n"
        f"**Consumer:** `{consumer}` (`{consumer_repo}`)\n"
        f"**Run ID:** `{run_id}`\n\n"
        f"### Violations\n\n{viol_lines}\n\n"
        f"### Required Action\n\n"
        f"The consuming service **{consumer}** must be updated before the provider PR is merged, "
        f"or the provider PR must be revised to maintain backwards compatibility.\n\n"
        f"_Automated detection by K11tech Microservice QA System (Paper 3)_"
    )
