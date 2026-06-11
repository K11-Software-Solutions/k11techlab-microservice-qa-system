# -*- coding: utf-8 -*-
"""
calibration/conformal.py
────────────────────────
Feature 8 — Conformal HITL Threshold.

Replaces the hand-tuned UNCERTAINTY_THRESHOLD with a threshold τ derived from
the calibration set via split conformal prediction (Vovk et al. 2005;
Angelopoulos & Bates 2023).

Guarantee (marginal, finite-sample):
    P(wrong verdict escapes HITL gate) ≤ α + 1/(n_wrong + 1)

where n_wrong is the number of incorrect-verdict runs in the calibration set.

The threshold is computed on-demand from the CalibrationStore (one async query)
and cached nowhere — it is always derived from the current calibration data.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from calibration.store import CalibrationStore

logger = logging.getLogger(__name__)

MIN_WRONG_SAMPLES: int = int(os.getenv("CONFORMAL_MIN_WRONG", "10"))
DEFAULT_ALPHA: float = float(os.getenv("CONFORMAL_ALPHA", "0.10"))


@dataclass(frozen=True)
class ConformalHITLThreshold:
    """
    Conformal HITL trigger threshold with a finite-sample FNR guarantee.

    Attributes
    ----------
    alpha             : target FNR level (fraction of wrong verdicts allowed to escape)
    threshold         : τ — flag for HITL if uncertainty_score >= threshold
    n_calibration     : total (uncertainty_score, is_wrong) pairs used
    n_wrong           : wrong-verdict runs — determines tightness of guarantee
    fnr_upper_bound   : alpha + 1/(n_wrong+1) — worst-case FNR
    coverage_guarantee: 1 - fnr_upper_bound — fraction of wrong verdicts caught
    """

    alpha: float
    threshold: float
    n_calibration: int
    n_wrong: int
    fnr_upper_bound: float
    coverage_guarantee: float

    # ── construction ──────────────────────────────────────────────────────────

    @classmethod
    def fit(
        cls,
        pairs: list[tuple[float, bool]],
        alpha: float = DEFAULT_ALPHA,
    ) -> "ConformalHITLThreshold":
        """
        Fit a conformal threshold from (uncertainty_score, is_wrong) calibration pairs.

        τ = the k-th smallest wrong-verdict uncertainty score, where
        k = n_wrong - ceil((1-alpha) * (n_wrong+1))    (0-indexed, ascending)

        This sets τ as the alpha-quantile of wrong-verdict scores so that at most
        alpha fraction of wrong verdicts have uncertainty_score < τ (escape the gate).
        The +1 correction gives the finite-sample coverage guarantee.
        """
        wrong_scores = sorted(s for s, is_wrong in pairs if is_wrong)
        n_wrong = len(wrong_scores)
        n_total = len(pairs)

        if n_wrong == 0:
            return cls(
                alpha=alpha, threshold=0.0,
                n_calibration=n_total, n_wrong=0,
                fnr_upper_bound=float(alpha),
                coverage_guarantee=round(1.0 - alpha, 4),
            )

        # k = n - ceil((1-alpha)*(n+1)) ensures the conformal quantile correction
        k = n_wrong - math.ceil((1 - alpha) * (n_wrong + 1))
        k = max(0, min(k, n_wrong - 1))
        tau = wrong_scores[k]

        fnr_ub = alpha + 1.0 / (n_wrong + 1)
        coverage = max(0.0, 1.0 - fnr_ub)

        return cls(
            alpha=alpha,
            threshold=round(tau, 4),
            n_calibration=n_total,
            n_wrong=n_wrong,
            fnr_upper_bound=round(fnr_ub, 4),
            coverage_guarantee=round(coverage, 4),
        )

    @classmethod
    async def from_store(
        cls,
        store: "CalibrationStore",
        alpha: float = DEFAULT_ALPHA,
        min_wrong: int = MIN_WRONG_SAMPLES,
    ) -> "ConformalHITLThreshold | None":
        """
        Load calibration data and fit the threshold.
        Returns None when fewer than min_wrong incorrect runs are available
        (cold start — caller should fall back to static UNCERTAINTY_THRESHOLD).
        """
        from pipeline.confidence import aggregate_agent_confidence  # local to avoid circular

        run_data = await store.get_run_calibration_data()
        pairs: list[tuple[float, bool]] = []
        for run in run_data:
            summary = aggregate_agent_confidence(run["agent_scores"])
            pairs.append((summary.uncertainty_score, run["any_wrong"]))

        n_wrong = sum(1 for _, is_wrong in pairs if is_wrong)
        if n_wrong < min_wrong:
            logger.debug(
                "Conformal threshold cold start: %d wrong runs < min=%d",
                n_wrong, min_wrong,
            )
            return None

        ct = cls.fit(pairs, alpha=alpha)
        logger.debug(
            "Conformal threshold: τ=%.3f α=%.2f FNR≤%.3f n_wrong=%d n_total=%d",
            ct.threshold, ct.alpha, ct.fnr_upper_bound, ct.n_wrong, ct.n_calibration,
        )
        return ct

    # ── helpers ───────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "threshold": self.threshold,
            "n_calibration": self.n_calibration,
            "n_wrong": self.n_wrong,
            "fnr_upper_bound": self.fnr_upper_bound,
            "coverage_guarantee": self.coverage_guarantee,
        }

    def summary_line(self) -> str:
        return (
            f"τ={self.threshold:.3f}  α={self.alpha:.2f}  "
            f"FNR≤{self.fnr_upper_bound:.3f}  coverage≥{self.coverage_guarantee:.3f}  "
            f"n_wrong={self.n_wrong}"
        )
