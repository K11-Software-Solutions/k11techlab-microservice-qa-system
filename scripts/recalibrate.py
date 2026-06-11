#!/usr/bin/env python3
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
scripts/recalibrate.py
────────────────────────
Feature 6 CLI — Adaptive Confidence Recalibration from HITL Feedback.

Fits per-agent isotonic regression (or Platt scaling) models from labelled
data accumulated in calibration.db, prints before/after ECE per agent, and
optionally generates a reliability diagram showing the calibration improvement.

Usage
─────
    # Fit isotonic models from all labelled data, print ECE table
    python scripts/recalibrate.py

    # Use Platt scaling instead
    python scripts/recalibrate.py --model-type platt

    # Dry-run: fit but do NOT persist models to the database
    python scripts/recalibrate.py --dry-run

    # List all models already stored in calibration.db
    python scripts/recalibrate.py --list-models

    # Apply the stored model to a single raw confidence value
    python scripts/recalibrate.py --transform 0.92
    python scripts/recalibrate.py --transform 0.92 --agent contract_compliance_agent:k11-payment-service

    # Generate before/after reliability diagrams to a PNG
    python scripts/recalibrate.py --plot before_after_calibration.png

    # Use a custom database path
    python scripts/recalibrate.py --db path/to/calibration.db
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass


# ── Plotting helper ───────────────────────────────────────────────────────────

def _plot_before_after(
    agent_samples: dict[str, list[tuple[float, bool]]],
    engine,
    output_path: str,
    n_bins: int = 10,
) -> None:
    """
    Generate a two-panel reliability diagram: raw confidence (before) and
    calibrated confidence (after) side-by-side for all agents.
    """
    import numpy as np

    try:
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except ImportError:
        print("  Cannot plot: install matplotlib (pip install matplotlib)")
        return

    fig = plt.figure(figsize=(12, 5))
    gs  = gridspec.GridSpec(1, 2, figure=fig)

    for panel_idx, calibrated in enumerate([False, True]):
        ax = fig.add_subplot(gs[panel_idx])
        bins = np.linspace(0.0, 1.0, n_bins + 1)

        for agent, samples in sorted(agent_samples.items()):
            if len(samples) < 3:
                continue
            short = agent.split(":")[-1] if ":" in agent else agent
            raw_confs = [s[0] for s in samples]
            labels    = [float(s[1]) for s in samples]

            if calibrated and engine.has_model(agent):
                confs = [engine.transform(agent, c) for c in raw_confs]
            else:
                confs = raw_confs

            bin_acc, bin_conf, bin_n = [], [], []
            for lo, hi in zip(bins[:-1], bins[1:]):
                mask = [
                    i for i, c in enumerate(confs)
                    if (c >= lo and c < hi) or (hi == 1.0 and c == 1.0)
                ]
                if not mask:
                    continue
                bin_conf.append(sum(confs[i] for i in mask) / len(mask))
                bin_acc.append(sum(labels[i] for i in mask) / len(mask))
                bin_n.append(len(mask))

            if bin_conf:
                ax.plot(bin_conf, bin_acc, marker="o", linewidth=1.5,
                        markersize=4, label=short)

        ax.plot([0, 1], [0, 1], "k--", linewidth=0.8, alpha=0.5, label="Perfect calibration")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel("Mean Confidence (bin)")
        ax.set_ylabel("Fraction Correct")
        title = "After Recalibration" if calibrated else "Before Recalibration"
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, alpha=0.3)

    fig.suptitle("K11tech Agent Confidence — Feature 6 Recalibration", fontsize=12)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  Plot saved to: {output_path}")


# ── Main async logic ──────────────────────────────────────────────────────────

