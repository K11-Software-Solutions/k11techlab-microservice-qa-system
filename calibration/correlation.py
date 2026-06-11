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
calibration/correlation.py
───────────────────────────
Feature 7: Correlation-Aware Uncertainty Aggregation.

The naive aggregation formula in pipeline/confidence.py averages agent confidence
scores with equal weights, implicitly assuming the agents are independent signals.
They are not — all agents process the same PR context, so a confusing PR makes
multiple agents uncertain simultaneously. Correlated uncertain signals carry less
total information than independent uncertain signals.

This module models the inter-agent correlation structure from historical run data
and uses it to compute **precision-weighted** (minimum-variance) aggregation instead
of a simple mean.

Theory
──────
Given N agent confidence scores x = [x₁, ..., xN] from a single run, the minimum-
variance linear unbiased estimator (Gauss-Markov) of the population mean uses the
inverse-covariance (precision) weights:

    w = Σ⁻¹ 1 / (1ᵀ Σ⁻¹ 1)        (precision weights, sum to 1)

where Σ is the N×N covariance matrix estimated from historical runs.

When all agents are perfectly correlated (Σ = σ² J, J = all-ones matrix), w
collapses to uniform 1/N weights — cannot do better than equal-weighting when
agents carry identical information. When agents are anti-correlated, their
weights are amplified relative to uniform, extracting the maximum information
from their disagreement.

