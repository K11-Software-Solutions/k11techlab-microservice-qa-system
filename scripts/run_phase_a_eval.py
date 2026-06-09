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
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_phase_a_eval.py
────────────────────────────
Phase A controlled evaluation — creates 15 PR scenarios on the K11-Software-Solutions
GitHub repos, triggers the webhook pipeline for each, collects verdicts, runs
diff-only and graph+diff baselines, and writes eval/results_phase_a.json.

Usage:
    python scripts/run_phase_a_eval.py

Prerequisites:
    - Webhook server running:  uvicorn api.webhook:app --port 9001
    - GITHUB_TOKEN and ANTHROPIC_API_KEY set in .env
    - Services registered in dependency_graph.db and contract_registry.db
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phase_a_eval")

ORG            = "K11-Software-Solutions"
WEBHOOK_URL    = "http://localhost:9001/webhook/github"
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "k11tech-webhook-secret-2026")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
RESULTS_PATH   = Path(__file__).parent.parent / "eval" / "results_phase_a.json"

# ── OpenAPI specs ──────────────────────────────────────────────────────────────

# k11-user-service baseline (v1.0.0) — for reference
_USER_BASE = """openapi: 3.0.0
info:
  title: User Service
  version: 1.0.0
paths:
  /api/v2/users:
    get:
      summary: List all users
      operationId: list_users
      parameters:
        - {name: page, in: query, required: false, schema: {type: integer, default: 1}}
        - {name: page_size, in: query, required: false, schema: {type: integer, default: 20}}
      responses:
        '200':
          description: List of users
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/User'
    post:
      summary: Create a new user
      operationId: create_user
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateUser'
      responses:
        '201':
          description: User created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
  /api/v2/users/{id}:
    get:
      summary: Get a user by ID
      operationId: get_user
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        '200':
          description: User found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '404':
          description: User not found
  /api/v2/users/{id}/contact:
    get:
      summary: Get user contact info
      operationId: get_user_contact
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        '200':
          description: Contact info
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ContactInfo'
        '404':
          description: User not found
components:
  schemas:
    User:
      type: object
      required: [id, email, name]
      properties:
        id:   {type: string}
        email: {type: string, format: email}
        name:  {type: string}
        avatar_url: {type: string, nullable: true}
    CreateUser:
      type: object
      required: [email, name]
      properties:
        email: {type: string, format: email}
        name:  {type: string}
    ContactInfo:
      type: object
      required: [email]
      properties:
        email: {type: string, format: email}
        phone: {type: string, nullable: true}
"""

# ── Scenario definitions ───────────────────────────────────────────────────────

@dataclass
class Scenario:
    id:                   str
    repo:                 str
    branch:               str
    title:                str
    body:                 str
    ground_truth:         str            # BREAKING | COMPATIBLE
    expected_consumers:   list[str]      = field(default_factory=list)
    openapi_content:      str            = ""
    notes:                str            = ""


def _user_spec(version: str, paths: dict, schemas: dict | None = None) -> str:
    """Build a user-service openapi.yaml string."""
    import yaml
    base = {
        "openapi": "3.0.0",
        "info": {"title": "User Service", "version": version},
        "paths": paths,
        "components": {"schemas": schemas or {
            "User": {
                "type": "object",
                "required": ["id", "email", "name"],
                "properties": {
                    "id": {"type": "string"},
                    "email": {"type": "string", "format": "email"},
                    "name": {"type": "string"},
                    "avatar_url": {"type": "string", "nullable": True},
                },
            },
            "CreateUser": {
                "type": "object",
                "required": ["email", "name"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "name": {"type": "string"},
                },
            },
            "ContactInfo": {
                "type": "object",
                "required": ["email"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "phone": {"type": "string", "nullable": True},
                },
            },
        }},
    }
    return yaml.dump(base, sort_keys=False, allow_unicode=True)