async def _list_models(db_path: str) -> None:
    from calibration.store import CalibrationStore
    async with CalibrationStore(db_path) as store:
        models = await store.list_models()
    if not models:
        print("No calibration models stored yet.")
        print("Run  python scripts/recalibrate.py  to fit and persist models.")
        return

    print(f"\n{'Agent':<55} {'Type':<10} {'n':>5} {'Before ECE':>12} {'After ECE':>10}  {'Trained At'}")
    print("-" * 110)
    for m in models:
        before = f"{m['before_ece']:.4f}" if m["before_ece"] is not None else "    N/A"
        after  = f"{m['after_ece']:.4f}"  if m["after_ece"]  is not None else "    N/A"
        print(
            f"  {m['agent']:<53} {m['model_type']:<10} {m['n_samples']:>5} "
            f"{before:>12} {after:>10}  {m['trained_at'][:19]}"
        )


async def _transform(
    db_path: str,
    raw: float,
    agent_filter: str | None,
    model_type: str,
) -> None:
    from calibration.store import CalibrationStore
    from calibration.recalibration import RecalibrationEngine

    engine = RecalibrationEngine(model_type=model_type)
    async with CalibrationStore(db_path) as store:
        n_loaded = await engine.load_all(store)
        models = await store.list_models()

    if n_loaded == 0:
        print("No stored models found. Run  python scripts/recalibrate.py  first.")
        return

    targets = [m["agent"] for m in models if m["model_type"] == model_type]
    if agent_filter:
        targets = [a for a in targets if agent_filter in a]
        if not targets:
            print(f"No {model_type} model found matching '{agent_filter}'.")
            return

    print(f"\nRaw confidence: {raw}")
    print(f"{'Agent':<55} {'Calibrated':>12}  {'Delta':>8}")
    print("-" * 80)
    for agent in sorted(targets):
        cal   = engine.transform(agent, raw)
        delta = cal - raw
        sign  = "+" if delta >= 0 else ""
        short = agent.split(":")[-1] if ":" in agent else agent
        print(f"  {short:<53} {cal:>12.4f}  {sign}{delta:>7.4f}")


