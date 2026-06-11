# -*- coding: utf-8 -*-
"""
Unit tests for Feature 9 — Uncertainty Source Classification.

Covers:
  - _parse_classification(): DATA/SCOPE extraction, malformed response fallback
  - classify_uncertainty(): LLM call + parse, error → UNCLASSIFIED
  - classify_all_low_confidence(): threshold filtering, concurrency, exception handling
  - UncertaintyClassification.to_dict(): keys, values
  - Store: record_uncertainty_classifications() and get_uncertainty_source_stats()
"""
import pytest
import pytest_asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from pipeline.uncertainty_classifier import (
    UncertaintyType,
    UncertaintyClassification,
    _parse_classification,
    classify_uncertainty,
    classify_all_low_confidence,
)
from calibration.store import CalibrationStore


# ── Helpers ───────────────────────────────────────────────────────────────────

@dataclass
class _FakeResult:
    consumer: str
    verdict: str = "UNCERTAIN"
    confidence: float = 0.3
    reasoning: str = "The change could be interpreted either way."


@pytest_asyncio.fixture
async def store(tmp_path):
    db = str(tmp_path / "unc.db")
    async with CalibrationStore(db) as s:
        yield s


def _mock_llm(response_text: str):
    llm = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = response_text
    llm.ainvoke = AsyncMock(return_value=mock_resp)
    return llm


# ── _parse_classification ─────────────────────────────────────────────────────

class TestParseClassification:

    def test_parses_data_uncertainty(self):
        raw = "TYPE: DATA_UNCERTAINTY\nREASON: Both interpretations are valid."
        result = _parse_classification("svc-a", 0.3, raw)
        assert result.unc_type == UncertaintyType.DATA
        assert "both" in result.reason.lower()
        assert result.consumer == "svc-a"
        assert result.confidence == pytest.approx(0.3)

    def test_parses_scope_uncertainty(self):
        raw = "TYPE: SCOPE_UNCERTAINTY\nREASON: gRPC streaming is outside evaluation domain."
        result = _parse_classification("svc-b", 0.2, raw)
        assert result.unc_type == UncertaintyType.SCOPE
        assert "grpc" in result.reason.lower()

    def test_case_insensitive_type(self):
        raw = "type: data_uncertainty\nreason: ambiguous field removal."
        result = _parse_classification("svc-c", 0.4, raw)
        assert result.unc_type == UncertaintyType.DATA

    def test_missing_type_returns_unclassified(self):
        raw = "I cannot determine the source of uncertainty."
        result = _parse_classification("svc-d", 0.3, raw)
        assert result.unc_type == UncertaintyType.UNCLASSIFIED
        assert result.consumer == "svc-d"

    def test_empty_response_returns_unclassified(self):
        result = _parse_classification("svc-e", 0.25, "")
        assert result.unc_type == UncertaintyType.UNCLASSIFIED

    def test_missing_reason_still_classified(self):
        raw = "TYPE: SCOPE_UNCERTAINTY"
        result = _parse_classification("svc-f", 0.2, raw)
        assert result.unc_type == UncertaintyType.SCOPE
        assert result.reason == ""

    def test_extra_preamble_ignored(self):
        raw = "After analysis:\nTYPE: DATA_UNCERTAINTY\nREASON: Evidence ambiguous."
        result = _parse_classification("svc-g", 0.35, raw)
        assert result.unc_type == UncertaintyType.DATA


# ── classify_uncertainty ──────────────────────────────────────────────────────

class TestClassifyUncertainty:

    @pytest.mark.asyncio
    async def test_calls_llm_and_returns_classification(self):
        llm = _mock_llm("TYPE: DATA_UNCERTAINTY\nREASON: Field removal is ambiguous.")
        result = await classify_uncertainty(_FakeResult("svc-a"), llm)
        assert result.unc_type == UncertaintyType.DATA
        llm.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_error_returns_unclassified(self):
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        result = await classify_uncertainty(_FakeResult("svc-b", confidence=0.2), llm)
        assert result.unc_type == UncertaintyType.UNCLASSIFIED
        assert result.consumer == "svc-b"

    @pytest.mark.asyncio
    async def test_scope_classification_returned(self):
        llm = _mock_llm("TYPE: SCOPE_UNCERTAINTY\nREASON: Protocol unknown.")
        result = await classify_uncertainty(_FakeResult("svc-c"), llm)
        assert result.unc_type == UncertaintyType.SCOPE

    @pytest.mark.asyncio
    async def test_consumer_and_confidence_preserved(self):
        llm = _mock_llm("TYPE: DATA_UNCERTAINTY\nREASON: Ambiguous.")
        r = _FakeResult("my-svc", confidence=0.27)
        result = await classify_uncertainty(r, llm)
        assert result.consumer == "my-svc"
        assert result.confidence == pytest.approx(0.27)


# ── classify_all_low_confidence ───────────────────────────────────────────────

