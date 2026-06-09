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
scripts/run_ablation.py
────────────────────────
Multi-model ablation for the Phase A eval set.

Runs the same 15 scenarios with different LLM backends and saves per-model
results to eval/results_ablation_<model>.json. A --compare flag prints a
side-by-side summary of all completed ablation runs.

Usage:
    # Run with a specific model (restarts uvicorn automatically)
    python scripts/run_ablation.py --model claude-haiku-4-5 --provider anthropic
    python scripts/run_ablation.py --model claude-opus-4-8  --provider anthropic
    python scripts/run_ablation.py --model gpt-4o           --provider openai

    # Compare all completed runs (does not restart server)
    python scripts/run_ablation.py --compare

    # Just compare two specific models
    python scripts/run_ablation.py --compare --models claude-sonnet-4-6,claude-haiku-4-5

Prerequisites:
    - GITHUB_TOKEN set in .env
    - For anthropic models: ANTHROPIC_API_KEY set in .env
    - For openai models:    OPENAI_API_KEY set in .env
    - uvicorn will be started/restarted automatically by this script
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ablation")

EVAL_DIR      = Path(__file__).parent.parent / "eval"
RESULTS_SONNET = EVAL_DIR / "results_phase_a.json"   # Eval 5 canonical baseline

# Models to include in comparison (add gpt-4o once OPENAI_API_KEY is filled)
KNOWN_MODELS = {
    "claude-sonnet-4-6":        ("anthropic", RESULTS_SONNET),
    "claude-haiku-4-5":         ("anthropic", EVAL_DIR / "results_ablation_claude-haiku-4-5.json"),
    "claude-opus-4-8":          ("anthropic", EVAL_DIR / "results_ablation_claude-opus-4-8.json"),
    "sonnet-complete-graph":    ("anthropic", EVAL_DIR / "results_ablation_sonnet-complete-graph.json"),
    "gpt-4o":                   ("openai",    EVAL_DIR / "results_ablation_gpt-4o.json"),
}


# ── Server management ──────────────────────────────────────────────────────────

_server_proc: subprocess.Popen | None = None


