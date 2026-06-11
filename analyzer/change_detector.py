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
analyzer/change_detector.py
─────────────────────────────
Diffs two ServiceContract versions and classifies each change as
breaking or non-breaking.

Breaking change taxonomy (OpenAPI-centric, adapted for gRPC/GraphQL):
  - Endpoint / field removed
  - Field type changed
  - Required field added to request body
  - Response status code removed
  - Auth scheme changed
"""
from __future__ import annotations

import logging
from typing import Literal

from contracts.models import (
    BreakingChangeType,
    ContractChange,
    ContractDiff,
    NonBreakingChangeType,
    ServiceContract,
)

logger = logging.getLogger(__name__)

_SEVERITY_MAP: dict[BreakingChangeType, Literal["critical", "high", "medium", "low"]] = {
    BreakingChangeType.ENDPOINT_REMOVED:      "critical",
    BreakingChangeType.FIELD_TYPE_CHANGED:    "high",
    BreakingChangeType.REQUIRED_FIELD_ADDED:  "high",
    BreakingChangeType.FIELD_REMOVED:         "high",
    BreakingChangeType.RESPONSE_CODE_REMOVED: "medium",
    BreakingChangeType.AUTH_SCHEME_CHANGED:   "critical",
    BreakingChangeType.INCOMPATIBLE_CHANGE:   "medium",
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _endpoint_key(endpoint) -> str:
    return f"{endpoint.method.upper()} {endpoint.path}"


def _schema_type(schema: dict | None) -> str:
    if not schema:
        return ""
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    return schema.get("type", "")


def _required_fields(schema: dict) -> set[str]:
    """Extract required field names from a JSON Schema object."""
    return set(schema.get("required", []))


def _properties(schema: dict) -> dict[str, dict]:
    return schema.get("properties", {})


def _resolve_body_schema(request_body: dict | None) -> dict:
    """Extract the first JSON content schema from a requestBody object."""
    if not request_body:
        return {}
    content = request_body.get("content", {})
    for media_type in ("application/json", "application/x-www-form-urlencoded"):
        schema = content.get(media_type, {}).get("schema", {})
        if schema:
            return schema
    return {}


def _resolve_response_schema(responses: dict | None) -> dict:
    """Extract the schema from the primary 2xx success response."""
    if not responses:
        return {}
    for code in sorted(str(k) for k in responses.keys()):
        if code.startswith("2"):
            resp = responses.get(code) or responses.get(int(code), {})
            if isinstance(resp, dict):
                schema = resp.get("content", {}).get("application/json", {}).get("schema", {})
                if schema:
                    return schema
    return {}


# ── Core diff logic ───────────────────────────────────────────────────────────

def diff_endpoints(
    base: ServiceContract,
    head: ServiceContract,
) -> ContractDiff:
    """
    Compare two contracts and return a ContractDiff with all classified changes.
    """
    changes: list[ContractChange] = []

    base_eps = {_endpoint_key(ep): ep for ep in base.endpoints}
    head_eps = {_endpoint_key(ep): ep for ep in head.endpoints}

    # 1. Removed endpoints
    for key in set(base_eps) - set(head_eps):
        changes.append(ContractChange(
            change_type=BreakingChangeType.ENDPOINT_REMOVED,
            is_breaking=True,
            endpoint=key,
            before=key,
            after="",
            severity=_SEVERITY_MAP[BreakingChangeType.ENDPOINT_REMOVED],
            description=f"Endpoint {key} was removed",
        ))

    # 2. Added endpoints (non-breaking)
    for key in set(head_eps) - set(base_eps):
        changes.append(ContractChange(
            change_type=NonBreakingChangeType.ENDPOINT_ADDED,
            is_breaking=False,
            endpoint=key,
            before="",
            after=key,
            severity="low",
            description=f"New endpoint {key} added",
        ))

    # 3. Modified endpoints
    for key in set(base_eps) & set(head_eps):
        base_ep = base_eps[key]
        head_ep = head_eps[key]
        changes.extend(_diff_single_endpoint(key, base_ep, head_ep))

    return ContractDiff(
        service=base.service_name,
        base_version=base.version,
        head_version=head.version,
        changes=changes,
    )


def _diff_single_endpoint(key: str, base_ep, head_ep) -> list[ContractChange]:
    changes: list[ContractChange] = []

    # Request body changes
    base_schema = _resolve_body_schema(base_ep.request_body)
    head_schema = _resolve_body_schema(head_ep.request_body)
    if base_schema or head_schema:
        changes.extend(_diff_schema(key, base_schema, head_schema))

    # Response body changes — only compare when BOTH sides have an explicit schema
    # (if one side omits the schema entirely, treat as unknown rather than "all fields removed")
    base_resp = _resolve_response_schema(base_ep.responses)
    head_resp  = _resolve_response_schema(head_ep.responses)
    if base_resp and head_resp:
        changes.extend(_diff_response_schema(key, base_resp, head_resp))

    # Response status code changes
    base_codes = set(str(k) for k in (base_ep.responses or {}).keys())
    head_codes  = set(str(k) for k in (head_ep.responses or {}).keys())
    for removed_code in base_codes - head_codes:
        if removed_code.startswith("2"):  # Removing a success code is breaking
            changes.append(ContractChange(
                change_type=BreakingChangeType.RESPONSE_CODE_REMOVED,
                is_breaking=True,
                endpoint=key,
                field=removed_code,
                before=removed_code,
                after="",
                severity=_SEVERITY_MAP[BreakingChangeType.RESPONSE_CODE_REMOVED],
                description=f"Success response code {removed_code} removed from {key}",
            ))

    # Parameter changes (path/query params removed = breaking)
    base_params = {p.get("name", ""): p for p in (base_ep.params or []) if isinstance(p, dict)}
    head_params = {p.get("name", ""): p for p in (head_ep.params or []) if isinstance(p, dict)}
    for removed_param in set(base_params) - set(head_params):
        changes.append(ContractChange(
            change_type=BreakingChangeType.FIELD_REMOVED,
            is_breaking=True,
            endpoint=key,
            field=removed_param,
            before=removed_param,
            after="",
            severity=_SEVERITY_MAP[BreakingChangeType.FIELD_REMOVED],
            description=f"Parameter '{removed_param}' removed from {key}",
        ))
    for added_param in set(head_params) - set(base_params):
        param = head_params[added_param]
        if param.get("required"):
            changes.append(ContractChange(
                change_type=BreakingChangeType.REQUIRED_FIELD_ADDED,
                is_breaking=True,
                endpoint=key,
                field=added_param,
                before="",
                after=added_param,
                severity=_SEVERITY_MAP[BreakingChangeType.REQUIRED_FIELD_ADDED],
                description=f"New required parameter '{added_param}' added to {key}",
            ))

    return changes


def _diff_response_schema(
    endpoint_key: str,
    base_schema: dict,
    head_schema: dict,
) -> list[ContractChange]:
    """Diff response body schemas. Rules differ from request body schemas."""
    changes: list[ContractChange] = []
    base_props = _properties(base_schema)
    head_props  = _properties(head_schema)
    base_req    = _required_fields(base_schema)
    head_req    = _required_fields(head_schema)

    # Field removed from response → breaking (consumers reading it get null/KeyError)
    for field_name in set(base_props) - set(head_props):
        changes.append(ContractChange(
            change_type=BreakingChangeType.FIELD_REMOVED,
            is_breaking=True,
            endpoint=endpoint_key,
            field=field_name,
            before=_schema_type(base_props[field_name]),
            after="",
            severity=_SEVERITY_MAP[BreakingChangeType.FIELD_REMOVED],
            description=f"Response field '{field_name}' removed from {endpoint_key}",
        ))

    # New optional field in response → non-breaking (consumers ignore unknown fields)
    for field_name in set(head_props) - set(base_props):
        changes.append(ContractChange(
            change_type=NonBreakingChangeType.OPTIONAL_FIELD_ADDED,
            is_breaking=False,
            endpoint=endpoint_key,
            field=field_name,
            before="",
            after=_schema_type(head_props[field_name]),
            severity="low",
            description=f"New optional field '{field_name}' added to response of {endpoint_key}",
        ))

    # Fields present in both — check type changes and enum changes
    for field_name in set(base_props) & set(head_props):
        base_field = base_props[field_name]
        head_field  = head_props[field_name]

        base_type = _schema_type(base_field)
        head_type  = _schema_type(head_field)
        if base_type and head_type and base_type != head_type:
            changes.append(ContractChange(
                change_type=BreakingChangeType.FIELD_TYPE_CHANGED,
                is_breaking=True,
                endpoint=endpoint_key,
                field=field_name,
                before=base_type,
                after=head_type,
                severity=_SEVERITY_MAP[BreakingChangeType.FIELD_TYPE_CHANGED],
                description=(
                    f"Response field '{field_name}' type changed "
                    f"from '{base_type}' to '{head_type}' in {endpoint_key}"
                ),
            ))

        # Enum values changed — potentially breaking if consumers compare literals
        base_enum = set(base_field.get("enum") or [])
        head_enum  = set(head_field.get("enum") or [])
        if base_enum and base_enum != head_enum:
            removed_vals = base_enum - head_enum
            is_breaking  = bool(removed_vals)
            description  = (
                f"Enum values for '{field_name}' changed: "
                f"removed {sorted(removed_vals)}, now {sorted(head_enum)}"
                if removed_vals else
                f"Enum values for '{field_name}' expanded: added {sorted(head_enum - base_enum)}"
            )
            changes.append(ContractChange(
                change_type=BreakingChangeType.INCOMPATIBLE_CHANGE,
                is_breaking=is_breaking,
                endpoint=endpoint_key,
                field=field_name,
                before=str(sorted(base_enum)),
                after=str(sorted(head_enum)),
                severity="medium" if is_breaking else "low",
                description=description,
            ))

    # Field removed from required list → consumers relying on guaranteed value may get null
    for field_name in base_req - head_req:
        if field_name in base_props and field_name in head_props:
            changes.append(ContractChange(
                change_type=BreakingChangeType.FIELD_REMOVED,
                is_breaking=True,
                endpoint=endpoint_key,
                field=field_name,
                before="required",
                after="optional",
                severity="medium",
                description=f"Response field '{field_name}' is no longer guaranteed (required → optional) in {endpoint_key}",
            ))

    return changes


def _diff_schema(
    endpoint_key: str,
    base_schema: dict,
    head_schema: dict,
) -> list[ContractChange]:
    changes: list[ContractChange] = []

    base_props = _properties(base_schema)
    head_props  = _properties(head_schema)
    base_req    = _required_fields(base_schema)
    head_req    = _required_fields(head_schema)

    # Removed fields
    for field_name in set(base_props) - set(head_props):
        changes.append(ContractChange(
            change_type=BreakingChangeType.FIELD_REMOVED,
            is_breaking=True,
            endpoint=endpoint_key,
            field=field_name,
            before=_schema_type(base_props[field_name]),
            after="",
            severity=_SEVERITY_MAP[BreakingChangeType.FIELD_REMOVED],
            description=f"Field '{field_name}' removed from request body of {endpoint_key}",
        ))

    # Type changed
    for field_name in set(base_props) & set(head_props):
        base_type = _schema_type(base_props[field_name])
        head_type  = _schema_type(head_props[field_name])
        if base_type and head_type and base_type != head_type:
            changes.append(ContractChange(
                change_type=BreakingChangeType.FIELD_TYPE_CHANGED,
                is_breaking=True,
                endpoint=endpoint_key,
                field=field_name,
                before=base_type,
                after=head_type,
                severity=_SEVERITY_MAP[BreakingChangeType.FIELD_TYPE_CHANGED],
                description=(
                    f"Field '{field_name}' type changed "
                    f"from '{base_type}' to '{head_type}' in {endpoint_key}"
                ),
            ))

    # New required fields (not in base required)
    for field_name in head_req - base_req:
        if field_name in head_props:
            changes.append(ContractChange(
                change_type=BreakingChangeType.REQUIRED_FIELD_ADDED,
                is_breaking=True,
                endpoint=endpoint_key,
                field=field_name,
                before="optional",
                after="required",
                severity=_SEVERITY_MAP[BreakingChangeType.REQUIRED_FIELD_ADDED],
                description=f"Field '{field_name}' became required in {endpoint_key}",
            ))

    return changes
