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
agents/base.py
───────────────
Base class for all microservice QA agents.
Mirrors the Paper 1 pattern: run() wraps execute() with timeout + retry.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class MCPError(Exception):
    """Raised when an MCP tool call fails."""


@dataclass
class AgentResult:
    """Uniform result container returned by every microservice QA agent."""
    agent:       str
    task_type:   str
    success:     bool               = True
    data:        dict               = field(default_factory=dict)
    errors:      list[str]          = field(default_factory=list)
    duration_s:  float              = 0.0

    def to_dict(self) -> dict:
        return {
            "agent":      self.agent,
            "task_type":  self.task_type,
            "success":    self.success,
            "data":       self.data,
            "errors":     self.errors,
            "duration_s": round(self.duration_s, 3),
        }


class BaseMicroserviceAgent(ABC):
    """
    Abstract base for all microservice pipeline agents.

    Sub-classes implement ``execute(state) → dict`` (partial state update).
    ``run()`` wraps execute with:
      - asyncio timeout (TIMEOUT seconds)
      - tenacity retry on MCPError (RETRIES attempts, exponential back-off)
    """

    NAME:    str = "base_microservice_agent"
    TIMEOUT: int = 300
    RETRIES: int = 3

    def __init__(self, mcp_clients: dict[str, Any] | None = None) -> None:
        self._mcp  = mcp_clients or {}
        self._log  = logging.getLogger(f"agents.{self.NAME}")

    async def run(self, state: dict) -> dict:
        """Execute with timeout + retry; always returns a partial state dict."""
        t0 = time.monotonic()

        @retry(
            stop=stop_after_attempt(self.RETRIES),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(MCPError),
            reraise=True,
        )
        async def _attempt() -> dict:
            return await asyncio.wait_for(self.execute(state), timeout=self.TIMEOUT)

        try:
            result = await _attempt()
        except asyncio.TimeoutError:
            self._log.error("Agent %s timed out after %ds", self.NAME, self.TIMEOUT)
            result = {"errors": [f"{self.NAME} timed out after {self.TIMEOUT}s"]}
        except Exception as exc:
            self._log.exception("Agent %s failed", self.NAME)
            result = {"errors": [f"{self.NAME} failed: {exc}"]}

        elapsed = time.monotonic() - t0
        self._log.info("Agent %s completed in %.2fs", self.NAME, elapsed)
        return result

    @abstractmethod
    async def execute(self, state: dict) -> dict:
        """Agent-specific logic. Returns a partial state update dict."""

    async def call_mcp(self, server: str, tool: str, args: dict) -> dict:
        """Call a named MCP server tool. Raises MCPError on failure."""
        client = self._mcp.get(server)
        if client is None:
            raise MCPError(f"MCP server '{server}' not registered")
        try:
            return await client.call_tool(tool, args)
        except Exception as exc:
            raise MCPError(f"{server}.{tool} failed: {exc}") from exc
