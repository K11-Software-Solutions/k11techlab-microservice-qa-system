#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/conformal_threshold.py
───────────────────────────────
Feature 8 — Conformal HITL Threshold CLI.

Computes and reports the split-conformal uncertainty threshold from the
current calibration store. Shows a table of (α, τ, FNR bound, empirical
metrics) to help operators choose an appropriate miscoverage level.

Usage
-----
    python scripts/conformal_threshold.py              # table at α = 0.05/0.10/0.15/0.20
    python scripts/conformal_threshold.py --alpha 0.10 # single row
    python scripts/conformal_threshold.py --alpha 0.10 --compare  # vs static threshold
    python scripts/conformal_threshold.py --db path/to/calibration.db
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

# allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

from calibration.conformal import ConformalHITLThreshold, DEFAULT_ALPHA, MIN_WRONG_SAMPLES
from calibration.store import CALIBRATION_DB, CalibrationStore
from pipeline.confidence import UNCERTAINTY_THRESHOLD, aggregate_agent_confidence


async def _load_pairs(db: str) -> list[tuple[float, bool]]:
    async with CalibrationStore(db) as store:
        run_data = await store.get_run_calibration_data()

    pairs: list[tuple[float, bool]] = []
    for run in run_data:
        summary = aggregate_agent_confidence(run["agent_scores"])
        pairs.append((summary.uncertainty_score, run["any_wrong"]))
    return pairs


def _empirical_fnr(pairs: list[tuple[float, bool]], threshold: float) -> float:
    """Fraction of wrong-verdict runs with uncertainty_score < threshold (escape gate)."""
    wrong = [s for s, is_wrong in pairs if is_wrong]
    if not wrong:
        return 0.0
    escaped = sum(1 for s in wrong if s < threshold)
    return escaped / len(wrong)


def _empirical_flag_rate(pairs: list[tuple[float, bool]], threshold: float) -> float:
    """Fraction of correct-verdict runs flagged (HITL cost)."""
    correct = [s for s, is_wrong in pairs if not is_wrong]
    if not correct:
        return 0.0
    flagged = sum(1 for s in correct if s >= threshold)
    return flagged / len(correct)


def _row(
    pairs: list[tuple[float, bool]],
    alpha: float,
) -> tuple[str, str, str, str, str, str]:
    ct = ConformalHITLThreshold.fit(pairs, alpha=alpha)
    emp_fnr   = _empirical_fnr(pairs, ct.threshold)
    flag_rate = _empirical_flag_rate(pairs, ct.threshold)
    return (
        f"{alpha:.2f}",
        f"{ct.threshold:.3f}",
        f"{ct.fnr_upper_bound:.3f}",
        f"{emp_fnr:.3f}",
        f"{flag_rate:.3f}",
        str(ct.n_wrong),
    )


def _print_table(pairs: list[tuple[float, bool]], alphas: list[float]) -> None:
    header = ("α", "τ", "FNR bound", "Emp. FNR", "Flag rate", "n_wrong")
    rows = [_row(pairs, a) for a in alphas]

    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(header)]

    sep = "  ".join("-" * w for w in widths)
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)

    print(fmt.format(*header))
    print(sep)
    for row in rows:
        print(fmt.format(*row))

    n_total = len(pairs)
    n_wrong = sum(1 for _, is_wrong in pairs if is_wrong)
    print(f"\nn_calibration={n_total}  n_wrong={n_wrong}  "
          f"static_threshold={UNCERTAINTY_THRESHOLD:.3f}  "
          f"min_wrong_to_activate={MIN_WRONG_SAMPLES}")


def _print_compare(pairs: list[tuple[float, bool]], alpha: float) -> None:
    ct = ConformalHITLThreshold.fit(pairs, alpha=alpha)

    static_fnr   = _empirical_fnr(pairs, UNCERTAINTY_THRESHOLD)
    static_flag  = _empirical_flag_rate(pairs, UNCERTAINTY_THRESHOLD)
    conf_fnr     = _empirical_fnr(pairs, ct.threshold)
    conf_flag    = _empirical_flag_rate(pairs, ct.threshold)

    print(f"{'':30s}  {'Static':>8s}  {'Conformal':>10s}")
    print("-" * 52)
    print(f"{'Threshold (τ)':30s}  {UNCERTAINTY_THRESHOLD:>8.3f}  {ct.threshold:>10.3f}")
    print(f"{'Empirical FNR':30s}  {static_fnr:>8.3f}  {conf_fnr:>10.3f}")
    print(f"{'FNR guarantee (upper bound)':30s}  {'—':>8s}  {ct.fnr_upper_bound:>10.3f}")
    print(f"{'Empirical flag rate (FPR)':30s}  {static_flag:>8.3f}  {conf_flag:>10.3f}")
    print(f"{'n_calibration':30s}  {ct.n_calibration:>8d}  {ct.n_calibration:>10d}")
    print(f"{'n_wrong':30s}  {ct.n_wrong:>8d}  {ct.n_wrong:>10d}")
    print(f"\nConformal α = {alpha:.2f}")
    if ct.n_wrong < MIN_WRONG_SAMPLES:
        print(
            f"WARNING: n_wrong={ct.n_wrong} < MIN_WRONG_SAMPLES={MIN_WRONG_SAMPLES}. "
            f"Pipeline will use static threshold until more wrong-verdict runs accumulate."
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Conformal HITL threshold report")
    parser.add_argument("--alpha", type=float, default=None,
                        help="Target FNR level. If omitted, shows table for 0.05/0.10/0.15/0.20.")
    parser.add_argument("--compare", action="store_true",
                        help="Compare conformal vs static threshold (requires --alpha).")
    parser.add_argument("--db", default=CALIBRATION_DB,
                        help="Path to calibration SQLite database.")
    args = parser.parse_args()

    pairs = await _load_pairs(args.db)
    n_wrong = sum(1 for _, is_wrong in pairs if is_wrong)

    if not pairs:
        print("No resolved calibration data found. Run the evaluation first.")
        sys.exit(1)

    if args.compare:
        alpha = args.alpha if args.alpha is not None else DEFAULT_ALPHA
        _print_compare(pairs, alpha)
    elif args.alpha is not None:
        _print_table(pairs, [args.alpha])
    else:
        _print_table(pairs, [0.05, 0.10, 0.15, 0.20])

    if n_wrong < MIN_WRONG_SAMPLES:
        print(
            f"\nNOTE: {n_wrong} wrong-verdict run(s) available — need {MIN_WRONG_SAMPLES} "
            f"to activate conformal threshold in the pipeline."
        )


if __name__ == "__main__":
    asyncio.run(main())
