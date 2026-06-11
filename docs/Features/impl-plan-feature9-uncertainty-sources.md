# Implementation Plan: Feature 9 — Uncertainty Source Classification
**Branch:** `feature/uncertainty-source-classification`
**Depends on:** Feature 1 (compliance_results with reasoning field), Feature 3 (calibration store), Feature 8 (UNCERTAINTY_THRESHOLD)

---

## Overview

Add a secondary LLM classification step that determines whether a low-confidence
verdict is caused by DATA_UNCERTAINTY (evidence is ambiguous — aleatoric) or
SCOPE_UNCERTAINTY (agent is out of domain — epistemic). Results surface in the
HITL interrupt payload and are persisted for analysis.

---

## Design Decisions

### Why secondary prompt, not rule-based classification?

A rule-based approach would pattern-match on the `reasoning` field (e.g., "if
'domain' appears in the text → SCOPE"). This is brittle and fails to generalise.
The LLM-based secondary prompt can reason about the *structure* of uncertainty
in a way that rules cannot. The `reasoning` field is 2–5 sentences produced by
the same LLM — a secondary prompt that reads it is effectively asking the LLM
to reflect on its own uncertainty, which current models handle well
(Kadavath et al. 2022).

The cost is one small LLM call per low-confidence consumer, firing only when
`confidence < UNCERTAINTY_THRESHOLD`. On typical runs this is 0–2 extra calls.

### Why only two types?

Binary classification maximises inter-rater reliability and maps cleanly to
distinct reviewer actions. A third category would require justified boundaries
and risks reviewer disagreement. The DATA/SCOPE split is grounded in
aleatoric/epistemic uncertainty theory (Kendall & Gal 2017) and therefore has
a defensible theoretical basis for the paper.

### Why NOT rerun the full compliance prompt?

The secondary classification does not resend the contract diff. The original
`reasoning` field already summarises the key evidence that led to low confidence.
Resending the full diff doubles cost and introduces the risk that the LLM changes
its assessment entirely rather than just classifying the uncertainty type.

### Where in the graph?

A new `classify_uncertainty_node` between `aggregate_confidence` and `hitl_check`.
This placement ensures:
1. `adjusted_compliance_results` (post-downgrade from Feature 3) are available
2. Classification result is in state before the HITL interrupt fires
3. The HITL reason string and interrupt payload are enriched in the same run

### Non-blocking guarantee

`classify_all_low_confidence` uses `asyncio.gather(..., return_exceptions=True)`.
Any individual LLM call failure returns `UNCLASSIFIED` for that consumer but does
not abort the batch. `classify_uncertainty_node` itself wraps the call in
try/except — a total failure returns `{"uncertainty_classifications": None}` and
the pipeline continues as before Feature 9.

---

## Files to Change

| File | Change Type |
|------|-------------|
| `pipeline/uncertainty_classifier.py` | **New file** |
| `pipeline/orchestrator.py` | New `classify_uncertainty_node`; graph wiring |
| `pipeline/hitl.py` | `_format_classification_summary()`; enrich reason + interrupt |
| `pipeline/state.py` | `uncertainty_classifications: Optional[dict]` field |
| `calibration/store.py` | `uncertainty_classifications` table + two new methods |
| `api/webhook.py` | `GET /calibration/uncertainty-sources` |
| `tests/unit/test_uncertainty_classifier.py` | **New file** |

---

## Step 1 — Taxonomy and Classifier (pipeline/uncertainty_classifier.py)

```python
class UncertaintyType(str, Enum):
    DATA        = "DATA_UNCERTAINTY"
    SCOPE       = "SCOPE_UNCERTAINTY"
    UNCLASSIFIED = "UNCLASSIFIED"

@dataclass
class UncertaintyClassification:
    consumer:   str
    unc_type:   UncertaintyType = UncertaintyType.UNCLASSIFIED
    reason:     str = ""
    confidence: float = 0.0
```

The secondary prompt receives `consumer`, `verdict`, `confidence`, `reasoning`
and returns `TYPE: DATA_UNCERTAINTY` or `TYPE: SCOPE_UNCERTAINTY` on a single
line, followed by `REASON: <sentence>`.

`_parse_classification()` uses `re.search` — handles leading preamble, trailing
text, and case variations. Returns `UNCLASSIFIED` if no TYPE line is found.

`classify_all_low_confidence(results, threshold, llm)`:
```python
low = [r for r in results if r.confidence < threshold]
tasks = [classify_uncertainty(r, llm) for r in low]
out = await asyncio.gather(*tasks, return_exceptions=True)
```

---

## Step 2 — Store Schema (calibration/store.py)

```sql
CREATE TABLE IF NOT EXISTS uncertainty_classifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    consumer      TEXT    NOT NULL,
    unc_type      TEXT    NOT NULL DEFAULT 'UNCLASSIFIED',
    reason        TEXT    NOT NULL DEFAULT '',
    confidence    REAL    NOT NULL DEFAULT 0.0,
    classified_at TEXT    NOT NULL,
    UNIQUE(run_id, consumer)
)
```

`record_uncertainty_classifications(run_id, dict[str, UncertaintyClassification])`:
- `INSERT OR REPLACE` (idempotent on re-run)
- Returns count of rows inserted

`get_uncertainty_source_stats()`:
- Aggregate by `unc_type` → totals dict
- Aggregate by `(consumer, unc_type)` → per_consumer breakdown
- High SCOPE count for a specific consumer → systematic coverage gap signal

---

## Step 3 — Orchestrator Node (pipeline/orchestrator.py)

```python
async def classify_uncertainty_node(state):
    results = state.get("adjusted_compliance_results") or state.get("compliance_results", [])
    result_objs = [_Result(r["consumer"], r["verdict"], r["confidence"], r["reasoning"])
                   for r in results if isinstance(r, dict)]
    try:
        llm = _default_llm()
        classifications = await classify_all_low_confidence(result_objs, UNCERTAINTY_THRESHOLD, llm)
    except Exception:
        return {"uncertainty_classifications": None}
    # persist (best-effort)
    ...
    return {"uncertainty_classifications": {c: cl.to_dict() for c, cl in classifications.items()}}
```

Graph wiring:
```
phase3 → aggregate_confidence → classify_uncertainty → hitl_check
```

---

## Step 4 — HITL Enrichment (pipeline/hitl.py)

```python
def _format_classification_summary(classifications: dict) -> str:
    counts = {}
    for c in classifications.values():
        counts[c.get("type", "UNCLASSIFIED")] += 1
    ...  # returns "1×DATA, 1×SCOPE"

# In cross_repo_hitl_check:
unc_classifications = state.get("uncertainty_classifications") or {}
unc_summary = _format_classification_summary(unc_classifications)
hitl_reason = f"... >= {unc_desc}. Source: {unc_summary}. ..."

# In cross_repo_human_review interrupt:
"uncertainty_classifications": state.get("uncertainty_classifications") or {}
```

---

## Step 5 — API (api/webhook.py)

```python
@app.get("/calibration/uncertainty-sources")
async def uncertainty_sources():
    async with CalibrationStore(CALIBRATION_DB) as store:
        return await store.get_uncertainty_source_stats()
```

A high SCOPE count for a specific consumer is actionable: it tells the team
which consumer's technology stack needs a dedicated sub-agent or prompt extension.

---

## Tests (tests/unit/test_uncertainty_classifier.py)

| Test | Covers |
|------|--------|
| `test_parses_data_uncertainty` | TYPE regex → DATA |
| `test_parses_scope_uncertainty` | TYPE regex → SCOPE |
| `test_case_insensitive_type` | lowercase type matched |
| `test_missing_type_returns_unclassified` | fallback |
| `test_empty_response_returns_unclassified` | empty string |
| `test_missing_reason_still_classified` | type without reason |
| `test_extra_preamble_ignored` | leading text before TYPE |
| `test_calls_llm_and_returns_classification` | LLM called once |
| `test_llm_error_returns_unclassified` | exception → UNCLASSIFIED |
| `test_scope_classification_returned` | SCOPE path |
| `test_consumer_and_confidence_preserved` | field propagation |
| `test_only_classifies_below_threshold` | high-confidence excluded |
| `test_empty_when_all_above_threshold` | no low-confidence → empty |
| `test_empty_results_returns_empty` | empty input |
| `test_multiple_consumers_classified_concurrently` | gather path |
| `test_individual_llm_failure_does_not_abort_others` | partial failure |
| `test_has_required_keys` | to_dict keys |
| `test_type_is_string_value` | enum serialisation |
| `test_unclassified_type_string` | default type |
| `test_record_and_retrieve_stats` | store roundtrip |
| `test_upsert_on_duplicate` | INSERT OR REPLACE |
| `test_per_consumer_breakdown` | per_consumer stats |
| `test_empty_store_returns_zero` | empty store |

---

## Estimated Effort

| Task | Effort |
|------|--------|
| `pipeline/uncertainty_classifier.py` | 1.5 hours |
| Store schema + two methods | 30 min |
| Orchestrator node + graph wiring | 45 min |
| HITL enrichment | 30 min |
| API endpoint | 15 min |
| Tests (23 cases) | 1.5 hours |
| Feature doc + impl plan | 1 hour |
| **Total** | **~6 hours** |