_BASE_USER_PATHS = {
    "/api/v2/users": {
        "get": {
            "summary": "List all users", "operationId": "list_users",
            "parameters": [
                {"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "default": 1}},
                {"name": "page_size", "in": "query", "required": False, "schema": {"type": "integer", "default": 20}},
            ],
            "responses": {"200": {"description": "List of users", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/User"}}}}}},
        },
        "post": {
            "summary": "Create a new user", "operationId": "create_user",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateUser"}}}},
            "responses": {"201": {"description": "User created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}},
        },
    },
    "/api/v2/users/{id}": {
        "get": {
            "summary": "Get a user by ID", "operationId": "get_user",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {
                "200": {"description": "User found", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}},
                "404": {"description": "Not found"},
            },
        },
    },
    "/api/v2/users/{id}/contact": {
        "get": {
            "summary": "Get user contact info", "operationId": "get_user_contact",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {
                "200": {"description": "Contact info", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ContactInfo"}}}},
                "404": {"description": "Not found"},
            },
        },
    },
}


def _build_scenarios() -> list[Scenario]:
    import yaml, copy

    def user_yaml(paths: dict, version: str, schemas: dict | None = None) -> str:
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "User Service", "version": version},
            "paths": paths,
            "components": {"schemas": schemas or {
                "User": {"type": "object", "required": ["id","email","name"],
                         "properties": {"id":{"type":"string"},"email":{"type":"string","format":"email"},"name":{"type":"string"},"avatar_url":{"type":"string","nullable":True}}},
                "CreateUser": {"type": "object", "required": ["email","name"],
                               "properties": {"email":{"type":"string","format":"email"},"name":{"type":"string"}}},
                "ContactInfo": {"type":"object","required":["email"],
                                "properties":{"email":{"type":"string","format":"email"},"phone":{"type":"string","nullable":True}}},
            }},
        }
        return yaml.dump(doc, sort_keys=False, allow_unicode=True)

    def order_yaml(paths: dict, version: str, schemas: dict | None = None) -> str:
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "Order Service", "version": version},
            "paths": paths,
            "components": {"schemas": schemas or {
                "Order": {"type":"object","required":["id","user_id","items","total","status"],
                          "properties":{"id":{"type":"string"},"user_id":{"type":"string"},"items":{"type":"array","items":{"type":"string"}},"total":{"type":"number"},"status":{"type":"string"}}},
                "CreateOrder": {"type":"object","required":["user_id","items","total"],
                                "properties":{"user_id":{"type":"string"},"items":{"type":"array","items":{"type":"string"}},"total":{"type":"number"}}},
            }},
        }
        return yaml.dump(doc, sort_keys=False, allow_unicode=True)

    p = copy.deepcopy(_BASE_USER_PATHS)

    # ── A-01: Remove GET /api/v2/users/{id}  (BREAKING) ───────────────────────
    p01 = copy.deepcopy(_BASE_USER_PATHS)
    del p01["/api/v2/users/{id}"]
    a01 = Scenario(
        id="A-01", repo="k11-user-service", branch="eval/a01-remove-users-by-id",
        title="eval(A-01): Remove GET /api/v2/users/{id}",
        body="Removes endpoint GET /api/v2/users/{id}. Consumers using this endpoint will break.",
        ground_truth="BREAKING",
        expected_consumers=["k11-order-service","k11-payment-service","k11-notification-svc"],
        openapi_content=user_yaml(p01, "2.0.0"),
        notes="Endpoint removal — most severe breaking change type",
    )

    # ── A-02: Add required field `phone` to CreateUser  (BREAKING) ───────────
    p02 = copy.deepcopy(_BASE_USER_PATHS)
    a02 = Scenario(
        id="A-02", repo="k11-user-service", branch="eval/a02-required-phone-field",
        title="eval(A-02): Add required field 'phone' to POST /api/v2/users",
        body="Makes phone a required field on user creation. Callers not sending phone will receive 422.",
        ground_truth="BREAKING",
        expected_consumers=["k11-order-service","k11-payment-service"],
        openapi_content=user_yaml(p02, "1.1.0", schemas={
            "User": {"type":"object","required":["id","email","name"],
                     "properties":{"id":{"type":"string"},"email":{"type":"string","format":"email"},"name":{"type":"string"},"avatar_url":{"type":"string","nullable":True}}},
            "CreateUser": {"type":"object","required":["email","name","phone"],
                           "properties":{"email":{"type":"string","format":"email"},"name":{"type":"string"},
                                         "phone":{"type":"string","description":"Required — must include country code"}}},
            "ContactInfo": {"type":"object","required":["email"],"properties":{"email":{"type":"string","format":"email"},"phone":{"type":"string","nullable":True}}},
        }),
        notes="Required field added to request body — breaks callers that create users",
    )

    # ── A-03: Rename `id` → `user_id` in User schema  (BREAKING) ─────────────
    p03 = copy.deepcopy(_BASE_USER_PATHS)
    a03 = Scenario(
        id="A-03", repo="k11-user-service", branch="eval/a03-rename-id-field",
        title="eval(A-03): Rename response field 'id' to 'user_id' in User schema",
        body="Renames the primary key field in User response. Consumers reading 'id' will get null/undefined.",
        ground_truth="BREAKING",
        expected_consumers=["k11-order-service","k11-payment-service","k11-notification-svc"],
        openapi_content=user_yaml(p03, "2.0.0", schemas={
            "User": {"type":"object","required":["user_id","email","name"],
                     "properties":{"user_id":{"type":"string","description":"Previously 'id'"},"email":{"type":"string","format":"email"},"name":{"type":"string"},"avatar_url":{"type":"string","nullable":True}}},
            "CreateUser": {"type":"object","required":["email","name"],"properties":{"email":{"type":"string","format":"email"},"name":{"type":"string"}}},
            "ContactInfo": {"type":"object","required":["email"],"properties":{"email":{"type":"string","format":"email"},"phone":{"type":"string","nullable":True}}},
        }),
        notes="Response field rename — silently breaks consumers expecting 'id'",
    )

    # ── A-04: Change response code 200 → 202 on GET /api/v2/users/{id}  (BREAKING) ──
    p04 = copy.deepcopy(_BASE_USER_PATHS)
    p04["/api/v2/users/{id}"]["get"]["responses"] = {
        "202": {"description": "User found (async)"},
        "404": {"description": "Not found"},
    }
    a04 = Scenario(
        id="A-04", repo="k11-user-service", branch="eval/a04-response-code-change",
        title="eval(A-04): Change GET /api/v2/users/{id} response 200 → 202",
        body="Success response code changed from 200 to 202. Consumers checking for 200 will treat the response as an error.",
        ground_truth="BREAKING",
        expected_consumers=["k11-order-service","k11-payment-service","k11-notification-svc"],
        openapi_content=user_yaml(p04, "1.1.0"),
        notes="Response code change — breaks strict status-code checkers",
    )

    # ── A-05: Remove `email` from required fields in User  (BREAKING) ─────────
    p05 = copy.deepcopy(_BASE_USER_PATHS)
    a05 = Scenario(
        id="A-05", repo="k11-user-service", branch="eval/a05-email-no-longer-required",
        title="eval(A-05): Remove 'email' from required response fields",
        body="Email is no longer guaranteed in user responses. Consumers relying on email for notifications/payments may get null.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service","k11-notification-svc"],
        openapi_content=user_yaml(p05, "1.1.0", schemas={
            "User": {"type":"object","required":["id","name"],
                     "properties":{"id":{"type":"string"},"email":{"type":"string","format":"email","nullable":True},"name":{"type":"string"},"avatar_url":{"type":"string","nullable":True}}},
            "CreateUser": {"type":"object","required":["name"],"properties":{"email":{"type":"string","format":"email"},"name":{"type":"string"}}},
            "ContactInfo": {"type":"object","required":["email"],"properties":{"email":{"type":"string","format":"email"},"phone":{"type":"string","nullable":True}}},
        }),
        notes="Required field removed from response — breaks consumers expecting guaranteed email",
    )

    # ── A-06: Remove GET /api/v1/orders/{id}  (BREAKING) ──────────────────────
    p06_paths = {
        "/api/v1/orders": {
            "get": {"summary":"List orders","operationId":"list_orders",
                    "parameters":[{"name":"page","in":"query","required":False,"schema":{"type":"integer","default":1}}],
                    "responses":{"200":{"description":"Orders"}}},
            "post": {"summary":"Create order","operationId":"create_order",
                     "requestBody":{"required":True,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/CreateOrder"}}}},
                     "responses":{"201":{"description":"Created"},"422":{"description":"Invalid"}}},
        },
        "/api/v1/orders/{id}": {
            "patch": {"summary":"Update status","operationId":"update_order_status",
                      "parameters":[{"name":"id","in":"path","required":True,"schema":{"type":"string"}},
                                    {"name":"status","in":"query","required":True,"schema":{"type":"string"}}],
                      "responses":{"200":{"description":"Updated"},"404":{"description":"Not found"}}},
        },
    }
    a06 = Scenario(
        id="A-06", repo="k11-order-service", branch="eval/a06-remove-get-order-by-id",
        title="eval(A-06): Remove GET /api/v1/orders/{id}",
        body="Removes order lookup by ID. payment-service uses this to validate orders before processing.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service"],
        openapi_content=order_yaml(p06_paths, "2.0.0"),
        notes="Tests breaking change on order-service — payment-service is its only consumer",
    )

    # ── A-07: Add required `currency` to CreateOrder  (BREAKING) ──────────────
    p07_paths = {
        "/api/v1/orders": {
            "get": {"summary":"List orders","operationId":"list_orders",
                    "parameters":[{"name":"page","in":"query","required":False,"schema":{"type":"integer","default":1}}],
                    "responses":{"200":{"description":"Orders"}}},
            "post": {"summary":"Create order","operationId":"create_order",
                     "requestBody":{"required":True,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/CreateOrder"}}}},
                     "responses":{"201":{"description":"Created"},"422":{"description":"Invalid"}}},
        },
        "/api/v1/orders/{id}": {
            "get": {"summary":"Get order","operationId":"get_order",
                    "parameters":[{"name":"id","in":"path","required":True,"schema":{"type":"string"}}],
                    "responses":{"200":{"description":"Found"},"404":{"description":"Not found"}}},
            "patch": {"summary":"Update status","operationId":"update_order_status",
                      "parameters":[{"name":"id","in":"path","required":True,"schema":{"type":"string"}},
                                    {"name":"status","in":"query","required":True,"schema":{"type":"string"}}],
                      "responses":{"200":{"description":"Updated"},"404":{"description":"Not found"}}},
        },
    }
    a07 = Scenario(
        id="A-07", repo="k11-order-service", branch="eval/a07-required-currency-field",
        title="eval(A-07): Add required 'currency' field to CreateOrder",
        body="currency is now required when creating orders. Callers not providing it will get 422.",
        ground_truth="BREAKING",
        expected_consumers=["k11-payment-service"],
        openapi_content=order_yaml(p07_paths, "1.1.0", schemas={
            "Order": {"type":"object","required":["id","user_id","items","total","status","currency"],
                      "properties":{"id":{"type":"string"},"user_id":{"type":"string"},"items":{"type":"array","items":{"type":"string"}},"total":{"type":"number"},"status":{"type":"string"},"currency":{"type":"string"}}},
            "CreateOrder": {"type":"object","required":["user_id","items","total","currency"],
                            "properties":{"user_id":{"type":"string"},"items":{"type":"array","items":{"type":"string"}},"total":{"type":"number"},"currency":{"type":"string","description":"Required — ISO 4217 code e.g. USD"}}},
        }),
        notes="Required field added to order creation — breaks callers not sending currency",
    )

    # ── A-08: Add optional `preferences_url` to User  (COMPATIBLE) ────────────
    p08 = copy.deepcopy(_BASE_USER_PATHS)
    a08 = Scenario(
        id="A-08", repo="k11-user-service", branch="eval/a08-add-optional-preferences-url",
        title="eval(A-08): Add optional 'preferences_url' field to User response",
        body="Purely additive — adds an optional preferences_url field. No existing consumers need to change.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content=user_yaml(p08, "1.1.0", schemas={
            "User": {"type":"object","required":["id","email","name"],
                     "properties":{"id":{"type":"string"},"email":{"type":"string","format":"email"},"name":{"type":"string"},"avatar_url":{"type":"string","nullable":True},"preferences_url":{"type":"string","nullable":True,"description":"New optional field"}}},
            "CreateUser": {"type":"object","required":["email","name"],"properties":{"email":{"type":"string","format":"email"},"name":{"type":"string"}}},
            "ContactInfo": {"type":"object","required":["email"],"properties":{"email":{"type":"string","format":"email"},"phone":{"type":"string","nullable":True}}},
        }),
        notes="Additive change — consumers ignore unknown fields",
    )

    # ── A-09: Add new endpoint GET /api/v2/users/{id}/preferences  (COMPATIBLE) ─
    p09 = copy.deepcopy(_BASE_USER_PATHS)
    p09["/api/v2/users/{id}/preferences"] = {
        "get": {
            "summary": "Get user preferences", "operationId": "get_user_preferences",
            "parameters": [{"name":"id","in":"path","required":True,"schema":{"type":"string"}}],
            "responses": {"200": {"description": "User preferences"}, "404": {"description": "Not found"}},
        },
    }
    a09 = Scenario(
        id="A-09", repo="k11-user-service", branch="eval/a09-add-preferences-endpoint",
        title="eval(A-09): Add new GET /api/v2/users/{id}/preferences endpoint",
        body="Purely additive — new endpoint, no existing endpoints changed.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content=user_yaml(p09, "1.1.0"),
        notes="New endpoint addition — no impact on existing consumers",
    )

    # ── A-10: Add optional `notes` field to Order  (COMPATIBLE) ───────────────
    a10 = Scenario(
        id="A-10", repo="k11-order-service", branch="eval/a10-add-optional-notes",
        title="eval(A-10): Add optional 'notes' field to Order response",
        body="Additive change — notes field added to Order schema. No existing fields changed.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content=order_yaml(p07_paths, "1.1.0", schemas={
            "Order": {"type":"object","required":["id","user_id","items","total","status"],
                      "properties":{"id":{"type":"string"},"user_id":{"type":"string"},"items":{"type":"array","items":{"type":"string"}},"total":{"type":"number"},"status":{"type":"string"},"notes":{"type":"string","nullable":True,"description":"Optional order notes"}}},
            "CreateOrder": {"type":"object","required":["user_id","items","total"],
                            "properties":{"user_id":{"type":"string"},"items":{"type":"array","items":{"type":"string"}},"total":{"type":"number"},"notes":{"type":"string"}}},
        }),
        notes="Additive field in response — backward compatible",
    )

    # ── A-11: Add new GET /api/v1/payments/{id}/receipt  (COMPATIBLE) ─────────
    a11 = Scenario(
        id="A-11", repo="k11-payment-service", branch="eval/a11-add-receipt-endpoint",
        title="eval(A-11): Add new GET /api/v1/payments/{id}/receipt endpoint",
        body="New endpoint for fetching payment receipts. Fully additive.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content="""openapi: 3.0.0
info:
  title: Payment Service
  version: 1.1.0
paths:
  /api/v1/payments:
    post:
      summary: Initiate a payment
      operationId: create_payment
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreatePayment'
      responses:
        '201': {description: Payment initiated}
        '422': {description: Invalid}
  /api/v1/payments/{id}:
    get:
      summary: Get a payment by ID
      operationId: get_payment
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        '200': {description: Payment found}
        '404': {description: Not found}
  /api/v1/payments/{id}/receipt:
    get:
      summary: Get a payment receipt (new)
      operationId: get_payment_receipt
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        '200': {description: Receipt found}
        '404': {description: Not found}
components:
  schemas:
    Payment:
      type: object
      required: [id, user_id, order_id, amount, currency, status]
      properties:
        id: {type: string}
        user_id: {type: string}
        order_id: {type: string}
        amount: {type: number}
        currency: {type: string}
        status: {type: string}
    CreatePayment:
      type: object
      required: [user_id, order_id, amount]
      properties:
        user_id: {type: string}
        order_id: {type: string}
        amount: {type: number}
        currency: {type: string, default: USD}
""",
        notes="New endpoint — no consumers of payment-service in graph, should be COMPATIBLE with 0 radius",
    )

    # ── A-12: Version bump only, no spec change  (COMPATIBLE) ─────────────────
    a12 = Scenario(
        id="A-12", repo="k11-user-service", branch="eval/a12-version-bump-only",
        title="eval(A-12): Version bump 1.0.0 → 1.0.1, no spec change",
        body="Patch release — only the info.version field changes. No endpoints or schemas modified.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content=user_yaml(copy.deepcopy(_BASE_USER_PATHS), "1.0.1"),
        notes="Version-only bump — diff should detect 0 breaking changes",
    )

    # ── A-13: Provider with no consumers (notification-svc removes endpoint)  (COMPATIBLE) ──
    a13 = Scenario(
        id="A-13", repo="k11-notification-svc", branch="eval/a13-remove-get-notification",
        title="eval(A-13): Remove GET /api/v1/notifications/{id} (no registered consumers)",
        body="Removes get-notification endpoint. notification-svc has no downstream consumers in the graph.",
        ground_truth="COMPATIBLE",
        expected_consumers=[],
        openapi_content="""openapi: 3.0.0
info:
  title: Notification Service
  version: 2.0.0
paths:
  /api/v1/notifications:
    post:
      summary: Send a notification
      operationId: send_notification
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/SendNotification'
      responses:
        '201': {description: Notification sent}
        '422': {description: Invalid}
components:
  schemas:
    Notification:
      type: object
      required: [id, user_id, channel, message, status]
      properties:
        id: {type: string}
        user_id: {type: string}
        channel: {type: string}
        message: {type: string}
        status: {type: string}
    SendNotification:
      type: object
      required: [user_id, channel, message]
      properties:
        user_id: {type: string}
        channel: {type: string}
        message: {type: string}
""",
        notes="Tests that pipeline returns 0 impact when provider has no registered consumers",
    )

    # ── A-14: Mixed — remove contact endpoint + add two new endpoints  (BREAKING) ─
    p14 = {
        "/api/v2/users": copy.deepcopy(_BASE_USER_PATHS["/api/v2/users"]),
        "/api/v2/users/{id}": copy.deepcopy(_BASE_USER_PATHS["/api/v2/users/{id}"]),
        "/api/v2/users/{id}/preferences": {
            "get": {"summary":"Get user preferences","operationId":"get_user_preferences",
                    "parameters":[{"name":"id","in":"path","required":True,"schema":{"type":"string"}}],
                    "responses":{"200":{"description":"Preferences"},"404":{"description":"Not found"}}},
        },
        "/api/v2/users/{id}/settings": {
            "get": {"summary":"Get user settings","operationId":"get_user_settings",
                    "parameters":[{"name":"id","in":"path","required":True,"schema":{"type":"string"}}],
                    "responses":{"200":{"description":"Settings"},"404":{"description":"Not found"}}},
        },
    }
    a14 = Scenario(
        id="A-14", repo="k11-user-service", branch="eval/a14-mixed-remove-and-add",
        title="eval(A-14): Remove /users/{id}/contact, add /preferences and /settings (mixed PR)",
        body="Breaking: removes contact endpoint (used by notification-svc). Also adds two new endpoints (non-breaking). Tests mixed diff handling.",
        ground_truth="BREAKING",
        expected_consumers=["k11-notification-svc"],
        openapi_content=user_yaml(p14, "2.0.0"),
        notes="Mixed PR — net breaking because contact endpoint removal affects notification-svc",
    )

    # ── A-15: HITL trigger — remove both /users/{id} and /users/{id}/contact  (BREAKING) ─
    p15 = {
        "/api/v2/users": copy.deepcopy(_BASE_USER_PATHS["/api/v2/users"]),
    }
    a15 = Scenario(
        id="A-15", repo="k11-user-service", branch="eval/a15-remove-two-endpoints-hitl",
        title="eval(A-15): Remove GET /users/{id} AND /users/{id}/contact (HITL trigger test)",
        body="Removes two endpoints simultaneously — affects all 3 consumers (order, payment, notification). Expected to trigger HITL gate.",
        ground_truth="BREAKING",
        expected_consumers=["k11-order-service","k11-payment-service","k11-notification-svc"],
        openapi_content=user_yaml(p15, "3.0.0"),
        notes="Two-endpoint removal — should trigger HITL (impact_radius=3, 2 breaking changes)",
    )

    return [a01, a02, a03, a04, a05, a06, a07, a08, a09, a10, a11, a12, a13, a14, a15]


# ── GitHub API helpers ─────────────────────────────────────────────────────────

async def gh_get(path: str, client: httpx.AsyncClient) -> dict:
    r = await client.get(f"https://api.github.com{path}",
                         headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                                  "Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28"})
    r.raise_for_status()
    return r.json()


async def gh_post(path: str, body: dict, client: httpx.AsyncClient) -> dict:
    r = await client.post(f"https://api.github.com{path}",
                          headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"},
                          json=body)
    r.raise_for_status()
    return r.json()


async def gh_put(path: str, body: dict, client: httpx.AsyncClient) -> dict:
    r = await client.put(f"https://api.github.com{path}",
                         headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                                  "Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28"},
                         json=body)
    r.raise_for_status()
    return r.json()


async def get_main_sha(repo: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """Returns (branch_tip_sha, openapi_file_sha)."""
    ref_data  = await gh_get(f"/repos/{ORG}/{repo}/git/ref/heads/main", client)
    tip_sha   = ref_data["object"]["sha"]
    file_data = await gh_get(f"/repos/{ORG}/{repo}/contents/openapi.yaml?ref=main", client)
    file_sha  = file_data["sha"]
    return tip_sha, file_sha


async def create_branch(repo: str, branch: str, from_sha: str, client: httpx.AsyncClient) -> None:
    try:
        await gh_post(f"/repos/{ORG}/{repo}/git/refs",
                      {"ref": f"refs/heads/{branch}", "sha": from_sha}, client)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            log.info("Branch %s already exists — reusing", branch)
        else:
            raise


async def update_file(repo: str, branch: str, content: str, file_sha: str, message: str,
                      client: httpx.AsyncClient) -> str:
    """Update openapi.yaml on branch. Returns the new commit SHA."""
    data = await gh_put(f"/repos/{ORG}/{repo}/contents/openapi.yaml", {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": file_sha,
        "branch": branch,
    }, client)
    return data["commit"]["sha"]


async def create_pr(repo: str, branch: str, title: str, body: str,
                    client: httpx.AsyncClient) -> tuple[int, str, str]:
    """Create PR. Returns (pr_number, head_sha, base_sha)."""
    try:
        data = await gh_post(f"/repos/{ORG}/{repo}/pulls", {
            "title": title, "body": body, "head": branch, "base": "main",
        }, client)
        return data["number"], data["head"]["sha"], data["base"]["sha"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            # PR already exists — find it
            prs = await gh_get(f"/repos/{ORG}/{repo}/pulls?head={ORG}:{branch}&state=open", client)
            if prs:
                pr = prs[0]
                return pr["number"], pr["head"]["sha"], pr["base"]["sha"]
        raise


# ── Webhook trigger ────────────────────────────────────────────────────────────

def trigger_webhook(repo: str, pr_number: int, head_sha: str, base_sha: str, base_ref: str) -> str:
    """POST a signed webhook payload. Returns run_id."""
    payload = json.dumps({
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "title": "eval PR",
            "body": "",
            "head": {"sha": head_sha, "ref": "eval-branch"},
            "base": {"sha": base_sha, "ref": base_ref},
        },
        "repository": {"full_name": f"{ORG}/{repo}"},
    }, separators=(',', ':')).encode('utf-8')

    sig = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()

    req = urllib.request.Request(
        WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "X-GitHub-Event": "pull_request",
                 "X-Hub-Signature-256": sig},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["run_id"]


def poll_run(run_id: str, timeout: int = 90) -> dict:
    """Poll /runs/{run_id} until complete."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        with urllib.request.urlopen(
            f"http://localhost:9001/runs/{run_id}", timeout=10
        ) as resp:
            run = json.loads(resp.read())
        if run["status"] != "running":
            return run
    return {"status": "timeout", "run_id": run_id, "summary": None}


