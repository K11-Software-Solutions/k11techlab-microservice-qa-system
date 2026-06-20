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
analyzer/contract_extractor.py
────────────────────────────────
Extracts API contracts from a PR branch.

Supports:
  - OpenAPI 3.x / Swagger 2.0  (.yaml / .json)
  - gRPC protobuf               (*.proto)
  - GraphQL                     (schema.graphql / *.graphql)
  - AsyncAPI 2.x                (asyncapi.yaml)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from contracts.models import (
    ContractFormat,
    Endpoint,
    ServiceContract,
)

logger = logging.getLogger(__name__)

# ── File patterns per contract format ────────────────────────────────────────
_OPENAPI_NAMES = {
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
}
_PROTO_SUFFIX   = ".proto"
_GRAPHQL_SUFFIX = ".graphql"
_ASYNCAPI_NAMES = {"asyncapi.yaml", "asyncapi.yml", "asyncapi.json"}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


# ── Public helpers ────────────────────────────────────────────────────────────

def detect_contract_files(file_paths: list[str]) -> dict[ContractFormat, list[str]]:
    """
    Given a list of file paths (from a PR diff), return a mapping of
    ContractFormat → [matching paths].
    """
    result: dict[ContractFormat, list[str]] = {f: [] for f in ContractFormat}
    for p in file_paths:
        name = Path(p).name.lower()
        if name in _OPENAPI_NAMES:
            result[ContractFormat.OPENAPI].append(p)
        elif name in _ASYNCAPI_NAMES:
            result[ContractFormat.ASYNCAPI].append(p)
        elif p.endswith(_PROTO_SUFFIX):
            result[ContractFormat.GRPC].append(p)
        elif p.endswith(_GRAPHQL_SUFFIX):
            result[ContractFormat.GRAPHQL].append(p)
    return {k: v for k, v in result.items() if v}


def _resolve_ref(schema: Any, doc: dict, _depth: int = 0) -> Any:
    """
    Recursively resolve JSON Schema $ref pointers within an OpenAPI document.
    Supports local references only (#/components/schemas/Foo).
    Stops at depth 5 to avoid circular reference loops.
    """
    if _depth > 5 or not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/"):
            parts = ref.lstrip("#/").split("/")
            node = doc
            for part in parts:
                node = node.get(part, {})
            return _resolve_ref(node, doc, _depth + 1)
        return schema
    return {k: _resolve_ref(v, doc, _depth + 1) for k, v in schema.items()}


def extract_openapi_contract(
    raw_content: str,
    service_name: str,
    repo: str,
    sha: str,
) -> ServiceContract:
    """Parse an OpenAPI / Swagger document into a ServiceContract."""
    from datetime import datetime, timezone

    try:
        data: dict = yaml.safe_load(raw_content) if not raw_content.lstrip().startswith("{") \
                     else json.loads(raw_content)
    except Exception as exc:
        raise ValueError(f"Failed to parse OpenAPI contract: {exc}") from exc

    version = (
        data.get("info", {}).get("version", "unknown")
        or data.get("openapi", data.get("swagger", "unknown"))
    )
    endpoints: list[Endpoint] = []

    paths = data.get("paths", {})
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            params       = operation.get("parameters", [])
            request_body = _resolve_ref(operation.get("requestBody"), data)
            responses    = _resolve_ref(operation.get("responses", {}), data)
            endpoints.append(Endpoint(
                path=path,
                method=method.upper(),
                summary=operation.get("summary", ""),
                params=params,
                request_body=request_body,
                responses=responses,
            ))

    return ServiceContract(
        service_name=service_name,
        repo=repo,
        format=ContractFormat.OPENAPI,
        version=version,
        sha=sha,
        recorded_at=datetime.now(timezone.utc),
        endpoints=endpoints,
        raw=data,
    )


