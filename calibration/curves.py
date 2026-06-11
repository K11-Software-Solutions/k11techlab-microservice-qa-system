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
calibration/curves.py
──────────────────────
Calibration curve computation and matplotlib visualisation.

A calibration curve asks: when the agent reports confidence X, was it
actually correct X fraction of the time?

Perfect calibration lies on the diagonal (y = x).
- Curve below diagonal → overconfident (agent claims certainty it doesn't have)
- Curve above diagonal → underconfident (agent is more accurate than it thinks)

Usage
─────
    rows   = await store.get_resolved_rows()
    result = compute_calibration(rows)
    fig    = plot_calibration(result, output_path="calibration.png")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CalibrationResult:
    """Output of compute_calibration()."""
    # Per-bin arrays (length = number of non-empty bins)
    bin_mean_confidence: list[float]   = field(default_factory=list)
    bin_accuracy:        list[float]   = field(default_factory=list)
    bin_counts:          list[int]     = field(default_factory=list)

    # Per-agent breakdowns: agent_name → CalibrationResult
    per_agent: dict[str, "CalibrationResult"] = field(default_factory=dict)

    # Per hop-depth breakdowns: hop_depth → CalibrationResult
    per_hop_depth: dict[int, "CalibrationResult"] = field(default_factory=dict)

    # Scalar summary metrics
    ece:            float = 0.0   # Expected Calibration Error
    total_samples:  int   = 0
    correct:        int   = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total_samples if self.total_samples else 0.0


def _is_correct(verdict: str, ground_truth: str) -> bool:
    """
    A verdict is correct when it matches ground truth.
    UNCERTAIN verdicts are treated as correct when ground_truth is BREAKING
    (conservative — the agent flagged doubt) and incorrect when COMPATIBLE.
    """
    if verdict == ground_truth:
        return True
    if verdict == "UNCERTAIN" and ground_truth == "BREAKING":
        return True   # flagged as uncertain → appropriate caution
    return False


def _compute_bins(
    confidences: list[float],
    correct_flags: list[bool],
    n_bins: int = 10,
) -> "CalibrationResult":
    if not confidences:
        return CalibrationResult()

    import math
    bin_size = 1.0 / n_bins
    bins: dict[int, list] = {i: [] for i in range(n_bins)}

    for conf, ok in zip(confidences, correct_flags):
        idx = min(int(conf / bin_size), n_bins - 1)
        bins[idx].append((conf, ok))

    bin_means, bin_accs, bin_counts = [], [], []
    ece_num = 0.0

    for entries in bins.values():
        if not entries:
            continue
        confs_b = [e[0] for e in entries]
        oks_b   = [e[1] for e in entries]
        mean_c  = sum(confs_b) / len(confs_b)
        acc_b   = sum(oks_b) / len(oks_b)
        bin_means.append(mean_c)
        bin_accs.append(acc_b)
        bin_counts.append(len(entries))
        ece_num += len(entries) * abs(mean_c - acc_b)

    n = len(confidences)
    ece = ece_num / n if n else 0.0

    return CalibrationResult(
        bin_mean_confidence=bin_means,
        bin_accuracy=bin_accs,
        bin_counts=bin_counts,
        ece=ece,
        total_samples=n,
        correct=sum(correct_flags),
    )


def compute_calibration(rows: list[dict], n_bins: int = 10) -> CalibrationResult:
    """
    Compute calibration curve from resolved calibration log rows.

    Each row must have: confidence, verdict, ground_truth, agent, hop_depth.
    Rows with ground_truth == 'UNKNOWN' are silently skipped.
    """
    valid = [
        r for r in rows
        if r.get("ground_truth") not in ("UNKNOWN", "pending", None)
    ]
    if not valid:
        logger.warning("compute_calibration: no resolved rows found")
        return CalibrationResult()

    confidences   = [float(r["confidence"]) for r in valid]
    correct_flags = [_is_correct(r["verdict"], r["ground_truth"]) for r in valid]

    overall = _compute_bins(confidences, correct_flags, n_bins)

    # Per-agent breakdown
    agents: dict[str, list] = {}
    for r, ok in zip(valid, correct_flags):
        agents.setdefault(r["agent"], []).append((float(r["confidence"]), ok))
    overall.per_agent = {
        agent: _compute_bins([c for c, _ in pairs], [ok for _, ok in pairs], n_bins)
        for agent, pairs in agents.items()
    }

    # Per hop-depth breakdown
    depths: dict[int, list] = {}
    for r, ok in zip(valid, correct_flags):
        depths.setdefault(int(r.get("hop_depth", 1)), []).append((float(r["confidence"]), ok))
    overall.per_hop_depth = {
        depth: _compute_bins([c for c, _ in pairs], [ok for _, ok in pairs], n_bins)
        for depth, pairs in depths.items()
    }

    logger.info(
        "Calibration computed: n=%d accuracy=%.3f ECE=%.4f",
        overall.total_samples, overall.accuracy, overall.ece,
    )
    return overall


def plot_calibration(
    result: CalibrationResult,
    output_path: str | None = None,
    title: str = "Agent Confidence Calibration",
) -> "matplotlib.figure.Figure":
    """
    Generate a calibration plot and optionally save it.

    Returns the matplotlib Figure (caller can show() or savefig() it).
    Requires matplotlib — raises ImportError with a helpful message if absent.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except ImportError as exc:
        raise ImportError(
            "matplotlib is required for calibration plots: pip install matplotlib"
        ) from exc

    n_agents = len(result.per_agent)
    n_depths = len(result.per_hop_depth)
    # Layout: overall + per-agent row + per-depth row
    n_rows = 1 + (1 if n_agents > 0 else 0) + (1 if n_depths > 1 else 0)
    n_cols = max(1, n_agents, n_depths)

    fig = plt.figure(figsize=(5 * n_cols, 5 * n_rows), constrained_layout=True)
    fig.suptitle(title, fontsize=14, fontweight="bold")
    gs = gridspec.GridSpec(n_rows, n_cols, figure=fig)

    def _draw_curve(ax, cal: CalibrationResult, label: str) -> None:
        ax.plot([0, 1], [0, 1], "k--", linewidth=1, label="Perfect calibration")
        if cal.bin_mean_confidence:
            sizes = [max(20, c * 5) for c in cal.bin_counts]
            ax.scatter(
                cal.bin_mean_confidence, cal.bin_accuracy,
                s=sizes, zorder=3, label=f"n={cal.total_samples}",
            )
            ax.plot(cal.bin_mean_confidence, cal.bin_accuracy, "-o", linewidth=1.5)
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.set_xlabel("Mean confidence"); ax.set_ylabel("Fraction correct")
        ax.set_title(f"{label}\nECE={cal.ece:.4f}  acc={cal.accuracy:.3f}")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    # Row 0: overall
    ax0 = fig.add_subplot(gs[0, :])
    _draw_curve(ax0, result, "Overall")

    # Row 1: per-agent
    if n_agents > 0:
        for col, (agent, cal) in enumerate(sorted(result.per_agent.items())):
            short = agent.split(":")[-1] if ":" in agent else agent
            ax = fig.add_subplot(gs[1, col % n_cols])
            _draw_curve(ax, cal, f"Agent: {short}")

    # Row 2: per hop-depth
    if n_depths > 1:
        row = 1 + (1 if n_agents > 0 else 0)
        for col, (depth, cal) in enumerate(sorted(result.per_hop_depth.items())):
            ax = fig.add_subplot(gs[row, col % n_cols])
            label = "Direct (depth=1)" if depth == 1 else f"Transitive (depth={depth})"
            _draw_curve(ax, cal, label)

    if output_path:
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
        logger.info("Calibration plot saved to %s", output_path)

    return fig
