# -*- coding: utf-8 -*-
"""
Unit tests for Feature 5 — Transitive Consumer Validation.

Graph used throughout (unless a test builds its own):

    D ──────────────────────→ A   (direct, GET, medium)
    B ─────────────────────→ A   (direct, POST, high)
    C ──→ B ──→ A                (transitive depth-2, C→B: GET/critical, B→A: POST/high)
    E ──→ C ──→ B ──→ A          (transitive depth-3, added when include_e=True)

Provider under test is always A.
Changed endpoints: ["/api/v1/users"]
"""
import os
import pytest

from graph.dependency_graph import ConsumptionEdge, DependencyGraph, ServiceNode
from graph.consumer_finder import (
    ConsumerContext,
    find_affected_consumers,
    serialise_consumers,
    _resolve_consumer_context,
    _normalise_specs,
    TRANSITIVE_VALIDATION_ENABLED,
    MAX_TRANSITIVE_DEPTH,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _build_graph(include_e: bool = False) -> DependencyGraph:
    g = DependencyGraph()
    for name, repo in [
        ("A", "org/a-service"),
        ("B", "org/b-service"),
        ("C", "org/c-service"),
        ("D", "org/d-service"),
    ]:
        g.add_service(ServiceNode(name=name, repo=repo, team=f"team-{name.lower()}"))

    # Direct consumers of A
    g.add_consumption(ConsumptionEdge(
        consumer="D", provider="A",
        endpoint_pattern="/api/v1/users", methods=["GET"], criticality="medium",
    ))
    g.add_consumption(ConsumptionEdge(
        consumer="B", provider="A",
        endpoint_pattern="/api/v1/users", methods=["POST"], criticality="high",
    ))
    # C is transitive: C → B → A
    g.add_consumption(ConsumptionEdge(
        consumer="C", provider="B",
        endpoint_pattern="/api/v1/orders", methods=["GET"], criticality="critical",
    ))

    if include_e:
        g.add_service(ServiceNode(name="E", repo="org/e-service"))
        g.add_consumption(ConsumptionEdge(
            consumer="E", provider="C",
            endpoint_pattern="/api/v1/reports", methods=["DELETE"], criticality="low",
        ))

    return g


CHANGED = ["/api/v1/users"]


# ── shortest_path_edges ───────────────────────────────────────────────────────

class TestShortestPathEdges:

    def test_direct_edge_returns_one_item(self):
        g = _build_graph()
        edges = g.shortest_path_edges("D", "A")
        assert len(edges) == 1
        assert edges[0]["endpoint_pattern"] == "/api/v1/users"

    def test_two_hop_returns_two_edges(self):
        g = _build_graph()
        edges = g.shortest_path_edges("C", "A")
        assert len(edges) == 2

    def test_three_hop_returns_three_edges(self):
        g = _build_graph(include_e=True)
        edges = g.shortest_path_edges("E", "A")
        assert len(edges) == 3

    def test_no_path_returns_empty(self):
        g = _build_graph()
        edges = g.shortest_path_edges("A", "D")   # reverse direction — no path
        assert edges == []

    def test_missing_node_returns_empty(self):
        g = _build_graph()
        assert g.shortest_path_edges("Z", "A") == []

    def test_same_source_and_target_returns_empty(self):
        g = _build_graph()
        assert g.shortest_path_edges("A", "A") == []


# ── Direct consumer — hop_depth = 1 ──────────────────────────────────────────

class TestDirectConsumer:

    def test_direct_consumer_hop_depth_1(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        d_ctx = next(c for c in consumers if c.consumer == "D")
        assert d_ctx.hop_depth == 1
        assert d_ctx.is_direct is True

    def test_direct_consumer_included(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        names = {c.consumer for c in consumers}
        assert "D" in names
        assert "B" in names


# ── Transitive consumer — depth 2 ────────────────────────────────────────────

class TestTransitiveDepth2:

    def test_two_hop_consumer_hop_depth_2(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        c_ctx = next((c for c in consumers if c.consumer == "C"), None)
        assert c_ctx is not None, "C should be included as a transitive consumer"
        assert c_ctx.hop_depth == 2

    def test_transitive_consumer_is_not_direct(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        c_ctx = next(c for c in consumers if c.consumer == "C")
        assert c_ctx.is_direct is False

    def test_transitive_criticality_is_path_max(self):
        """C→B is 'critical', B→A is 'high' — max should be 'critical'."""
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        c_ctx = next(c for c in consumers if c.consumer == "C")
        assert c_ctx.edge_criticality == "critical"

    def test_transitive_methods_union(self):
        """C→B: GET, B→A: POST — union = ['GET', 'POST']."""
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        c_ctx = next(c for c in consumers if c.consumer == "C")
        assert set(c_ctx.edge_methods) == {"GET", "POST"}


# ── Transitive consumer — depth 3 ────────────────────────────────────────────

class TestTransitiveDepth3:

    def test_three_hop_consumer_depth_3(self):
        g = _build_graph(include_e=True)
        consumers = find_affected_consumers(g, "A", CHANGED)
        e_ctx = next((c for c in consumers if c.consumer == "E"), None)
        assert e_ctx is not None
        assert e_ctx.hop_depth == 3

    def test_three_hop_criticality_max(self):
        """E→C: low, C→B: critical, B→A: high — max is critical."""
        g = _build_graph(include_e=True)
        consumers = find_affected_consumers(g, "A", CHANGED)
        e_ctx = next(c for c in consumers if c.consumer == "E")
        assert e_ctx.edge_criticality == "critical"

    def test_three_hop_methods_union(self):
        """E→C: DELETE, C→B: GET, B→A: POST — union = {DELETE, GET, POST}."""
        g = _build_graph(include_e=True)
        consumers = find_affected_consumers(g, "A", CHANGED)
        e_ctx = next(c for c in consumers if c.consumer == "E")
        assert set(e_ctx.edge_methods) == {"DELETE", "GET", "POST"}


# ── Depth cap ─────────────────────────────────────────────────────────────────

class TestDepthCap:

    def test_beyond_max_depth_excluded(self, monkeypatch):
        """A consumer at depth > MAX_TRANSITIVE_DEPTH is skipped."""
        monkeypatch.setattr(
            "graph.consumer_finder.MAX_TRANSITIVE_DEPTH", 2
        )
        g = _build_graph(include_e=True)
        consumers = find_affected_consumers(g, "A", CHANGED)
        names = {c.consumer for c in consumers}
        # E is at depth 3 > 2 — must be excluded
        assert "E" not in names
        # C is at depth 2 == max — must be included
        assert "C" in names

    def test_at_max_depth_included(self, monkeypatch):
        monkeypatch.setattr(
            "graph.consumer_finder.MAX_TRANSITIVE_DEPTH", 3
        )
        g = _build_graph(include_e=True)
        consumers = find_affected_consumers(g, "A", CHANGED)
        names = {c.consumer for c in consumers}
        assert "E" in names


# ── Feature flag ──────────────────────────────────────────────────────────────

class TestTransitiveFlag:

    def test_transitive_disabled_excludes_indirect(self, monkeypatch):
        monkeypatch.setattr(
            "graph.consumer_finder.TRANSITIVE_VALIDATION_ENABLED", False
        )
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        names = {c.consumer for c in consumers}
        # Direct consumers still present
        assert "D" in names
        assert "B" in names
        # Transitive consumer excluded
        assert "C" not in names

    def test_transitive_enabled_includes_indirect(self, monkeypatch):
        monkeypatch.setattr(
            "graph.consumer_finder.TRANSITIVE_VALIDATION_ENABLED", True
        )
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        names = {c.consumer for c in consumers}
        assert "C" in names


# ── Sort order ────────────────────────────────────────────────────────────────

class TestSortOrder:

    def test_sort_order_direct_before_transitive(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        direct_indices     = [i for i, c in enumerate(consumers) if c.is_direct]
        transitive_indices = [i for i, c in enumerate(consumers) if not c.is_direct]
        assert max(direct_indices) < min(transitive_indices), (
            "All direct consumers should appear before any transitive consumer"
        )


# ── Serialisation ─────────────────────────────────────────────────────────────

class TestSerialise:

    def test_serialise_includes_hop_depth(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        serialised = serialise_consumers(consumers)
        for item in serialised:
            assert "hop_depth" in item, f"hop_depth missing for consumer {item.get('consumer')}"

    def test_serialise_direct_hop_depth_is_1(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        serialised = serialise_consumers(consumers)
        d_item = next(s for s in serialised if s["consumer"] == "D")
        assert d_item["hop_depth"] == 1

    def test_serialise_transitive_hop_depth_is_2(self):
        g = _build_graph()
        consumers = find_affected_consumers(g, "A", CHANGED)
        serialised = serialise_consumers(consumers)
        c_item = next(s for s in serialised if s["consumer"] == "C")
        assert c_item["hop_depth"] == 2