class TestClassifyAllLowConfidence:

    @pytest.mark.asyncio
    async def test_only_classifies_below_threshold(self):
        results = [
            _FakeResult("low-svc",  confidence=0.25),   # below threshold
            _FakeResult("high-svc", confidence=0.80),   # above threshold
        ]
        llm = _mock_llm("TYPE: DATA_UNCERTAINTY\nREASON: Ambiguous.")
        classified = await classify_all_low_confidence(results, threshold=0.35, llm=llm)
        assert "low-svc" in classified
        assert "high-svc" not in classified

    @pytest.mark.asyncio
    async def test_empty_when_all_above_threshold(self):
        results = [_FakeResult("svc", confidence=0.9)]
        llm = _mock_llm("TYPE: DATA_UNCERTAINTY\nREASON: x")
        classified = await classify_all_low_confidence(results, threshold=0.35, llm=llm)
        assert classified == {}

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty(self):
        llm = _mock_llm("TYPE: DATA_UNCERTAINTY\nREASON: x")
        classified = await classify_all_low_confidence([], threshold=0.35, llm=llm)
        assert classified == {}

    @pytest.mark.asyncio
    async def test_multiple_consumers_classified_concurrently(self):
        results = [
            _FakeResult(f"svc-{i}", confidence=0.2) for i in range(4)
        ]
        llm = _mock_llm("TYPE: SCOPE_UNCERTAINTY\nREASON: Out of domain.")
        classified = await classify_all_low_confidence(results, threshold=0.35, llm=llm)
        assert len(classified) == 4
        assert all(c.unc_type == UncertaintyType.SCOPE for c in classified.values())

    @pytest.mark.asyncio
    async def test_individual_llm_failure_does_not_abort_others(self):
        results = [
            _FakeResult("good-svc", confidence=0.2),
            _FakeResult("bad-svc",  confidence=0.2),
        ]
        call_count = 0

        async def _side_effect(messages, **_):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            if call_count == 2:
                raise RuntimeError("timeout")
            resp.content = "TYPE: DATA_UNCERTAINTY\nREASON: Ambiguous."
            return resp

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=_side_effect)
        classified = await classify_all_low_confidence(results, threshold=0.35, llm=llm)
        # Both consumers should have entries; one will be UNCLASSIFIED
        assert len(classified) == 2
        types = {c.unc_type for c in classified.values()}
        assert UncertaintyType.DATA in types
        assert UncertaintyType.UNCLASSIFIED in types


# ── to_dict ───────────────────────────────────────────────────────────────────

class TestToDict:

    def test_has_required_keys(self):
        c = UncertaintyClassification("svc-a", UncertaintyType.DATA, "Ambiguous.", 0.3)
        d = c.to_dict()
        assert "consumer" in d
        assert "type" in d
        assert "reason" in d
        assert "confidence" in d

    def test_type_is_string_value(self):
        c = UncertaintyClassification("svc-b", UncertaintyType.SCOPE, "", 0.2)
        assert c.to_dict()["type"] == "SCOPE_UNCERTAINTY"

    def test_unclassified_type_string(self):
        c = UncertaintyClassification("svc-c")
        assert c.to_dict()["type"] == "UNCLASSIFIED"


# ── Store operations ──────────────────────────────────────────────────────────

class TestStoreOperations:

    @pytest.mark.asyncio
    async def test_record_and_retrieve_stats(self, store):
        classifications = {
            "svc-a": UncertaintyClassification("svc-a", UncertaintyType.DATA, "Ambiguous.", 0.3),
            "svc-b": UncertaintyClassification("svc-b", UncertaintyType.SCOPE, "Out of domain.", 0.2),
        }
        n = await store.record_uncertainty_classifications("run-001", classifications)
        assert n == 2

        stats = await store.get_uncertainty_source_stats()
        assert stats["total_classified"] == 2
        assert stats["totals"].get("DATA_UNCERTAINTY", 0) == 1
        assert stats["totals"].get("SCOPE_UNCERTAINTY", 0) == 1

    @pytest.mark.asyncio
    async def test_upsert_on_duplicate(self, store):
        c1 = {"svc-a": UncertaintyClassification("svc-a", UncertaintyType.DATA, "v1", 0.3)}
        c2 = {"svc-a": UncertaintyClassification("svc-a", UncertaintyType.SCOPE, "v2", 0.3)}
        await store.record_uncertainty_classifications("run-001", c1)
        await store.record_uncertainty_classifications("run-001", c2)

        stats = await store.get_uncertainty_source_stats()
        # INSERT OR REPLACE → only 1 row
        assert stats["total_classified"] == 1

    @pytest.mark.asyncio
    async def test_per_consumer_breakdown(self, store):
        rows = {
            "pay": UncertaintyClassification("pay", UncertaintyType.DATA, "", 0.3),
            "ord": UncertaintyClassification("ord", UncertaintyType.SCOPE, "", 0.2),
        }
        await store.record_uncertainty_classifications("run-x", rows)
        stats = await store.get_uncertainty_source_stats()
        assert "pay" in stats["per_consumer"]
        assert stats["per_consumer"]["pay"].get("DATA_UNCERTAINTY") == 1

    @pytest.mark.asyncio
    async def test_empty_store_returns_zero(self, store):
        stats = await store.get_uncertainty_source_stats()
        assert stats["total_classified"] == 0
        assert stats["totals"] == {}