# ── Baselines ──────────────────────────────────────────────────────────────────

def baseline_diff_only(scenario: Scenario, head_contract, base_contract) -> str:
    """B1: Return BREAKING if diff has any breaking changes, else COMPATIBLE."""
    from contracts.diff import diff_contracts
    if base_contract is None or head_contract is None:
        return "UNCERTAIN"
    diff = diff_contracts(base_contract, head_contract)
    if diff is None:
        return "COMPATIBLE"
    return "BREAKING" if diff.breaking else "COMPATIBLE"


async def baseline_graph_diff_no_llm(scenario: Scenario, head_contract, base_contract) -> str:
    """B2: Graph-aware diff without LLM — BREAKING if consumer has matching edge to changed endpoint."""
    import os
    from contracts.diff import diff_contracts
    from graph.graph_store import GraphStore
    from graph.consumer_finder import find_affected_consumers

    if base_contract is None or head_contract is None:
        return "UNCERTAIN"
    diff = diff_contracts(base_contract, head_contract)
    if diff is None or not diff.breaking:
        return "COMPATIBLE"

    changed_endpoints = [c.endpoint for c in diff.changes]
    provider = scenario.repo
    db = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
    async with GraphStore(db) as store:
        graph = await store.load_graph()
    consumers = find_affected_consumers(graph, provider, changed_endpoints)
    return "BREAKING" if any(c.consuming_endpoints for c in consumers) else "COMPATIBLE"


