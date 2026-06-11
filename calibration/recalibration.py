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
calibration/recalibration.py
──────────────────────────────
Feature 6: Adaptive Confidence Recalibration from HITL Feedback.

Closes the feedback loop between human reviewer decisions and agent confidence
thresholds. Each time a reviewer approves or overrides a flagged pipeline run,
the decision is recorded as a labelled sample. Periodically, per-agent
isotonic regression models are re-fit against the accumulated labels so that
future confidence scores reflect historical accuracy rather than raw
self-assessment.

Two calibration methods supported:
  - Isotonic regression  (default): non-parametric monotone fit; preserves
    ordering without assuming any functional form. Robust for small datasets.
  - Platt scaling        (alternative): sigmoid logistic fit; smoother but
    requires more data to avoid overfitting.

Per-agent models: calibration is maintained separately for each
(agent, consumer) pair because systematic biases differ — payment-service
may be overconfident while notification-svc is well-calibrated.

Usage::

    from calibration.recalibration import RecalibrationEngine
    from calibration.store import CalibrationStore

    async with CalibrationStore() as store:
        engine = RecalibrationEngine()
        results = await engine.fit_all(store)
        for r in results:
            print(r.agent, r.before_ece, r.after_ece)

    # Apply calibrated confidence at inference time
    calibrated = engine.transform("contract_compliance_agent:k11-payment-service", 0.92)
