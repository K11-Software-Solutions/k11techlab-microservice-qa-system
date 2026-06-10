# -*- coding: utf-8 -*-
"""
Unit tests for Feature 2 — HTTP Method Filtering in Consumer Matching.

Verifies that find_affected_consumers only includes consumers whose registered
edge methods overlap with the HTTP methods that actually changed.
"""
import pytest
from graph.dependency_graph import ConsumptionEdge, DependencyGraph, ServiceNode
from graph.consumer_finder import find_affected_consumers, _normalise_specs, serialise_consumers


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _build_graph() -> DependencyGraph:
    """
    provider exposes /api/v2/users/{id}.
    consumer_get  → provider  via GET
    consumer_post → provider  via POST
    consumer_any  → provider  via GET + POST
    consumer_put  → provider  via PUT
    """
    g = DependencyGraph()
    for svc in ["provider", "consumer_get", "consumer_post", "consumer_any", "consumer_put"]:
        g.add_service(ServiceNode(name=svc, repo=f"org/{svc}"))

    g.add_consumption(ConsumptionEdge(
        consumer="consumer_get",  provider="provider",
        endpoint_pattern="/api/v2/users/{id}", methods=["GET"], criticality="high",
    ))
    g.add_consumption(ConsumptionEdge(
        consumer="consumer_post", provider="provider",
        endpoint_pattern="/api/v2/users/{id}", methods=["POST"], criticality="medium",
    ))
    g.add_consumption(ConsumptionEdge(
        consumer="consumer_any",  provider="provider",
        endpoint_pattern="/api/v2/users/{id}", methods=["GET", "POST"], criticality="low",
    ))
    g.add_consumption(ConsumptionEdge(
        consumer="consumer_put",  provider="provider",
        endpoint_pattern="/api/v2/users/{id}", methods=["PUT"], criticality="low",
    ))
    return g


# ── Method filtering ──────────────────────────────────────────────────────────

class TestMethodFiltering:

    def test_get_change_includes_get_and_any_consumers(self):
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}],
        )
        names = {c.consumer for c in result}
        assert "consumer_get" in names
        assert "consumer_any" in names

    def test_get_change_excludes_post_only_consumer(self):
        """The ablation false positive: POST consumer must not fire on GET change."""
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}],
        )
        names = {c.consumer for c in result}
        assert "consumer_post" not in names
        assert "consumer_put"  not in names

    def test_post_change_includes_post_and_any_consumers(self):
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["POST"]}],
        )
        names = {c.consumer for c in result}
        assert "consumer_post" in names
        assert "consumer_any"  in names
        assert "consumer_get"  not in names

    def test_put_change_only_includes_put_consumer(self):
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["PUT"]}],
        )
        names = {c.consumer for c in result}
        assert "consumer_put" in names
        assert len(names) == 1

    def test_unknown_method_matches_nothing(self):
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["DELETE"]}],
        )
        assert result == []

    def test_multiple_changed_methods_union(self):
        """GET+POST change should affect all except PUT-only consumer."""
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["GET", "POST"]}],
        )
        names = {c.consumer for c in result}
        assert "consumer_get"  in names
        assert "consumer_post" in names
        assert "consumer_any"  in names
        assert "consumer_put"  not in names


# ── Backward compatibility ────────────────────────────────────────────────────

class TestBackwardCompatibility:

    def test_empty_methods_matches_all_consumers(self):
        """methods=[] means match all — preserves old behaviour."""
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": []}],
        )
        assert len(result) == 4

    def test_flat_string_without_method_prefix_matches_all(self):
        """Plain path string (no method prefix) matches all consumers."""
        g = _build_graph()
        result = find_affected_consumers(g, "provider", ["/api/v2/users/{id}"])
        assert len(result) == 4

    def test_flat_string_with_method_prefix_filters(self):
        """'GET /api/v2/users/{id}' parsed as method=GET."""
        g = _build_graph()
        result = find_affected_consumers(g, "provider", ["GET /api/v2/users/{id}"])
        names = {c.consumer for c in result}
        assert "consumer_get" in names
        assert "consumer_post" not in names

    def test_legacy_phase1_dict_form(self):
        """{"endpoint": "...", "method": "GET"} from Phase 1 state."""
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"endpoint": "/api/v2/users/{id}", "method": "GET"}],
        )
        names = {c.consumer for c in result}
        assert "consumer_get" in names
        assert "consumer_post" not in names

    def test_mixed_input_list(self):
        """Mix of flat strings and dicts in one call."""
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [
                "GET /api/v2/users/{id}",
                {"pattern": "/api/v2/users/{id}", "methods": ["POST"]},
            ],
        )
        names = {c.consumer for c in result}
        assert "consumer_get"  in names
        assert "consumer_post" in names
        assert "consumer_any"  in names
        assert "consumer_put"  not in names


# ── _normalise_specs ──────────────────────────────────────────────────────────

class TestNormaliseSpecs:

    def test_prefixed_string(self):
        specs = _normalise_specs(["GET /api/v2/users/{id}"])
        assert specs[0]["pattern"] == "/api/v2/users/{id}"
        assert specs[0]["methods"] == {"GET"}

    def test_plain_path_string(self):
        specs = _normalise_specs(["/api/v2/users/{id}"])
        assert specs[0]["pattern"] == "/api/v2/users/{id}"
        assert specs[0]["methods"] == set()

    def test_structured_dict(self):
        specs = _normalise_specs([{"pattern": "/api/v1/orders/{id}", "methods": ["GET", "POST"]}])
        assert specs[0]["methods"] == {"GET", "POST"}

    def test_legacy_dict(self):
        specs = _normalise_specs([{"endpoint": "/api/v1/orders/{id}", "method": "DELETE"}])
        assert specs[0]["pattern"] == "/api/v1/orders/{id}"
        assert specs[0]["methods"] == {"DELETE"}

    def test_methods_uppercased(self):
        specs = _normalise_specs([{"pattern": "/x", "methods": ["get", "post"]}])
        assert specs[0]["methods"] == {"GET", "POST"}


# ── Edge metadata propagation ─────────────────────────────────────────────────

class TestEdgeMetadata:

    def test_edge_methods_propagated(self):
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}],
        )
        get_ctx = next(c for c in result if c.consumer == "consumer_get")
        assert "GET" in get_ctx.edge_methods

    def test_criticality_propagated(self):
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}],
        )
        get_ctx = next(c for c in result if c.consumer == "consumer_get")
        assert get_ctx.edge_criticality == "high"

    def test_serialise_includes_all_fields(self):
        g = _build_graph()
        result = find_affected_consumers(
            g, "provider",
            [{"pattern": "/api/v2/users/{id}", "methods": ["GET"]}],
        )
        serialised = serialise_consumers(result)
        assert all("consumer" in r for r in serialised)
        assert all("edge_methods" in r for r in serialised)
        assert all("edge_criticality" in r for r in serialised)