# ── Topology extension ─────────────────────────────────────────────────────────

async def extend_topology():
    """Add notification-svc → order-service transitive edge."""
    import os
    from graph.dependency_graph import ConsumptionEdge
    from graph.graph_store import GraphStore
    db = os.getenv("GRAPH_STORE_DB", "dependency_graph.db")
    async with GraphStore(db) as store:
        await store.record_consumption(ConsumptionEdge(
            consumer="k11-notification-svc",
            provider="k11-order-service",
            endpoint_pattern="/api/v1/orders/{id}",
            methods=["GET"],
            criticality="medium",
        ))
    log.info("Extended topology: k11-notification-svc → k11-order-service")


# ── Main eval loop ─────────────────────────────────────────────────────────────

async def get_branch_file_sha(repo: str, branch: str, client: httpx.AsyncClient) -> str | None:
    """Get current SHA of openapi.yaml on the given branch (None if not found)."""
    try:
        r = await client.get(
            f"https://api.github.com/repos/{ORG}/{repo}/contents/openapi.yaml",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            params={"ref": branch},
        )
        r.raise_for_status()
        return r.json()["sha"]
    except Exception:
        return None


async def run_scenario(scenario: Scenario, client: httpx.AsyncClient) -> dict:
    log.info("=" * 60)
    log.info("Running scenario %s — %s", scenario.id, scenario.title)

    # 1. Get main branch state
    tip_sha, main_file_sha = await get_main_sha(scenario.repo, client)

    # 2. Create branch (reuses if exists)
    await create_branch(scenario.repo, scenario.branch, tip_sha, client)

    # 3. Get the file SHA from the eval branch itself (handles re-runs where branch was
    #    previously modified — GitHub rejects updates if SHA doesn't match current state)
    branch_file_sha = await get_branch_file_sha(scenario.repo, scenario.branch, client)
    file_sha = branch_file_sha if branch_file_sha else main_file_sha

    # 4. Push modified spec
    commit_sha = await update_file(
        scenario.repo, scenario.branch,
        scenario.openapi_content, file_sha,
        f"{scenario.id}: {scenario.title}",
        client,
    )
    log.info("%s — pushed %s (head=%s)", scenario.id, scenario.branch, commit_sha[:8])

    # 5. Create PR — always use commit_sha as head_sha; the PR API may return a
    #    stale head SHA on re-runs because it caches the previous push.
    pr_number, _pr_head_sha, base_sha = await create_pr(
        scenario.repo, scenario.branch, scenario.title, scenario.body, client
    )
    head_sha = commit_sha   # use the commit we just pushed
    log.info("%s — PR #%d (head=%s base=%s)", scenario.id, pr_number, head_sha[:8], base_sha[:8])

    # 6. Fetch both specs for baseline comparison
    from analyzer.contract_extractor import extract_contract
    import base64 as _b64

    async def fetch_spec(sha: str):
        try:
            r = await client.get(
                f"https://api.github.com/repos/{ORG}/{scenario.repo}/contents/openapi.yaml",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}","Accept":"application/vnd.github+json"},
                params={"ref": sha},
            )
            r.raise_for_status()
            raw = _b64.b64decode(r.json()["content"]).decode()
            return extract_contract("openapi.yaml", raw, scenario.repo, f"{ORG}/{scenario.repo}", sha)
        except Exception as e:
            log.warning("Could not fetch spec at %s: %s", sha[:8], e)
            return None

    head_contract, base_contract = await asyncio.gather(
        fetch_spec(head_sha), fetch_spec(base_sha)
    )

    # 7. Baseline verdicts (no I/O — computed locally)
    b1 = baseline_diff_only(scenario, head_contract, base_contract)
    b2 = await baseline_graph_diff_no_llm(scenario, head_contract, base_contract)
    log.info("%s — B1(diff-only)=%s  B2(graph+diff)=%s", scenario.id, b1, b2)

    # 8. Trigger full pipeline
    t_start = time.time()
    run_id = trigger_webhook(scenario.repo, pr_number, head_sha, base_sha, "main")
    result = poll_run(run_id)
    elapsed = round(time.time() - t_start, 1)

    summary = result.get("summary") or {}
    if result["status"] == "timeout":
        verdict = "TIMEOUT"
    elif result["status"] == "completed" and not summary:
        # Pipeline short-circuited at Phase 1 — no contract changes detected
        verdict = "COMPATIBLE"
    else:
        verdict = summary.get("overall_verdict", "ERROR")
    breaking_consumers = summary.get("breaking_consumers", 0)
    uncertain_consumers = summary.get("uncertain_consumers", 0)
    impact_score = summary.get("impact_score", 0.0)
    hitl = summary.get("hitl_required", False) if summary else False

    correct = verdict == scenario.ground_truth
    log.info(
        "%s — verdict=%s ground_truth=%s correct=%s  consumers(breaking=%d uncertain=%d)  score=%.3f  time=%.1fs",
        scenario.id, verdict, scenario.ground_truth, correct,
        breaking_consumers, uncertain_consumers, impact_score, elapsed,
    )

    return {
        "scenario_id":        scenario.id,
        "repo":               scenario.repo,
        "ground_truth":       scenario.ground_truth,
        "expected_consumers": scenario.expected_consumers,
        "notes":              scenario.notes,
        # Full pipeline (B3)
        "verdict":            verdict,
        "breaking_consumers": breaking_consumers,
        "uncertain_consumers":uncertain_consumers,
        "compatible_consumers": summary.get("compatible_consumers", 0),
        "impact_score":       impact_score,
        "impact_radius":      summary.get("impact_radius", 0),
        "breaking_changes":   summary.get("breaking_changes", 0),
        "hitl_triggered":     hitl,
        "elapsed_s":          elapsed,
        "run_id":             run_id,
        "correct":            correct,
        # Baselines
        "b1_diff_only":       b1,
        "b2_graph_no_llm":    b2,
        "b1_correct":         b1 == scenario.ground_truth,
        "b2_correct":         b2 == scenario.ground_truth,
    }