def extract_grpc_contract(
    proto_content: str,
    service_name: str,
    repo: str,
    sha: str,
) -> ServiceContract:
    """
    Lightweight proto parser — extracts rpc method signatures.
    Not a full protobuf compiler; sufficient for contract diffing.
    """
    from datetime import datetime, timezone

    endpoints: list[Endpoint] = []
    # Find service blocks
    service_blocks = re.findall(
        r'service\s+(\w+)\s*\{([^}]+)\}', proto_content, re.DOTALL
    )
    for svc_name, block in service_blocks:
        rpcs = re.findall(
            r'rpc\s+(\w+)\s*\(([^)]+)\)\s*returns\s*\(([^)]+)\)',
            block,
        )
        for rpc_name, request_type, response_type in rpcs:
            endpoints.append(Endpoint(
                path=f"/{svc_name}/{rpc_name}",
                method="RPC",
                summary=f"{rpc_name}({request_type.strip()}) → {response_type.strip()}",
            ))

    # Extract package version from proto option or comment
    ver_match = re.search(r'option\s+java_package\s*=\s*"[^"]*\.v(\d+)"', proto_content)
    version = f"v{ver_match.group(1)}" if ver_match else "v1"

    return ServiceContract(
        service_name=service_name,
        repo=repo,
        format=ContractFormat.GRPC,
        version=version,
        sha=sha,
        recorded_at=datetime.now(timezone.utc),
        endpoints=endpoints,
        raw={"proto_source": proto_content},
    )


def extract_graphql_contract(
    schema_content: str,
    service_name: str,
    repo: str,
    sha: str,
) -> ServiceContract:
    """
    Lightweight GraphQL schema parser — extracts Query / Mutation fields.
    """
    from datetime import datetime, timezone

    endpoints: list[Endpoint] = []
    # Find Query and Mutation type blocks
    for op_type in ("Query", "Mutation", "Subscription"):
        pattern = rf'type\s+{op_type}\s*\{{([^}}]+)\}}'
        block_match = re.search(pattern, schema_content, re.DOTALL)
        if not block_match:
            continue
        block = block_match.group(1)
        fields = re.findall(r'(\w+)\s*(?:\([^)]*\))?\s*:\s*([\w!\[\]]+)', block)
        for field_name, return_type in fields:
            endpoints.append(Endpoint(
                path=f"/{op_type}/{field_name}",
                method=op_type.upper(),
                summary=f"{field_name}: {return_type}",
            ))

    return ServiceContract(
        service_name=service_name,
        repo=repo,
        format=ContractFormat.GRAPHQL,
        version="1",
        sha=sha,
        recorded_at=datetime.now(timezone.utc),
        endpoints=endpoints,
        raw={"schema": schema_content},
    )


def _looks_like_openapi(raw: str) -> bool:
    """Sniff: does the content appear to be an OpenAPI/Swagger spec?"""
    # Fast path: check common locations (first 4KB and last 2KB for large files)
    for chunk in (raw[:4096], raw[-2048:]):
        if '"openapi"' in chunk or '"swagger"' in chunk or "openapi:" in chunk or "swagger:" in chunk:
            return True
    # Slower path: parse root keys for large files where the version key may be buried
    try:
        import json as _json
        data = _json.loads(raw)
        return "openapi" in data or "swagger" in data
    except Exception:
        pass
    try:
        import yaml as _yaml
        data = _yaml.safe_load(raw)
        return isinstance(data, dict) and ("openapi" in data or "swagger" in data)
    except Exception:
        pass
    return False


def extract_contract(
    file_path: str,
    raw_content: str,
    service_name: str,
    repo: str,
    sha: str,
) -> ServiceContract | None:
    """
    Dispatch to the appropriate extractor based on file path.
    Returns None if the file is not a recognised contract format.
    """
    name = Path(file_path).name.lower()
    if name in _OPENAPI_NAMES or name in _ASYNCAPI_NAMES:
        return extract_openapi_contract(raw_content, service_name, repo, sha)
    if file_path.endswith(_PROTO_SUFFIX):
        return extract_grpc_contract(raw_content, service_name, repo, sha)
    if file_path.endswith(_GRAPHQL_SUFFIX):
        return extract_graphql_contract(raw_content, service_name, repo, sha)
    # Content-sniff fallback: any .json/.yaml/.yml file that looks like OpenAPI
    if name.endswith((".json", ".yaml", ".yml")) and _looks_like_openapi(raw_content):
        return extract_openapi_contract(raw_content, service_name, repo, sha)
    return None
