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
"""Unit tests for the DependencyGraph."""
import pytest

from graph.dependency_graph import ConsumptionEdge, DependencyGraph, ServiceNode


@pytest.fixture
def sample_graph() -> DependencyGraph:
    g = DependencyGraph()
    for svc in ("user-service", "order-service", "payment-service",
                "notification-svc", "analytics-svc"):
        g.add_service(ServiceNode(name=svc, repo=f"org/{svc}"))

    g.add_consumption(ConsumptionEdge(
        "order-service", "user-service", "/api/v2/users/{id}", ["GET"], "high"))
    g.add_consumption(ConsumptionEdge(
        "payment-service", "user-service", "/api/v2/users/{id}", ["GET"], "critical"))
    g.add_consumption(ConsumptionEdge(
        "payment-service", "order-service", "/api/v1/orders/{id}", ["GET"], "critical"))
    g.add_consumption(ConsumptionEdge(
        "notification-svc", "user-service", "/api/v2/users/{id}/contact", ["GET"], "medium"))
    g.add_consumption(ConsumptionEdge(
        "analytics-svc", "order-service", "/api/v1/orders", ["GET"], "low"))
    return g


def test_direct_consumers(sample_graph):
    direct = set(sample_graph.direct_consumers("user-service"))
    assert "order-service" in direct
    assert "payment-service" in direct
    assert "notification-svc" in direct


def test_downstream_consumers(sample_graph):
    # analytics-svc consumes order-service, order-service consumes user-service
    # So analytics-svc is a transitive downstream consumer of user-service
    all_down = set(sample_graph.downstream_consumers("user-service"))
    assert "order-service" in all_down
    assert "payment-service" in all_down
    assert "notification-svc" in all_down
    assert "analytics-svc" in all_down


def test_downstream_consumers_isolated_service(sample_graph):
    # analytics-svc has no consumers
    assert sample_graph.downstream_consumers("analytics-svc") == []


def test_impact_radius(sample_graph):
    radius = sample_graph.impact_radius("user-service")
    assert radius == 4  # order, payment, notification, analytics (transitive)


def test_impact_score_no_consumers(sample_graph):
    score = sample_graph.impact_score("analytics-svc", ["/api/v1/reports"])
    assert score == 0.0


def test_impact_score_with_consumers(sample_graph):
    score = sample_graph.impact_score("user-service", ["/api/v2/users/{id}"])
    assert 0.0 < score <= 1.0


def test_impact_score_critical_consumers_higher(sample_graph):
    # user-service has critical consumers → score should be higher than order-service
    user_score  = sample_graph.impact_score("user-service",  ["/api/v2/users/{id}"])
    order_score = sample_graph.impact_score("order-service", ["/api/v1/orders"])
    assert user_score >= order_score


def test_serialise_deserialise(sample_graph):
    data = sample_graph.to_dict()
    restored = DependencyGraph.from_dict(data)
    assert set(restored.direct_consumers("user-service")) == \
           set(sample_graph.direct_consumers("user-service"))
    assert restored.impact_radius("user-service") == sample_graph.impact_radius("user-service")


def test_unknown_provider(sample_graph):
    assert sample_graph.downstream_consumers("nonexistent-service") == []
    assert sample_graph.impact_radius("nonexistent-service") == 0
    assert sample_graph.impact_score("nonexistent-service", ["/api/x"]) == 0.0