def start_server(model: str, provider: str) -> subprocess.Popen:
    """Start uvicorn with the specified LLM model."""
    env = os.environ.copy()
    env["LLM_MODEL"]    = model
    env["LLM_PROVIDER"] = provider
    env["PYTHONIOENCODING"] = "utf-8"

    log.info("Starting uvicorn with LLM_MODEL=%s LLM_PROVIDER=%s", model, provider)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.webhook:app", "--port", "9001"],
        env=env,
        stdout=open(EVAL_DIR / "ablation_server_out.log", "w"),
        stderr=open(EVAL_DIR / "ablation_server_err.log", "w"),
        cwd=str(Path(__file__).parent.parent),
    )
    time.sleep(5)   # give server time to boot
    log.info("Server started (PID=%d)", proc.pid)
    return proc


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    log.info("Stopping server (PID=%d)...", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.info("Server stopped")


# ── Run ablation for one model ─────────────────────────────────────────────────

# Aliases: logical name → actual LLM model string passed to the server
_MODEL_ALIASES = {
    "sonnet-complete-graph": "claude-sonnet-4-6",
}


async def run_model(model: str, provider: str) -> None:
    """Run the Phase A eval with a specific model and save results."""
    out_path = EVAL_DIR / f"results_ablation_{model}.json"
    llm_model = _MODEL_ALIASES.get(model, model)   # resolve alias → real model name

    if model == "claude-sonnet-4-6" and RESULTS_SONNET.exists():
        log.info("Sonnet results already exist at %s — skipping re-run", RESULTS_SONNET)
        return

    # Validate key availability
    if provider == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set — check .env")
    if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY not set — check .env (add your OpenAI key to enable GPT-4o ablation)")

    global _server_proc
    _server_proc = start_server(llm_model, provider)

    try:
        # Import and run the Phase A eval loop with modified output path
        import importlib.util
        _mod_name = f"run_phase_a_eval_{model.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(
            _mod_name,
            Path(__file__).parent / "run_phase_a_eval.py",
        )
        mod = importlib.util.module_from_spec(spec)
        # Register before exec so @dataclass can resolve cls.__module__
        sys.modules[_mod_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        # Monkey-patch the output path and run
        mod.RESULTS_PATH = out_path
        await mod.main()

        # Stamp which model produced these results
        if out_path.exists():
            data = json.loads(out_path.read_text())
            data["ablation_label"]    = model          # logical name (e.g. sonnet-complete-graph)
            data["ablation_model"]    = llm_model      # actual LLM model string
            data["ablation_provider"] = provider
            out_path.write_text(json.dumps(data, indent=2))
            log.info("Saved ablation results to %s", out_path)

    finally:
        stop_server(_server_proc)
        _server_proc = None


# ── Comparison table ───────────────────────────────────────────────────────────

def load_results(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def compare_models(model_names: list[str] | None = None) -> None:
    """Print a comparison table of all completed ablation runs."""
    targets = model_names or list(KNOWN_MODELS.keys())

    rows = []
    for model in targets:
        if model not in KNOWN_MODELS:
            log.warning("Unknown model %s — skipping", model)
            continue
        provider, path = KNOWN_MODELS[model]
        data = load_results(path)
        if data is None:
            rows.append((model, provider, None))
            continue
        m = data.get("metrics", {})
        b3 = m.get("rq1_full_pipeline", {})
        hitl = m.get("rq4_hitl", {})
        lat = m.get("rq5_latency", {})
        rows.append((model, provider, {
            "P":    round(b3.get("precision", 0) * 100, 1),
            "R":    round(b3.get("recall", 0) * 100, 1),
            "F1":   round(b3.get("f1", 0) * 100, 1),
            "Acc":  round(b3.get("accuracy", 0) * 100, 1),
            "TP":   b3.get("tp", 0),
            "FP":   b3.get("fp", 0),
            "TN":   b3.get("tn", 0),
            "FN":   b3.get("fn", 0),
            "Unc":  b3.get("uncertain", 0),
            "HITL": f"{hitl.get('triggered',0)}T/{hitl.get('false_escalations',0)}F",
            "Lat":  lat.get("mean_s", 0),
        }))

    # Print table
    print()
    print("=" * 90)
    print("MULTI-MODEL ABLATION — Phase A (15 scenarios, 9 BREAKING, 6 COMPATIBLE)")
    print("=" * 90)
    hdr = f"{'Model':<24} {'Provider':<12} {'P':>6} {'R':>6} {'F1':>6} {'Acc':>6}  {'TP/FP/TN/FN/Unc':>20}  {'HITL':>8}  {'Lat(s)':>7}"
    print(hdr)
    print("-" * 90)
    for model, provider, d in rows:
        if d is None:
            print(f"{model:<24} {provider:<12}  {'(not run yet)':>50}")
            continue
        counts = f"{d['TP']}/{d['FP']}/{d['TN']}/{d['FN']}/{d['Unc']}"
        print(f"{model:<24} {provider:<12} {d['P']:>5.1f}% {d['R']:>5.1f}% {d['F1']:>5.1f}% {d['Acc']:>5.1f}%  {counts:>20}  {d['HITL']:>8}  {d['Lat']:>6.1f}s")
    print("=" * 90)

    # Per-scenario breakdown if multiple models have results
    completed = [(m, p, d) for m, p, d in rows if d is not None]
    if len(completed) < 2:
        return

    all_data = {}
    for model, _, path_or_dict in rows:
        if _ is None or path_or_dict is None:
            continue
        _, path = KNOWN_MODELS.get(model, (None, None))
        if path is None:
            continue
        data = load_results(path)
        if data:
            all_data[model] = {r["scenario_id"]: r["verdict"] for r in data.get("results", [])}

    if len(all_data) < 2:
        return

    print()
    print("PER-SCENARIO VERDICTS")
    print("-" * 90)
    model_list = list(all_data.keys())
    header = f"{'ID':>5}  {'GT':>12}  " + "  ".join(f"{m[:20]:>20}" for m in model_list)
    print(header)
    print("-" * 90)

    # Use any model's results to get scenario list/ground truth
    first_path = KNOWN_MODELS[model_list[0]][1]
    first_data = load_results(first_path)
    if first_data:
        for r in first_data.get("results", []):
            sid = r["scenario_id"]
            gt  = r["ground_truth"]
            verdicts = "  ".join(
                f"{all_data.get(m, {}).get(sid, '?'):>20}"
                for m in model_list
            )
            print(f"{sid:>5}  {gt:>12}  {verdicts}")
    print("=" * 90)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-model ablation for Phase A eval")
    ap.add_argument("--model",    default="claude-haiku-4-5",
                    help="LLM model name (default: claude-haiku-4-5)")
    ap.add_argument("--provider", default="anthropic",
                    help="LLM provider: anthropic | openai (default: anthropic)")
    ap.add_argument("--compare",  action="store_true",
                    help="Print comparison table of all completed runs")
    ap.add_argument("--models",   default="",
                    help="Comma-separated list of models for --compare filter")
    args = ap.parse_args()

    if args.compare:
        model_filter = [m.strip() for m in args.models.split(",") if m.strip()] or None
        compare_models(model_filter)
        return

    def _sigint(sig, frame):
        stop_server(_server_proc)
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)
    asyncio.run(run_model(args.model, args.provider))
    compare_models()   # print summary after run


if __name__ == "__main__":
    main()
