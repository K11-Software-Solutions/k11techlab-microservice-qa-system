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
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/03_analyse_results.py
────────────────────────────
Analyses evaluation results and computes RQ1–RQ4 metrics.

RQ1: How accurately does the system detect breaking contract changes?
     → Precision, recall, F1 on breaking change detection
RQ2: What is the false negative rate for breaking changes reaching production?
     → FNR = FN / (TP + FN)
RQ3: How does impact radius scoring correlate with production incident severity?
     → Pearson correlation (requires prod_severity in results)
RQ4: Does the cross-repo HITL gate reduce distributed system failures?
     → HITL trigger rate, false positive rate, missed escalations

Usage:
    python eval/03_analyse_results.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "results.json")


def compute_classification_metrics(results: list[dict]) -> dict:
    """
    Binary classification: does the pipeline correctly identify breaking scenarios?
    Ground truth: scenario["is_breaking"]
    Prediction:   pipeline_verdict == "BREAKING"
    """
    tp = fp = tn = fn = 0
    for r in results:
        actual    = r.get("is_breaking", False)
        predicted = r.get("pipeline_verdict") == "BREAKING"
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif not actual and not predicted:
            tn += 1
        else:
            fn += 1  # missed breaking change

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    fnr       = fn / (tp + fn) if (tp + fn) > 0 else 0.0
    accuracy  = (tp + tn) / len(results) if results else 0.0

    return {
        "TP": tp, "FP": fp, "TN": tn, "FN": fn,
        "precision": round(precision, 4),
        "recall":    round(recall, 4),
        "f1":        round(f1, 4),
        "FNR":       round(fnr, 4),    # RQ2: false negative rate
        "accuracy":  round(accuracy, 4),
    }


def compute_impact_score_stats(results: list[dict]) -> dict:
    """Descriptive statistics on impact scores by scenario type."""
    breaking_scores     = [r["impact_score"] for r in results if r.get("is_breaking")]
    non_breaking_scores = [r["impact_score"] for r in results if not r.get("is_breaking")]

    def stats(scores):
        if not scores:
            return {}
        return {
            "count":  len(scores),
            "mean":   round(sum(scores) / len(scores), 4),
            "min":    round(min(scores), 4),
            "max":    round(max(scores), 4),
        }

    return {
        "breaking_scenarios":     stats(breaking_scores),
        "non_breaking_scenarios": stats(non_breaking_scores),
    }


def compute_hitl_metrics(results: list[dict]) -> dict:
    """
    RQ4: HITL gate effectiveness.
    hitl_triggered = True when pipeline escalated for human review.
    Correct escalation = breaking scenario + hitl_triggered.
    """
    triggered     = [r for r in results if r.get("hitl_triggered")]
    not_triggered = [r for r in results if not r.get("hitl_triggered")]

    correct_esc   = [r for r in triggered     if r.get("is_breaking")]
    false_esc     = [r for r in triggered     if not r.get("is_breaking")]
    missed_esc    = [r for r in not_triggered if r.get("is_breaking")]
    correct_pass  = [r for r in not_triggered if not r.get("is_breaking")]

    total_breaking = sum(1 for r in results if r.get("is_breaking"))
    hitl_precision = len(correct_esc) / len(triggered)   if triggered else 0.0
    hitl_recall    = len(correct_esc) / total_breaking    if total_breaking else 0.0

    return {
        "total_scenarios":   len(results),
        "hitl_triggered":    len(triggered),
        "trigger_rate":      round(len(triggered) / len(results), 4) if results else 0.0,
        "correct_escalation": len(correct_esc),
        "false_escalation":   len(false_esc),
        "missed_escalation":  len(missed_esc),
        "hitl_precision":    round(hitl_precision, 4),
        "hitl_recall":       round(hitl_recall, 4),
    }


def print_report(results: list[dict]) -> None:
    clf   = compute_classification_metrics(results)
    score = compute_impact_score_stats(results)
    hitl  = compute_hitl_metrics(results)

    print("=" * 60)
    print("K11tech Microservice QA — Evaluation Results")
    print("=" * 60)

    print("\n── RQ1: Breaking Change Detection Accuracy ──")
    print(f"  Precision:   {clf['precision']:.1%}")
    print(f"  Recall:      {clf['recall']:.1%}")
    print(f"  F1 Score:    {clf['f1']:.1%}")
    print(f"  Accuracy:    {clf['accuracy']:.1%}")
    print(f"  TP={clf['TP']}  FP={clf['FP']}  TN={clf['TN']}  FN={clf['FN']}")

    print("\n── RQ2: False Negative Rate ──")
    print(f"  FNR: {clf['FNR']:.1%}  (breaking changes that reached 'COMPATIBLE' verdict)")
    print(f"  {clf['FN']} out of {clf['TP'] + clf['FN']} breaking scenarios were missed")

    print("\n── RQ3: Impact Score Distribution ──")
    bs = score["breaking_scenarios"]
    ns = score["non_breaking_scenarios"]
    if bs:
        print(f"  Breaking scenarios    — mean={bs['mean']:.3f}  min={bs['min']:.3f}  max={bs['max']:.3f}")
    if ns:
        print(f"  Non-breaking scenarios — mean={ns['mean']:.3f}  min={ns['min']:.3f}  max={ns['max']:.3f}")

    print("\n── RQ4: Cross-Repo HITL Gate ──")
    print(f"  Trigger rate:        {hitl['trigger_rate']:.1%} of scenarios")
    print(f"  Correct escalations: {hitl['correct_escalation']}")
    print(f"  False escalations:   {hitl['false_escalation']}")
    print(f"  Missed escalations:  {hitl['missed_escalation']}")
    print(f"  HITL Precision:      {hitl['hitl_precision']:.1%}")
    print(f"  HITL Recall:         {hitl['hitl_recall']:.1%}")

    print("\n── Per-Scenario Summary ──")
    for r in results:
        verdict = r.get("pipeline_verdict", "?")
        gt      = "BREAKING" if r.get("is_breaking") else "COMPATIBLE"
        match   = "✓" if verdict == gt or (verdict == "UNCERTAIN" and gt == "BREAKING") else "✗"
        print(
            f"  {match} {r['scenario_id']:8s}  "
            f"GT={gt:10s}  Pred={verdict:10s}  "
            f"score={r.get('impact_score', 0):.3f}  "
            f"radius={r.get('impact_radius', 0)}"
        )
    print("=" * 60)

    # Write JSON metrics
    metrics_path = os.path.join(os.path.dirname(__file__), "metrics.json")
    with open(metrics_path, "w") as f:
        json.dump({
            "classification": clf,
            "impact_scores":  score,
            "hitl":           hitl,
        }, f, indent=2)
    print(f"\n  Metrics saved to: {metrics_path}")


if __name__ == "__main__":
    if not os.path.exists(RESULTS_PATH):
        print(f"Results not found at {RESULTS_PATH}. Run 02_run_evaluation.py first.")
        sys.exit(1)
    with open(RESULTS_PATH) as f:
        results = json.load(f)
    print_report(results)