async def _fit(
    db_path: str,
    model_type: str,
    dry_run: bool,
    plot_path: str | None,
    gt_source: str | None,
) -> None:
    from calibration.store import CalibrationStore
    from calibration.recalibration import RecalibrationEngine, MIN_SAMPLES_TO_FIT

    engine = RecalibrationEngine(model_type=model_type)

    async with CalibrationStore(db_path) as store:
        stats = await store.stats()
        print(f"\nCalibration store: {db_path}")
        print(f"  Total rows : {stats['total']}")
        print(f"  Resolved   : {stats['resolved']}")
        print(f"  Pending    : {stats['pending']}")

        if stats["resolved"] == 0:
            print("\nNo resolved data yet. Populate calibration.db via:")
            print("  python scripts/seed_evaluation.py")
            print("  POST /calibration/resolve")
            return

        # Collect labelled data to show counts before fitting
        gt_rows = await store.get_resolved_rows()
        hitl_rows = await store.get_hitl_labels()

    if gt_source:
        gt_rows = [r for r in gt_rows if r.get("gt_source") == gt_source]
        print(f"  Filtered to gt_source='{gt_source}': {len(gt_rows)} rows")

    print(f"\n  Ground-truth rows : {len(gt_rows)}")
    print(f"  HITL-labelled rows: {len(hitl_rows)}")
    print(f"\nFitting {model_type} recalibration models "
          f"(min_samples={MIN_SAMPLES_TO_FIT})...")

    if dry_run:
        print("  [DRY RUN] Models will NOT be persisted to the database.\n")

    async with CalibrationStore(db_path) as store:
        results = await engine.fit_all(store)

    # Print results table
    fitted  = [r for r in results if r.status == "fitted"]
    skipped = [r for r in results if r.status == "skipped"]

    print(f"\n{'Agent':<55} {'n':>5} {'Before ECE':>12} {'After ECE':>10}  {'Δ ECE':>8}  Status")
    print("-" * 100)

    for r in sorted(results, key=lambda x: x.agent):
        short = r.agent.split(":")[-1] if ":" in r.agent else r.agent
        if r.status == "fitted":
            before = f"{r.before_ece:.4f}" if r.before_ece is not None else "    N/A"
            after  = f"{r.after_ece:.4f}"  if r.after_ece  is not None else "    N/A"
            delta  = (r.after_ece - r.before_ece) if (r.before_ece is not None and r.after_ece is not None) else None
            delta_s = f"{delta:+.4f}" if delta is not None else "    N/A"
            print(f"  {short:<53} {r.n_samples:>5} {before:>12} {after:>10}  {delta_s:>8}  fitted")
        else:
            print(f"  {short:<53} {r.n_samples:>5} {'—':>12} {'—':>10}  {'—':>8}  {r.status} ({r.message})")

    print()
    print(f"  Fitted : {len(fitted)} agent(s)")
    print(f"  Skipped: {len(skipped)} agent(s) (insufficient data)")

    if fitted:
        mean_before = sum(r.before_ece for r in fitted if r.before_ece is not None) / len(fitted)
        mean_after  = sum(r.after_ece  for r in fitted if r.after_ece  is not None) / len(fitted)
        print(f"\n  Mean ECE before: {mean_before:.4f}")
        print(f"  Mean ECE after : {mean_after:.4f}  ({(mean_after - mean_before):+.4f})")

    if dry_run and fitted:
        print("\n  Models were NOT persisted (--dry-run). Re-run without --dry-run to save.")

    # Optional plot
    if plot_path and fitted:
        print(f"\nGenerating before/after reliability diagram...")
        # Rebuild agent_samples for plotting
        from calibration.recalibration import _is_correct
        agent_samples: dict[str, list[tuple[float, bool]]] = {}
        for row in gt_rows:
            if row["ground_truth"] in ("UNKNOWN", ""):
                continue
            agent = row["agent"]
            correct = _is_correct(row["verdict"], row["ground_truth"])
            agent_samples.setdefault(agent, []).append((float(row["confidence"]), correct))
        _plot_before_after(agent_samples, engine, plot_path)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Feature 6 CLI — fit per-agent confidence recalibration models "
            "from HITL feedback and resolved ground truth."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/recalibrate.py
  python scripts/recalibrate.py --model-type platt
  python scripts/recalibrate.py --dry-run
  python scripts/recalibrate.py --list-models
  python scripts/recalibrate.py --transform 0.92
  python scripts/recalibrate.py --transform 0.92 --agent k11-payment-service
  python scripts/recalibrate.py --plot before_after.png
  python scripts/recalibrate.py --gt-source controlled --plot calibration_recal.png
""",
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to calibration.db (default: $CALIBRATION_DB or calibration.db)",
    )
    parser.add_argument(
        "--model-type", default="isotonic", choices=["isotonic", "platt"],
        help="Recalibration model type (default: isotonic)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fit models but do not persist them to the database",
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="List all stored calibration models and exit",
    )
    parser.add_argument(
        "--transform", type=float, default=None, metavar="RAW",
        help="Apply the stored model to this raw confidence value and exit",
    )
    parser.add_argument(
        "--agent", default=None,
        help="Filter --transform or --list-models to a specific agent (substring match)",
    )
    parser.add_argument(
        "--plot", default=None, metavar="OUTPUT_PNG",
        help="Path for before/after reliability diagram PNG",
    )
    parser.add_argument(
        "--gt-source", default=None,
        help="Filter training data to a specific gt_source (e.g. controlled, manual)",
    )

    args = parser.parse_args()

    db_path = args.db or os.getenv("CALIBRATION_DB", "calibration.db")

    if args.list_models:
        asyncio.run(_list_models(db_path))
        return

    if args.transform is not None:
        asyncio.run(_transform(db_path, args.transform, args.agent, args.model_type))
        return

    asyncio.run(_fit(db_path, args.model_type, args.dry_run, args.plot, args.gt_source))


if __name__ == "__main__":
    main()