def compute_metrics(results: list[dict]) -> dict:
    def metrics_for(verdicts_correct: list[tuple[str, str]]) -> dict:
        tp = sum(1 for v, gt in verdicts_correct if v == "BREAKING" and gt == "BREAKING")
        fp = sum(1 for v, gt in verdicts_correct if v == "BREAKING" and gt == "COMPATIBLE")
        tn = sum(1 for v, gt in verdicts_correct if v == "COMPATIBLE" and gt == "COMPATIBLE")
        fn = sum(1 for v, gt in verdicts_correct if v == "COMPATIBLE" and gt == "BREAKING")
        uncertain = sum(1 for v, _ in verdicts_correct if v == "UNCERTAIN")
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec  = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        acc  = (tp + tn) / len(verdicts_correct) if verdicts_correct else 0
        return {"precision": round(prec,3), "recall": round(rec,3), "f1": round(f1,3),
                "accuracy": round(acc,3), "tp": tp, "fp": fp, "tn": tn, "fn": fn,
                "uncertain": uncertain}

    b3_pairs = [(r["verdict"], r["ground_truth"]) for r in results]
    b1_pairs = [(r["b1_diff_only"], r["ground_truth"]) for r in results]
    b2_pairs = [(r["b2_graph_no_llm"], r["ground_truth"]) for r in results]

    breaking_results = [r for r in results if r["ground_truth"] == "BREAKING"]
    compatible_results = [r for r in results if r["ground_truth"] == "COMPATIBLE"]

    hitl_triggered  = sum(1 for r in results if r["hitl_triggered"])
    hitl_breaking   = sum(1 for r in results if r["hitl_triggered"] and r["ground_truth"] == "BREAKING")
    hitl_compatible = sum(1 for r in results if r["hitl_triggered"] and r["ground_truth"] == "COMPATIBLE")

    latencies = [r["elapsed_s"] for r in results if isinstance(r["elapsed_s"], (int, float))]

    return {
        "total_scenarios":  len(results),
        "breaking_scenarios": len(breaking_results),
        "compatible_scenarios": len(compatible_results),
        "rq1_full_pipeline": metrics_for(b3_pairs),
        "rq1_b1_diff_only":  metrics_for(b1_pairs),
        "rq1_b2_graph_no_llm": metrics_for(b2_pairs),
        "rq4_hitl": {
            "triggered": hitl_triggered,
            "true_escalations": hitl_breaking,
            "false_escalations": hitl_compatible,
            "missed_escalations": len(breaking_results) - hitl_breaking,
        },
        "rq5_latency": {
            "mean_s": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "min_s": min(latencies) if latencies else 0,
            "max_s": max(latencies) if latencies else 0,
        },
    }


