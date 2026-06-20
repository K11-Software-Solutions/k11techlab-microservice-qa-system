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
contracts/diff.py
──────────────────
Higher-level diff utilities that combine contract_extractor + change_detector.
Entry point for Phase 1 of the pipeline.
"""
from __future__ import annotations

import logging
from typing import Optional

from analyzer.change_detector import diff_endpoints
from contracts.models import ContractDiff, ServiceContract

logger = logging.getLogger(__name__)


def diff_contracts(
    base: Optional[ServiceContract],
    head: Optional[ServiceContract],
) -> Optional[ContractDiff]:
    """
    Produce a ContractDiff between two contract versions.

    Returns None if either contract is None (nothing to diff)
    or if the contracts are from different services (misconfiguration).
    """
    if base is None and head is None:
        logger.debug("Both contracts are None — no diff possible")
        return None

    if base is None:
        # Brand new contract — all endpoints are additions, nothing breaking
        logger.info("No base contract found for %s — treating as initial version", head.service_name)
        from contracts.models import ContractChange, NonBreakingChangeType
        changes = [
            ContractChange(
                change_type=NonBreakingChangeType.ENDPOINT_ADDED,
                is_breaking=False,
                endpoint=f"{ep.method} {ep.path}",
                before="",
                after=f"{ep.method} {ep.path}",
                severity="low",
                description=f"Initial version: endpoint {ep.method} {ep.path}",
            )
            for ep in head.endpoints
        ]
        return ContractDiff(
            service=head.service_name,
            base_version="(none)",
            head_version=head.version,
            changes=changes,
        )

    if head is None:
        # Contract removed entirely — all endpoints removed
        logger.warning("Head contract is None for %s — entire contract removed?", base.service_name)
        from contracts.models import ContractChange, BreakingChangeType
        changes = [
            ContractChange(
                change_type=BreakingChangeType.ENDPOINT_REMOVED,
                is_breaking=True,
                endpoint=f"{ep.method} {ep.path}",
                before=f"{ep.method} {ep.path}",
                after="",
                severity="critical",
                description=f"Contract removed: endpoint {ep.method} {ep.path} no longer exists",
            )
            for ep in base.endpoints
        ]
        return ContractDiff(
            service=base.service_name,
            base_version=base.version,
            head_version="(removed)",
            changes=changes,
        )

    if base.service_name != head.service_name:
        logger.error(
            "Service name mismatch in diff: base=%s head=%s",
            base.service_name, head.service_name,
        )
        return None

    if base.sha == head.sha:
        logger.debug("Contracts are identical (same SHA) — no changes")
        return ContractDiff(
            service=base.service_name,
            base_version=base.version,
            head_version=head.version,
            changes=[],
        )

    return diff_endpoints(base, head)


def serialise_diff(diff: ContractDiff) -> dict:
    """Convert ContractDiff to a JSON-serialisable dict for pipeline state."""
    return {
        "service":        diff.service,
        "base_version":   diff.base_version,
        "head_version":   diff.head_version,
        "has_breaking":   diff.has_breaking_changes,
        "breaking_count": len(diff.breaking),
        "total_changes":  len(diff.changes),
        "changes": [
            {
                "change_type":  c.change_type.value,
                "is_breaking":  c.is_breaking,
                "endpoint":     c.endpoint,
                "field":        c.field,
                "before":       c.before,
                "after":        c.after,
                "severity":     c.severity,
                "description":  c.description,
            }
            for c in diff.changes
        ],
    }


def breaking_changes_as_list(diff: ContractDiff) -> list[dict]:
    """Return only the breaking changes as a list of dicts for pipeline state."""
    return [
        {
            "change_type": c.change_type.value,
            "endpoint":    c.endpoint,
            "field":       c.field,
            "before":      c.before,
            "after":       c.after,
            "severity":    c.severity,
            "description": c.description,
        }
        for c in diff.breaking
    ]
