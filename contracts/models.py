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
contracts/models.py
────────────────────
Data models for API contracts, versions, and breaking change classification.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class ContractFormat(str, Enum):
    OPENAPI   = "openapi"
    GRPC      = "grpc"
    GRAPHQL   = "graphql"
    ASYNCAPI  = "asyncapi"


class BreakingChangeType(str, Enum):
    ENDPOINT_REMOVED       = "endpoint_removed"
    FIELD_REMOVED          = "field_removed"
    FIELD_TYPE_CHANGED     = "field_type_changed"
    REQUIRED_FIELD_ADDED   = "required_field_added"
    RESPONSE_CODE_REMOVED  = "response_code_removed"
    AUTH_SCHEME_CHANGED    = "auth_scheme_changed"
    INCOMPATIBLE_CHANGE    = "incompatible_change"

class NonBreakingChangeType(str, Enum):
    ENDPOINT_ADDED         = "endpoint_added"
    OPTIONAL_FIELD_ADDED   = "optional_field_added"
    DESCRIPTION_CHANGED    = "description_changed"
    EXAMPLE_CHANGED        = "example_changed"


@dataclass
class Endpoint:
    path:    str
    method:  str
    summary: str = ""
    params:  list[dict] = field(default_factory=list)
    request_body: dict | None = None
    responses: dict = field(default_factory=dict)


@dataclass
class ServiceContract:
    service_name: str
    repo:         str
    format:       ContractFormat
    version:      str
    sha:          str             # git commit SHA
    recorded_at:  datetime
    endpoints:    list[Endpoint] = field(default_factory=list)
    raw:          dict = field(default_factory=dict)


@dataclass
class ContractChange:
    change_type:     BreakingChangeType | NonBreakingChangeType
    is_breaking:     bool
    endpoint:        str
    field:           str = ""
    before:          str = ""
    after:           str = ""
    severity:        Literal["critical", "high", "medium", "low"] = "medium"
    description:     str = ""


@dataclass
class ContractDiff:
    service:         str
    base_version:    str
    head_version:    str
    changes:         list[ContractChange] = field(default_factory=list)

    @property
    def breaking(self) -> list[ContractChange]:
        return [c for c in self.changes if c.is_breaking]

    @property
    def has_breaking_changes(self) -> bool:
        return bool(self.breaking)
