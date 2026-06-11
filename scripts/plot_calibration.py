#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/plot_calibration.py
────────────────────────────
CLI tool for generating calibration plots and printing statistics from the
calibration study database.

Usage
─────
    # Plot overall + per-agent + per-hop-depth curves
    python scripts/plot_calibration.py --output calibration.png

    # Print stats table only (no plot)
    python scripts/plot_calibration.py --stats-only

    # Use a different database
    python scripts/plot_calibration.py --db path/to/calibration.db --output out.png

    # Fetch pending CI ground truth before plotting
    python scripts/plot_calibration.py --fetch-gt --output calibration.png

    # Filter to manual-only ground truth (highest trust)
    python scripts/plot_calibration.py --gt-source manual --output calibration_manual.png
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make sure the project root is on sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass


async def _main(args: argparse.Namespace) -> None:
    from calibration.store import CalibrationStore
    from calibration.curves import compute_calibration, plot_calibration

    db_path = args.db or os.getenv("CALIBRATION_DB", "calibration.db")

    async with CalibrationStore(db_path) as store:
        # Optionally resolve pending CI ground truth first
        if args.fetch_gt:
            from calibration.ground_truth import fetch_ci_ground_truth
            print("Fetching CI ground truth for pending runs...")
            counts = await fetch_ci_ground_truth(store)
            print(f"  resolved={counts['resolved']}  skipped={counts['skipped']}  errors={counts['errors']}")

        stats = await store.stats()
        print(f"\nCalibration store: {db_path}")
        print(f"  Total rows : {stats['total']}")
        print(f"  Resolved   : {stats['resolved']}")
        print(f"  Pending    : {stats['pending']}")

        if stats["resolved"] == 0:
            print("\nNo resolved rows yet — run the pipeline on real PRs and resolve ground truth.")
            return

        rows = await store.get_resolved_rows()

    # Filter by gt_source if requested
    if args.gt_source:
        rows = [r for r in rows if r.get("gt_source") == args.gt_source]
        print(f"  Filtered to gt_source='{args.gt_source}': {len(rows)} rows")

    if not rows:
        print("No rows match the filter.")
        return

    result = compute_calibration(rows, n_bins=args.bins)

    # Print summary table
    print(f"\n{'Agent':<50} {'n':>6} {'Accuracy':>10} {'ECE':>8}")
    print("-" * 78)
    print(f"{'Overall':<50} {result.total_samples:>6} {result.accuracy:>10.3f} {result.ece:>8.4f}")
    for agent, cal in sorted(result.per_agent.items()):
        short = agent.split(":")[-1] if ":" in agent else agent
        print(f"  {short:<48} {cal.total_samples:>6} {cal.accuracy:>10.3f} {cal.ece:>8.4f}")

    if result.per_hop_depth:
        print(f"\n{'Hop depth':<50} {'n':>6} {'Accuracy':>10} {'ECE':>8}")
        print("-" * 78)
        for depth, cal in sorted(result.per_hop_depth.items()):
            label = "Direct (1)" if depth == 1 else f"Transitive ({depth})"
            print(f"  {label:<48} {cal.total_samples:>6} {cal.accuracy:>10.3f} {cal.ece:>8.4f}")

    if args.stats_only:
        return

    # Generate plot
    output = args.output or "calibration.png"
    try:
        plot_calibration(result, output_path=output, title="K11tech Agent Confidence Calibration")
        print(f"\nPlot saved to: {output}")
    except ImportError as exc:
        print(f"\nCannot plot: {exc}")
        print("Install matplotlib: pip install matplotlib")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate calibration curves from the K11tech QA pipeline study."
    )
    parser.add_argument("--db",         default=None,           help="Path to calibration.db")
    parser.add_argument("--output",     default="calibration.png", help="Output image path")
    parser.add_argument("--bins",       type=int, default=10,   help="Number of confidence bins")
    parser.add_argument("--stats-only", action="store_true",    help="Print stats, skip plot")
    parser.add_argument("--fetch-gt",   action="store_true",    help="Fetch CI ground truth first")
    parser.add_argument("--gt-source",  default=None,           help="Filter: ci_failure | manual | proxy")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
