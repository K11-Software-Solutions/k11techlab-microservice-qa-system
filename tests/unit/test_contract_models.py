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
"""Unit tests for contract extraction and change detection."""
import pytest
from datetime import datetime, timezone

from contracts.models import (
    BreakingChangeType,
    ContractFormat,
    Endpoint,
    NonBreakingChangeType,
    ServiceContract,
)
from analyzer.contract_extractor import extract_openapi_contract
from analyzer.change_detector import diff_endpoints


def _make_contract(raw: dict, sha: str = "test") -> ServiceContract:
    import yaml
    return extract_openapi_contract(yaml.dump(raw), "test-service", "org/test", sha)


OPENAPI_V1 = {
    "openapi": "3.0.0",
    "info": {"title": "Test", "version": "1.0.0"},
    "paths": {
        "/api/v1/users": {
            "get": {"summary": "List", "responses": {"200": {"description": "OK"}}},
            "post": {
                "summary": "Create",
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["email"],
                        "properties": {"email": {"type": "string"}, "name": {"type": "string"}},
                    }}},
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/api/v1/users/{id}": {
            "get": {"summary": "Get", "responses": {"200": {"description": "OK"}}},
        },
    },
}

OPENAPI_V2_REMOVE_ENDPOINT = {
    "openapi": "3.0.0",
    "info": {"title": "Test", "version": "2.0.0"},
    "paths": {
        "/api/v1/users": {
            "get": {"summary": "List", "responses": {"200": {"description": "OK"}}},
            # POST removed
        },
        # /api/v1/users/{id} REMOVED
    },
}

OPENAPI_V2_ADD_REQUIRED_FIELD = {
    "openapi": "3.0.0",
    "info": {"title": "Test", "version": "1.1.0"},
    "paths": {
        "/api/v1/users": {
            "get": {"summary": "List", "responses": {"200": {"description": "OK"}}},
            "post": {
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["email", "phone"],  # phone added as required
                        "properties": {
                            "email": {"type": "string"},
                            "name":  {"type": "string"},
                            "phone": {"type": "string"},
                        },
                    }}},
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/api/v1/users/{id}": {
            "get": {"summary": "Get", "responses": {"200": {"description": "OK"}}},
        },
    },
}

OPENAPI_V2_ADD_OPTIONAL_FIELD = {
    "openapi": "3.0.0",
    "info": {"title": "Test", "version": "1.1.0"},
    "paths": {
        "/api/v1/users": {
            "get": {"summary": "List", "responses": {"200": {"description": "OK"}}},
            "post": {
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["email"],
                        "properties": {
                            "email":  {"type": "string"},
                            "name":   {"type": "string"},
                            "avatar": {"type": "string"},  # optional addition
                        },
                    }}},
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/api/v1/users/{id}": {
            "get": {"summary": "Get", "responses": {"200": {"description": "OK"}}},
        },
        "/api/v1/users/{id}/preferences": {  # new endpoint
            "get": {"summary": "Prefs", "responses": {"200": {"description": "OK"}}},
        },
    },
}


# ── Extraction tests ──────────────────────────────────────────────────────────

def test_extract_openapi_endpoints():
    contract = _make_contract(OPENAPI_V1)
    paths = {(ep.path, ep.method) for ep in contract.endpoints}
    assert ("/api/v1/users", "GET") in paths
    assert ("/api/v1/users", "POST") in paths
    assert ("/api/v1/users/{id}", "GET") in paths


def test_extract_openapi_version():
    contract = _make_contract(OPENAPI_V1)
    assert contract.version == "1.0.0"


def test_extract_openapi_format():
    contract = _make_contract(OPENAPI_V1)
    assert contract.format == ContractFormat.OPENAPI


# ── Diff tests ────────────────────────────────────────────────────────────────

def test_diff_endpoint_removed():
    base = _make_contract(OPENAPI_V1, sha="v1")
    head = _make_contract(OPENAPI_V2_REMOVE_ENDPOINT, sha="v2")
    diff = diff_endpoints(base, head)

    breaking = diff.breaking
    assert diff.has_breaking_changes
    removed = [c for c in breaking if c.change_type == BreakingChangeType.ENDPOINT_REMOVED]
    assert len(removed) >= 1


def test_diff_required_field_added():
    base = _make_contract(OPENAPI_V1, sha="v1")
    head = _make_contract(OPENAPI_V2_ADD_REQUIRED_FIELD, sha="v2")
    diff = diff_endpoints(base, head)

    assert diff.has_breaking_changes
    req_added = [c for c in diff.breaking
                 if c.change_type == BreakingChangeType.REQUIRED_FIELD_ADDED]
    assert len(req_added) >= 1
    assert any(c.field == "phone" for c in req_added)


def test_diff_non_breaking_addition():
    base = _make_contract(OPENAPI_V1, sha="v1")
    head = _make_contract(OPENAPI_V2_ADD_OPTIONAL_FIELD, sha="v2")
    diff = diff_endpoints(base, head)

    assert not diff.has_breaking_changes
    added = [c for c in diff.changes
             if c.change_type == NonBreakingChangeType.ENDPOINT_ADDED]
    assert len(added) >= 1


def test_diff_identical_contracts():
    base = _make_contract(OPENAPI_V1, sha="same")
    head = _make_contract(OPENAPI_V1, sha="same2")
    diff = diff_endpoints(base, head)
    assert not diff.has_breaking_changes