async def main():
    if not GITHUB_TOKEN:
        sys.exit("GITHUB_TOKEN not set — check .env")

    log.info("Extending dependency graph topology...")
    await extend_topology()

    scenarios = _build_scenarios()
    results = []
    RESULTS_PATH.parent.mkdir(exist_ok=True)

    async with httpx.AsyncClient(timeout=30) as client:
        for scenario in scenarios:
            try:
                result = await run_scenario(scenario, client)
                results.append(result)
                # Save after each run so progress is not lost
                RESULTS_PATH.write_text(json.dumps({"results": results}, indent=2))
                log.info("Saved %d/%d results", len(results), len(scenarios))
            except Exception as exc:
                log.error("Scenario %s failed: %s", scenario.id, exc, exc_info=True)
                results.append({
                    "scenario_id": scenario.id, "repo": scenario.repo,
                    "ground_truth": scenario.ground_truth, "verdict": "ERROR",
                    "correct": False, "error": str(exc),
                })

    metrics = compute_metrics(results)
    output = {"results": results, "metrics": metrics}
    RESULTS_PATH.write_text(json.dumps(output, indent=2))

    log.info("\n%s", "=" * 60)
    log.info("PHASE A EVALUATION COMPLETE")
    log.info("=" * 60)
    log.info("Scenarios: %d", metrics["total_scenarios"])
    m = metrics["rq1_full_pipeline"]
    log.info("Full pipeline  — P=%.1f%% R=%.1f%% F1=%.1f%% Acc=%.1f%%",
             m["precision"]*100, m["recall"]*100, m["f1"]*100, m["accuracy"]*100)
    m1 = metrics["rq1_b1_diff_only"]
    log.info("Diff-only (B1) — P=%.1f%% R=%.1f%% F1=%.1f%% Acc=%.1f%%",
             m1["precision"]*100, m1["recall"]*100, m1["f1"]*100, m1["accuracy"]*100)
    m2 = metrics["rq1_b2_graph_no_llm"]
    log.info("Graph+diff B2  — P=%.1f%% R=%.1f%% F1=%.1f%% Acc=%.1f%%",
             m2["precision"]*100, m2["recall"]*100, m2["f1"]*100, m2["accuracy"]*100)
    h = metrics["rq4_hitl"]
    log.info("HITL: %d triggered (%d true, %d false, %d missed)",
             h["triggered"], h["true_escalations"], h["false_escalations"], h["missed_escalations"])
    lat = metrics["rq5_latency"]
    log.info("Latency: mean=%.1fs  min=%.1fs  max=%.1fs", lat["mean_s"], lat["min_s"], lat["max_s"])
    log.info("Results saved to %s", RESULTS_PATH)


if __name__ == "__main__":
    asyncio.run(main())
