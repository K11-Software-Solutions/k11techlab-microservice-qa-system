# -*- coding: utf-8 -*-
"""
Unit tests for Feature 4 — Contract Change History & Drift Detection.
"""
import pytest
from analyzer.drift_detector import (
    detect_drift,
    DriftReport,
    DRIFT_WINDOW_DAYS,
    DRIFT_MEDIUM_THRESHOLD,
    DRIFT_HIGH_THRESHOLD,
    DRIFT_CRITICAL_THRESHOLD,
)
from pipeline.confidence import apply_drift_floor


def _history(*shas, recorded_at="2026-06-01T00:00:00+00:00") -> list[dict]:
    """Build a minimal history list from SHA strings."""
    return [{"sha": sha, "recorded_at": recorded_at} for sha in shas]


# ── detect_drift ──────────────────────────────────────────────────────────────

class TestDetectDrift:

    def test_empty_history_returns_low_drift(self):
        report = detect_drift("svc-a", [])
        assert report.drift_level == "LOW"
        assert report.change_count == 0
        assert report.uncertainty_floor == 0.0
        assert report.last_changed_at is None

    def test_single_change_low_drift(self):
        report = detect_drift("svc-a", _history("sha1"))
        assert report.drift_level == "LOW"
        assert report.uncertainty_floor == 0.0

    def test_below_medium_threshold_is_low(self):
        shas = [f"sha{i}" for i in range(DRIFT_MEDIUM_THRESHOLD - 1)]
        report = detect_drift("svc-a", _history(*shas))
        assert report.drift_level == "LOW"

    def test_at_medium_threshold_is_medium(self):
        shas = [f"sha{i}" for i in range(DRIFT_MEDIUM_THRESHOLD)]
        report = detect_drift("svc-a", _history(*shas))
        assert report.drift_level == "MEDIUM"
        assert report.uncertainty_floor == 0.10

    def test_at_high_threshold_is_high(self):
        shas = [f"sha{i}" for i in range(DRIFT_HIGH_THRESHOLD)]
        report = detect_drift("svc-a", _history(*shas))
        assert report.drift_level == "HIGH"
        assert report.uncertainty_floor == 0.25

    def test_at_critical_threshold_is_critical(self):
        shas = [f"sha{i}" for i in range(DRIFT_CRITICAL_THRESHOLD)]
        report = detect_drift("svc-a", _history(*shas))
        assert report.drift_level == "CRITICAL"
        assert report.uncertainty_floor == 0.40

    def test_duplicate_shas_counted_once(self):
        """Re-recorded SHAs (re-runs) must not inflate the count."""
        report = detect_drift("svc-a", _history("sha1", "sha1", "sha1"))
        assert report.change_count == 1
        assert report.drift_level == "LOW"

    def test_velocity_calculated_per_week(self):
        shas = [f"sha{i}" for i in range(7)]   # 7 changes in 30-day window
        report = detect_drift("svc-a", _history(*shas), window_days=30)
        expected_velocity = 7 / (30 / 7)
        assert report.change_velocity == pytest.approx(expected_velocity, abs=0.01)

    def test_last_changed_at_is_first_row(self):
        history = [
            {"sha": "sha1", "recorded_at": "2026-06-10T00:00:00+00:00"},
            {"sha": "sha2", "recorded_at": "2026-06-01T00:00:00+00:00"},
        ]
        report = detect_drift("svc-a", history)
        assert report.last_changed_at == "2026-06-10T00:00:00+00:00"

    def test_to_dict_contains_all_fields(self):
        report = detect_drift("svc-a", _history("sha1"))
        d = report.to_dict()
        for key in ("service", "window_days", "change_count", "change_velocity",
                    "drift_level", "last_changed_at", "uncertainty_floor"):
            assert key in d

    def test_service_name_preserved(self):
        report = detect_drift("k11-payment-service", [])
        assert report.service == "k11-payment-service"

    def test_custom_window_days(self):
        shas = [f"sha{i}" for i in range(DRIFT_MEDIUM_THRESHOLD)]
        report = detect_drift("svc-a", _history(*shas), window_days=7)
        assert report.window_days == 7


# ── apply_drift_floor ─────────────────────────────────────────────────────────

class TestApplyDriftFloor:

    def test_none_report_returns_score_unchanged(self):
        assert apply_drift_floor(0.15, None) == pytest.approx(0.15)

    def test_floor_raises_low_score(self):
        report = {"uncertainty_floor": 0.25}
        assert apply_drift_floor(0.10, report) == pytest.approx(0.25)

    def test_floor_does_not_lower_higher_score(self):
        report = {"uncertainty_floor": 0.25}
        assert apply_drift_floor(0.40, report) == pytest.approx(0.40)

    def test_zero_floor_no_effect(self):
        report = {"uncertainty_floor": 0.0}
        assert apply_drift_floor(0.12, report) == pytest.approx(0.12)

    def test_critical_floor_applied(self):
        shas = [f"sha{i}" for i in range(DRIFT_CRITICAL_THRESHOLD)]
        drift_dict = detect_drift("svc-a", _history(*shas)).to_dict()
        result = apply_drift_floor(0.05, drift_dict)
        assert result == pytest.approx(0.40)

    def test_missing_floor_key_safe(self):
        assert apply_drift_floor(0.10, {}) == pytest.approx(0.10)
