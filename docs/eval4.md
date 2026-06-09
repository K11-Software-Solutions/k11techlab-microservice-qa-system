# Evaluation Run 4 — Phase A Re-run (Fixes Applied)

**Date:** 2026-06-08  
**Model:** `claude-sonnet-4-6` (via `langchain-anthropic`)  
**Scenarios:** 15 (9 BREAKING, 6 COMPATIBLE)  
**Repos:** K11-Software-Solutions/{k11-user-service, k11-order-service, k11-payment-service, k11-notification-svc}  
**Trigger:** HMAC-signed webhook POST (simulated) → `api/webhook.py` → LangGraph pipeline  
**Script:** `scripts/run_phase_a_eval.py`  
**Results:** `eval/results_phase_a.json`

---

## Fixes Applied Since Eval 3

| Fix | File | Description |
|-----|------|-------------|
| F-1 (partial) | `analyzer/change_detector.py` | Added `_diff_response_schema()` + `_resolve_response_schema()`. Guard: only compare when **both** base and head have response schemas (`if base_resp and head_resp`). Without the guard, missing response schemas in generated specs cause all base fields to appear "removed". |
| F-1 (partial) | `scripts/run_phase_a_eval.py` | Updated `_BASE_USER_PATHS` to include `$ref` response schemas (GET /users, POST /users, GET /users/{id}, GET /users/{id}/contact). Required so head contracts also carry response schemas, enabling A-03/A-05 comparison. |
| F-3 | `pipeline/orchestrator.py` | Moved `hitl_check` node from Phase 2→Phase 3 boundary to **after Phase 3**. `compliance_results` is now populated before HITL reads `breaking_consumers`. |
| HITL interrupt | `pipeline/hitl.py` | `cross_repo_hitl_check` now stores a preliminary `summary` when HITL fires. Previously, `ainvoke()` returned `state["summary"]=None` on interrupt, which the eval (and `/runs` API) mapped to COMPATIBLE. Now the interrupt state carries `overall_verdict=BREAKING` + Phase 3 consumer counts. |
| Re-run SHA fix | `scripts/run_phase_a_eval.py` | Added `get_branch_file_sha()` — gets current file SHA from the eval branch (not main). Prevents 422 SHA mismatch on re-runs. |
| PR head SHA fix | `scripts/run_phase_a_eval.py` | Webhook now uses `commit_sha` (from `update_file`) as `head_sha` instead of the PR API's cached value. The PR API can return a stale head SHA on re-runs. **Note: this fix took effect mid-run (A-10 onward), so A-03/A-05 still used stale SHAs in this run.** |

---

## Results Summary

### RQ1 — Breaking Change Detection Accuracy

| Pipeline | Precision | Recall | F1 | Accuracy | TP | FP | TN | FN | Uncertain |
|----------|-----------|--------|-----|----------|----|----|----|----|-----------|
| **B3 — Full pipeline (LLM)** | **100%** | 75% | **85.7%** | 80.0% | 6 | 0 | 6 | 2 | 1 |
| B2 — Graph + diff (no LLM) | **100%** | 77.8% | **87.5%** | **86.7%** | 7 | 0 | 6 | 2 | 0 |
| B1 — Diff only (no graph) | 87.5% | 77.8% | 82.4% | 80.0% | 7 | 1 | 5 | 2 | 0 |

**Note:** Recall denominator excludes UNCERTAIN verdicts (A-02 counted as excluded, not FN). True FNs are A-03 and A-05.

**Identical to Eval 3 on precision/recall metrics** — improvements are in HITL and system correctness, not diff detection (A-03/A-05 expected to be fixed in Eval 5).

### RQ4 — HITL Gate

**HITL triggered for 5 scenarios (5 true, 0 false, 4 missed)** — major improvement over Eval 3 (0 triggered).

| Scenario | Consumers (breaking) | HITL Triggered | Reason |
|----------|----------------------|----------------|--------|
| A-01 | 3 | **Yes** | 3 ≥ threshold 2 |
| A-04 | 3 | **Yes** | 3 ≥ threshold 2 |
| A-06 | 2 | **Yes** | 2 ≥ threshold 2 |
| A-07 | 1 | No | 1 < threshold 2 |
| A-14 | 3 | **Yes** | 3 ≥ threshold 2 |
| A-15 | 3 | **Yes** | 3 ≥ threshold 2 |
| A-02 | 0 (all uncertain) | No | uncertain ≠ breaking |
| A-03/A-05 | 0 (FN) | No | Phase 1 missed → no consumers |

4 missed escalations: A-02 (uncertain, not confirmed breaking), A-03/A-05 (FNs), A-07 (1 consumer < 2).

HITL interrupt-and-resume flow works correctly. Preliminary summary stored on interrupt preserves the BREAKING verdict and Phase 3 consumer counts through the pause.

### RQ5 — Latency and Cost

| Metric | Value |
|--------|-------|
| Mean wall time | **11.9 s** |
| Min wall time | 8.1 s (Phase 1 short-circuit, no LLM) |
| Max wall time | 24.3 s (A-01: 3 parallel LLM consumer checks) |
| HITL scenarios (Phase 3 + interrupt) | ~14–17 s |

---

## Per-Scenario Results