"""
from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from calibration.store import CalibrationStore

logger = logging.getLogger(__name__)

MIN_SAMPLES_TO_FIT = 5   # don't fit with fewer than this many labelled rows


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class AgentRecalibrationResult:
    agent:        str
    model_type:   str
    n_samples:    int
    before_ece:   float | None
    after_ece:    float | None
    status:       str   # 'fitted' | 'skipped' | 'identity'
    message:      str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_correct(verdict: str, ground_truth: str) -> bool:
    """Match calibration/curves.py correctness definition."""
    if verdict == "COMPATIBLE" and ground_truth == "COMPATIBLE":
        return True
    if verdict == "BREAKING" and ground_truth == "BREAKING":
        return True
    if verdict == "UNCERTAIN" and ground_truth in ("BREAKING", "UNCERTAIN"):
        return True
    return False


def _ece(confidences: list[float], correct: list[bool], n_bins: int = 10) -> float:
    if not confidences:
        return 0.0
    confs = np.array(confidences)
    hits  = np.array([float(c) for c in correct])
    bins  = np.linspace(0.0, 1.0, n_bins + 1)
    ece   = 0.0
    n     = len(confs)
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (confs >= lo) & (confs < hi)
        if hi == 1.0:
            mask |= confs == 1.0
        if not mask.any():
            continue
        bin_acc  = hits[mask].mean()
        bin_conf = confs[mask].mean()
        ece += (mask.sum() / n) * abs(bin_conf - bin_acc)
    return float(ece)


def _fit_isotonic(confidences: np.ndarray, correct: np.ndarray):
    from sklearn.isotonic import IsotonicRegression
    ir = IsotonicRegression(out_of_bounds="clip", increasing=True)
    ir.fit(confidences, correct)
    return ir


def _fit_platt(confidences: np.ndarray, correct: np.ndarray):
    from sklearn.linear_model import LogisticRegression
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)
    lr.fit(confidences.reshape(-1, 1), correct)
    return lr


# ── Engine ────────────────────────────────────────────────────────────────────

class RecalibrationEngine:
    """
    Per-agent confidence recalibration using isotonic regression or Platt scaling.

    The engine maintains an in-memory model cache populated either by fitting
    against the calibration store (fit_all) or by deserialising previously
    persisted models (load_all).
    """

    def __init__(self, model_type: str = "isotonic") -> None:
        if model_type not in ("isotonic", "platt"):
            raise ValueError(f"Unknown model_type: {model_type!r}")
        self.model_type = model_type
        self._models: dict[str, object] = {}   # agent → sklearn estimator

    # ── Inference ─────────────────────────────────────────────────────────────

    def transform(self, agent: str, raw_confidence: float) -> float:
        """
        Apply the fitted calibration model for `agent` to `raw_confidence`.
        Returns raw_confidence unchanged if no model is available (identity).
        """
        model = self._models.get(agent)
        if model is None:
            return raw_confidence
        if self.model_type == "isotonic":
            return float(model.predict([raw_confidence])[0])
        else:  # platt
            prob = model.predict_proba([[raw_confidence]])[0][1]
            return float(prob)

    def has_model(self, agent: str) -> bool:
        return agent in self._models

    # ── Training ──────────────────────────────────────────────────────────────

    async def fit_all(self, store: "CalibrationStore") -> list[AgentRecalibrationResult]:
        """
        Build training data from the calibration store, fit per-agent models,
        persist them, and return per-agent results.

        Training data comes from two sources (merged, deduped by run_id+consumer):
          1. Resolved ground truth rows from calibration_log
          2. HITL reviewer decisions from hitl_outcomes (weaker signal)
        """
        # --- Gather labelled data from resolved ground truth ---
        gt_rows = await store.get_resolved_rows()
        gt_labels: dict[tuple, tuple[float, bool]] = {}  # (run_id, consumer) → (conf, correct)
        for row in gt_rows:
            if row["ground_truth"] in ("UNKNOWN", ""):
                continue
            correct = _is_correct(row["verdict"], row["ground_truth"])
            key = (row.get("run_id", ""), row["consumer"])
            gt_labels[key] = (float(row["confidence"]), correct)

        # --- Gather HITL-labelled data ---
        hitl_rows = await store.get_hitl_labels()
        hitl_labels: dict[tuple, tuple[float, bool]] = {}
        for row in hitl_rows:
            key = (row.get("run_id", ""), row["consumer"])
            if key not in gt_labels:   # don't double-count GT-resolved rows
                hitl_labels[key] = (float(row["confidence"]), bool(row["correct"]))

        # --- Merge and group by agent ---
        agent_samples: dict[str, list[tuple[float, bool]]] = {}
        for (_, consumer), (conf, correct) in {**hitl_labels, **gt_labels}.items():
            agent = f"contract_compliance_agent:{consumer}"
            agent_samples.setdefault(agent, []).append((conf, correct))

        # Also group by consumer from gt_rows for the agent key
        for row in gt_rows:
            if row["ground_truth"] in ("UNKNOWN", ""):
                continue
            agent = row["agent"]
            correct = _is_correct(row["verdict"], row["ground_truth"])
            agent_samples.setdefault(agent, []).append((float(row["confidence"]), correct))

        # Dedup within each agent list
        agent_samples = {
            a: list({(c, ok) for c, ok in samples})
            for a, samples in agent_samples.items()
        }

        results: list[AgentRecalibrationResult] = []
        for agent, samples in sorted(agent_samples.items()):
            result = await self._fit_agent(store, agent, samples)
            results.append(result)

        return results

    async def _fit_agent(
        self,
        store: "CalibrationStore",
        agent: str,
        samples: list[tuple[float, bool]],
    ) -> AgentRecalibrationResult:
        if len(samples) < MIN_SAMPLES_TO_FIT:
            return AgentRecalibrationResult(
                agent=agent,
                model_type=self.model_type,
                n_samples=len(samples),
                before_ece=None,
                after_ece=None,
                status="skipped",
                message=f"Only {len(samples)} samples (min {MIN_SAMPLES_TO_FIT})",
            )

        confs  = np.array([s[0] for s in samples])
        labels = np.array([float(s[1]) for s in samples])

        before_ece = _ece(confs.tolist(), labels.astype(bool).tolist())

        try:
            if self.model_type == "isotonic":
                model = _fit_isotonic(confs, labels)
            else:
                model = _fit_platt(confs, labels)
        except Exception as exc:
            logger.warning("Model fitting failed for %s: %s", agent, exc)
            return AgentRecalibrationResult(
                agent=agent, model_type=self.model_type, n_samples=len(samples),
                before_ece=before_ece, after_ece=None, status="skipped",
                message=str(exc),
            )

        self._models[agent] = model

        # Evaluate calibrated ECE
        if self.model_type == "isotonic":
            cal_confs = model.predict(confs).tolist()
        else:
            cal_confs = [model.predict_proba([[c]])[0][1] for c in confs]
        after_ece = _ece(cal_confs, labels.astype(bool).tolist())

        # Persist to store
        model_bytes = pickle.dumps(model)
        await store.save_model(
            agent=agent,
            model_type=self.model_type,
            model_data=model_bytes,
            n_samples=len(samples),
            before_ece=before_ece,
            after_ece=after_ece,
        )

        logger.info(
            "Recalibrated %s (n=%d): ECE %.4f → %.4f",
            agent, len(samples), before_ece, after_ece,
        )
        return AgentRecalibrationResult(
            agent=agent,
            model_type=self.model_type,
            n_samples=len(samples),
            before_ece=before_ece,
            after_ece=after_ece,
            status="fitted",
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    async def load_all(self, store: "CalibrationStore") -> int:
        """
        Deserialise all persisted models from the store into memory.
        Returns the number of models loaded.
        """
        model_rows = await store.list_models()
        loaded = 0
        for row in model_rows:
            if row["model_type"] != self.model_type:
                continue
            data = await store.load_model(row["agent"], self.model_type)
            if data:
                try:
                    self._models[row["agent"]] = pickle.loads(data)
                    loaded += 1
                except Exception as exc:
                    logger.warning("Failed to load model for %s: %s", row["agent"], exc)
        logger.info("RecalibrationEngine: loaded %d %s models", loaded, self.model_type)
        return loaded