The **effective number of independent agents** quantifies the compression:

    n_eff = (Σᵢ λᵢ)² / Σᵢ λᵢ²       (Kish's formula applied to eigenvalues of R)

where λᵢ are eigenvalues of the Pearson correlation matrix R. n_eff = N when agents
are fully independent; n_eff = 1 when all agents are perfectly correlated.

Usage
─────
    from calibration.correlation import AgentCorrelationMatrix
    from calibration.store import CalibrationStore

    async with CalibrationStore() as store:
        matrix = await AgentCorrelationMatrix.from_store(store)

    if matrix:
        adj_mean   = matrix.adjusted_mean(scores_dict)
        n_eff      = matrix.effective_n()
        corr_data  = matrix.to_dict()
"""
from __future__ import annotations

import logging
import os
import statistics
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from calibration.store import CalibrationStore

logger = logging.getLogger(__name__)

# Minimum historical runs needed before we trust the correlation estimate.
# Below this count the matrix is too noisy; fall through to uniform weights.
MIN_RUNS: int = int(os.getenv("CORR_MIN_RUNS", "10"))

# Tikhonov regularisation added to the diagonal of Σ before inversion.
# Prevents singular-matrix errors when agents are nearly perfectly correlated.
# Default 0.10 corresponds to adding noise variance of 0.01 to each diagonal.
REGULARIZATION: float = float(os.getenv("CORR_REGULARIZATION", "0.10"))


class AgentCorrelationMatrix:
    """
    Pairwise Pearson correlation structure for a set of consumer compliance agents.

    Constructed from historical calibration_log entries.  Each row in the log
    is a (run_id, agent, confidence) triple; we pivot to a runs × agents matrix,
    then compute the sample covariance and Pearson correlation matrices.

    The precision-weighted mean returned by adjusted_mean() replaces the naive
    arithmetic mean in aggregate_agent_confidence() so that correlated agents
    are discounted and anti-correlated agents are amplified.
    """

    def __init__(
        self,
        agents: list[str],
        corr: np.ndarray,
        cov: np.ndarray,
        n_runs: int,
    ) -> None:
        self.agents = agents          # ordered list of agent names
        self.corr   = corr            # N×N Pearson correlation matrix
        self.cov    = cov             # N×N sample covariance matrix
        self.n_runs = n_runs          # number of complete runs used

        self._weights_cache: dict[tuple, np.ndarray] = {}

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    async def from_store(
        cls,
        store: "CalibrationStore",
        min_runs: int = MIN_RUNS,
    ) -> "AgentCorrelationMatrix | None":
        """
        Build the correlation matrix from the calibration store.

        Returns None if there are fewer than `min_runs` complete runs (where
        "complete" means every agent reported a confidence score for that run).
        At that point the correlation estimate is too noisy to be useful and
        the caller should fall back to uniform weighting.
        """
        rows = await store.get_agent_scores_matrix()

        if len(rows) < min_runs:
            logger.debug(
                "AgentCorrelationMatrix: only %d runs, need %d — using uniform weights",
                len(rows), min_runs,
            )
            return None

        # Collect the union of all agents seen across runs
        all_agents: set[str] = set()
        for row in rows:
            all_agents.update(row["agent_scores"].keys())
        agents = sorted(all_agents)

        if len(agents) < 2:
            logger.debug("AgentCorrelationMatrix: fewer than 2 agents — no correlation structure")
            return None

        # Keep only runs where ALL known agents reported a score (complete cases)
        complete = [r for r in rows if all(a in r["agent_scores"] for a in agents)]

        if len(complete) < min_runs:
            logger.debug(
                "AgentCorrelationMatrix: only %d complete runs (need %d) — using uniform weights",
                len(complete), min_runs,
            )
            return None

        X = np.array(
            [[row["agent_scores"][a] for a in agents] for row in complete],
            dtype=float,
        )   # shape: (n_complete_runs, n_agents)

        # Pearson correlation matrix (N×N)
        corr = np.corrcoef(X, rowvar=False)

        # Sample covariance matrix (N×N)
        cov = np.cov(X, rowvar=False)

        logger.info(
            "AgentCorrelationMatrix built: n_agents=%d n_runs=%d n_eff=%.2f",
            len(agents), len(complete),
            cls._compute_effective_n(corr),
        )
        return cls(agents=agents, corr=corr, cov=cov, n_runs=len(complete))

    # ── Effective N ───────────────────────────────────────────────────────────

    def effective_n(self) -> float:
        """
        Kish's effective sample size applied to eigenvalues of the correlation matrix.

            n_eff = (Σᵢ λᵢ)² / Σᵢ λᵢ²

        Since tr(R) = N, this simplifies to N²/Σᵢ λᵢ².

        Range:  1 ≤ n_eff ≤ N
          - n_eff = N   when all eigenvalues equal 1 (independent agents)
          - n_eff = 1   when one eigenvalue = N and rest = 0 (perfect correlation)
        """
        return self._compute_effective_n(self.corr)

    @staticmethod
    def _compute_effective_n(corr: np.ndarray) -> float:
        eigenvalues = np.linalg.eigvalsh(corr)
        eigenvalues = np.maximum(eigenvalues, 0.0)  # clip numerical negatives
        sum_sq = float(np.sum(eigenvalues ** 2))
        if sum_sq == 0.0:
            return float(corr.shape[0])
        return float(np.sum(eigenvalues) ** 2 / sum_sq)

    # ── Precision weights ─────────────────────────────────────────────────────

    def precision_weights(self, present_agents: list[str]) -> np.ndarray:
        """
        Compute the minimum-variance (precision-weighted) combination weights for
        the supplied subset of agents.

            w = clip(Σ_reg⁻¹ 1, 0) / ‖clip(Σ_reg⁻¹ 1, 0)‖₁

        Regularisation: Σ_reg = Σ[sub] + λ·I prevents inversion failure when
        agents are nearly perfectly correlated.

        Negative weights are clipped to 0 and the remainder is renormalised.
        This loses the formal BLUE property but produces a valid convex combination
        that is interpretable and numerically stable.

        If fewer than 2 agents overlap with the stored agents, returns uniform weights.
        """
        key = tuple(sorted(present_agents))
        if key in self._weights_cache:
            return self._weights_cache[key]

        overlap = [a for a in present_agents if a in self.agents]
        n = len(overlap)

        if n < 2:
            w = np.ones(len(present_agents)) / max(len(present_agents), 1)
            self._weights_cache[key] = w
            return w

        idx = [self.agents.index(a) for a in overlap]
        cov_sub = self.cov[np.ix_(idx, idx)]
        cov_reg = cov_sub + REGULARIZATION * np.eye(n)

        try:
            cov_inv = np.linalg.inv(cov_reg)
        except np.linalg.LinAlgError:
            logger.warning("Precision weight inversion failed — falling back to uniform")
            w = np.ones(n) / n
            self._weights_cache[key] = w
            return w

        ones  = np.ones(n)
        raw_w = cov_inv @ ones
        raw_w = np.maximum(raw_w, 0.0)       # clip to non-negative
        total = raw_w.sum()
        w     = raw_w / total if total > 1e-12 else np.ones(n) / n

        self._weights_cache[key] = w
        return w

    # ── Aggregation ───────────────────────────────────────────────────────────

    def adjusted_mean(self, scores: dict[str, float]) -> float:
        """
        Return the precision-weighted mean of `scores`.

        Agents not in the stored correlation structure receive equal (uniform)
        weight among themselves; agents present in the structure receive
        precision weights derived from the covariance matrix.

        If the overlap between `scores` and stored agents is < 2, falls back
        to a simple arithmetic mean.
        """
        if not scores:
            return 0.0

        overlap   = [a for a in self.agents if a in scores]
        non_overlap = [a for a in scores if a not in self.agents]

        if len(overlap) < 2:
            return statistics.mean(scores.values())

        weights     = self.precision_weights(overlap)
        known_vals  = np.array([scores[a] for a in overlap])
        known_wmean = float(np.dot(weights, known_vals))

        if not non_overlap:
            return known_wmean

        # Blend: known (precision-weighted) vs unknown (equal-weighted) subsets
        # Weight each subset proportionally to its size
        n_total   = len(overlap) + len(non_overlap)
        unk_mean  = statistics.mean(scores[a] for a in non_overlap)
        blended   = (len(overlap) / n_total) * known_wmean + (len(non_overlap) / n_total) * unk_mean
        return float(blended)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return a JSON-serialisable summary of the correlation matrix."""
        n = len(self.agents)
        return {
            "n_agents":   n,
            "n_runs":     self.n_runs,
            "effective_n": round(self.effective_n(), 3),
            "agents":     self.agents,
            "correlation_matrix": {
                self.agents[i]: {
                    self.agents[j]: round(float(self.corr[i, j]), 4)
                    for j in range(n)
                }
                for i in range(n)
            },
            "precision_weights": {
                a: round(float(w), 4)
                for a, w in zip(
                    self.agents,
                    self.precision_weights(self.agents),
                )
            },
        }