| ID | Verdict | GT | OK | B1 | B2 | Consumers | Score | HITL |
|----|---------|----|----|-----|-----|-----------|-------|------|
| A-01 | BREAKING | BREAKING | ✓ | BREAKING | BREAKING | 3 breaking | 0.370 | **Yes** |
| A-02 | UNCERTAIN | BREAKING | ✗ | BREAKING | BREAKING | 0 br, 3 unc | 0.295 | No |
| A-03 | COMPATIBLE | BREAKING | ✗ | COMPATIBLE | COMPATIBLE | 0 | 0.000 | No |
| A-04 | BREAKING | BREAKING | ✓ | BREAKING | BREAKING | 3 breaking | 0.220 | **Yes** |
| A-05 | COMPATIBLE | BREAKING | ✗ | COMPATIBLE | COMPATIBLE | 0 | 0.000 | No |
| A-06 | BREAKING | BREAKING | ✓ | BREAKING | BREAKING | 2 breaking | 0.405 | **Yes** |
| A-07 | BREAKING | BREAKING | ✓ | BREAKING | BREAKING | 1 br, 1 unc | 0.225 | No |
| A-08 | COMPATIBLE | COMPATIBLE | ✓ | COMPATIBLE | COMPATIBLE | 0 | 0.000 | No |
| A-09 | COMPATIBLE | COMPATIBLE | ✓ | COMPATIBLE | COMPATIBLE | 0 | 0.050 | No |
| A-10 | COMPATIBLE | COMPATIBLE | ✓ | COMPATIBLE | COMPATIBLE | 0 | 0.000 | No |
| A-11 | COMPATIBLE | COMPATIBLE | ✓ | COMPATIBLE | COMPATIBLE | 0 | 0.000 | No |
| A-12 | COMPATIBLE | COMPATIBLE | ✓ | COMPATIBLE | COMPATIBLE | 0 | 0.000 | No |
| A-13 | COMPATIBLE | COMPATIBLE | ✓ | **BREAKING** | COMPATIBLE | 0 | 0.300 | No |
| A-14 | BREAKING | BREAKING | ✓ | BREAKING | BREAKING | 3 breaking | 0.405 | **Yes** |
| A-15 | BREAKING | BREAKING | ✓ | BREAKING | BREAKING | 3 breaking | 0.405 | **Yes** |

---

## Root Cause: A-03/A-05 Still FN

**Expected** after F-1 fix: A-03 and A-05 should detect response schema changes.

**Actual** in this run: both still return COMPATIBLE (score=0.000).

**Root cause:** The `commit_sha` fix (use `update_file` return SHA as `head_sha` for webhook) was applied to the eval script **while the eval was running**, at scenario A-10. Python processes load the script once at startup; the fix was not visible to scenarios A-01–A-09. The webhook for A-03/A-05 received the stale PR head SHA (pointing to the **previous run's** commit), which was pushed before `_BASE_USER_PATHS` had response schemas. Without response schemas in the head spec, `_resolve_response_schema(head_ep.responses)` returns `{}`, and the `if base_resp and head_resp` guard skips the comparison.

**Verification:** B1 and B2 baselines for A-03/A-05 also show COMPATIBLE — they too computed the diff from the stale spec SHA. This confirms the issue is the spec being fetched (wrong SHA), not the diff algorithm.

**Fix already in place** for Eval 5: `head_sha = commit_sha` (line ~858 in `run_phase_a_eval.py`). The next full run will use the freshly-pushed commit for all scenarios.

**Expected Eval 5 metrics:** B3 P=100%, R=88.9%, F1=94.1% (A-03 and A-05 detected).

---

## Comparison: All Four Evaluations

| Metric | Eval 1 (synthetic, 4 scenarios) | Eval 2 (live webhook, 1 PR) | Eval 3 (controlled, 15 PRs) | Eval 4 (15 PRs, fixes) |
|--------|--------------------------------|------------------------------|------------------------------|------------------------|
| Scenarios | 4 | 1 | 15 | 15 |
| B3 Precision | 100% | 100% | 100% | **100%** |
| B3 Recall | 100% | 100% | 75% | 75% |
| B3 F1 | 100% | 100% | 86% | **85.7%** |
| False positives | 0 | 0 | 0 | 0 |
| HITL triggered | — | — | 0 (structural bug) | **5 (5 true, 0 false)** |
| Latency mean | — | ~15s | 13.4s | **11.9s** |

The B3 recall is unchanged at 75% because A-03/A-05 require the clean-run fix. The meaningful improvements in this eval are the HITL gate (0→5 correct escalations) and elimination of the HITL-interrupt COMPATIBLE misclassification bug.

---

## Open Items for Eval 5

| Item | Expected outcome |
|------|-----------------|
| Re-run with `commit_sha` fix fully in effect | A-03 detected (response field rename `id→user_id`) |
| Re-run with `commit_sha` fix fully in effect | A-05 detected (email no longer required in response) |
| Verify HITL triggers for A-03/A-05 after detection | Both have 3 consumers → expect HITL trigger |
| A-02 UNCERTAIN → BREAKING path | Requires registering POST /api/v2/users consumer edges |
| Phase B: external open-source repos (50+ PRs) | Required for full paper independence claim |
| Multi-model ablation (gpt-4o vs claude-sonnet) | Required for full paper |
