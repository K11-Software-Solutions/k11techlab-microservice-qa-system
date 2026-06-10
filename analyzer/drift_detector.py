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
analyzer/drift_detector.py
───────────────────────────
Detects contract change velocity — a leading indicator of instability.

A service whose contract has changed frequently in a short window is
statistically more likely to introduce a breaking change. High-velocity
services receive an elevated uncertainty_score floor so the pipeline
treats them with extra caution even when the current diff looks clean.

Drift levels and their uncertainty_score floors:
  LOW      (0 – 4 changes)   floor = 0.00
  MEDIUM   (5 – 9 changes)   floor = 0.10
  HIGH     (10–14 changes)   floor = 0.25
  CRITICAL (15+ changes)     floor = 0.40
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DRIFT_WINDOW_DAYS        = int(os.getenv("DRIFT_WINDOW_DAYS",        "30"))
DRIFT_MEDIUM_THRESHOLD   = int(os.getenv("DRIFT_MEDIUM_THRESHOLD",   "5"))
DRIFT_HIGH_THRESHOLD     = int(os.getenv("DRIFT_HIGH_THRESHOLD",     "10"))
DRIFT_CRITICAL_THRESHOLD = int(os.getenv("DRIFT_CRITICAL_THRESHOLD", "15"))


@dataclass
class DriftReport:
    service:           str
    window_days:       int
    change_count:      int      # distinct SHAs recorded in window
    change_velocity:   float    # changes per week
    drift_level:       str      # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    last_changed_at:   str | None
    uncertainty_floor: float    # minimum uncertainty_score to apply (0.0 = no effect)

    def to_dict(self) -> dict:
        return {
            "service":           self.service,
            "window_days":       self.window_days,
            "change_count":      self.change_count,
            "change_velocity":   round(self.change_velocity, 2),
            "drift_level":       self.drift_level,
            "last_changed_at":   self.last_changed_at,
            "uncertainty_floor": self.uncertainty_floor,
        }


def detect_drift(
    service: str,
    history: list[dict],
    window_days: int = DRIFT_WINDOW_DAYS,
) -> DriftReport:
    """
    Compute drift metrics from contract history rows.

    Each row must have at least {"sha": str, "recorded_at": str}.
    Duplicate SHAs (same commit re-recorded) are counted once.
    """
    unique_shas  = {r["sha"] for r in history}
    change_count = len(unique_shas)
    weeks        = window_days / 7
    velocity     = change_count / weeks if weeks > 0 else 0.0

    if change_count == 0:
        drift_level, uncertainty_floor = "LOW", 0.00
    elif change_count < DRIFT_MEDIUM_THRESHOLD:
        drift_level, uncertainty_floor = "LOW", 0.00
    elif change_count < DRIFT_HIGH_THRESHOLD:
        drift_level, uncertainty_floor = "MEDIUM", 0.10
    elif change_count < DRIFT_CRITICAL_THRESHOLD:
        drift_level, uncertainty_floor = "HIGH", 0.25
    else:
        drift_level, uncertainty_floor = "CRITICAL", 0.40

    last_changed_at = history[0]["recorded_at"] if history else None

    return DriftReport(
        service=service,
        window_days=window_days,
        change_count=change_count,
        change_velocity=velocity,
        drift_level=drift_level,
        last_changed_at=last_changed_at,
        uncertainty_floor=uncertainty_floor,
    )
