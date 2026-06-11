# Feature 9 — Uncertainty Source Classification

## Overview

Features 1–8 treat all low agent confidence as a single signal: the pipeline
does not know *why* an agent is uncertain, only *that* it is. This matters
because the appropriate reviewer response differs by cause:

| Uncertainty source | What it means | Reviewer action |
|-------------------|---------------|-----------------|
| **DATA_UNCERTAINTY** | The contract change evidence is genuinely ambiguous — a reasonable expert could reach either verdict | Examine the specific diff; exercise judgment on the edge case |
| **SCOPE_UNCERTAINTY** | The agent is outside its evaluation domain — the technology or usage pattern is unfamiliar | Escalate to a domain expert; flag the agent's coverage gap for improvement |

Feature 9 introduces a taxonomy with two categories and a secondary LLM
classification prompt that determines which applies for each low-confidence
consumer verdict.

---

## The Taxonomy

### DATA_UNCERTAINTY (aleatoric)

The uncertainty is intrinsic to the change, not to the agent's knowledge. The
contract evidence admits multiple valid interpretations; providing the agent
with more context or retrying would not reliably change the outcome. This maps
to **aleatoric uncertainty** in the uncertainty quantification literature
(Kendall & Gal 2017): irreducible noise in the observation.

Examples in the contract compliance domain:
- A field removal where some consumer code paths may handle the absent field
  gracefully (no-op) while others will throw
- A type narrowing (`int64` → `int32`) where the consumer's actual usage
  values never overflow, making the change safe in practice but breaking in
  specification
- A response schema change where the consumer ignores unknown fields — correct
  per the Postel principle but the contract does not encode this

**Reviewer guidance:** Look at the specific diff line-by-line. Check the
consumer's actual usage patterns. This is a judgment call that requires
domain knowledge of both sides.

### SCOPE_UNCERTAINTY (epistemic)

The agent lacks sufficient knowledge to assess this case confidently, regardless
of evidence quality. Providing more context about *this change* would not help —
the agent needs broader domain knowledge. This maps to **epistemic uncertainty**:
reducible if the agent's knowledge base is expanded.

Examples:
- Binary wire format changes (Protobuf field numbering, Avro schema evolution)
  where the agent was trained on JSON REST contracts
- Consumer-side framework internals (e.g., Spring's `@FeignClient` retry
  behaviour on 4xx responses)
- gRPC streaming semantics vs. unary RPC, where the agent evaluates only
  request/response schemas
- Database-specific query contract (e.g., stored procedure signature changes)

**Reviewer guidance:** Escalate to a domain specialist. File a coverage gap
issue against the agent — this is a known blind spot. Consider extending the
`COMPLIANCE_SYSTEM` prompt or adding a specialised sub-agent for this domain.

---

## Classification Method

### Secondary LLM Prompt

Classification is performed via a secondary prompt on the same LLM used for
the original compliance check. The prompt receives:
- Consumer name
- Original verdict and confidence
- The agent's existing `reasoning` field

It does **not** resend the full contract diff — the reasoning already summarises
the evidence. This means classification costs one small LLM call (reasoning is
typically 2–5 sentences) rather than a full re-analysis.

```
You are classifying the SOURCE of uncertainty in an API compatibility assessment.

An AI agent assessed contract compliance for consumer 'k11-payment-service' and
reported verdict=UNCERTAIN with confidence=0.31.

The agent's reasoning was:
"The removal of the 'discount_code' field from the response schema may or may not
 affect this consumer. The consumer's usage patterns show it reads this field
 in 3 of 8 observed request flows, but the field appears optional in 2 of those."

Classify the PRIMARY source of this low confidence as EXACTLY ONE of:
  DATA_UNCERTAINTY — evidence is genuinely ambiguous
  SCOPE_UNCERTAINTY — agent lacks domain knowledge

TYPE: DATA_UNCERTAINTY
REASON: The consumer's partial field usage makes the impact genuinely ambiguous.
```

### Trigger Condition

The classifier fires for every consumer result whose `confidence < UNCERTAINTY_THRESHOLD`
(default 0.35). All qualifying consumers in a run are classified concurrently
(one LLM call per consumer, gathered with `asyncio.gather`). Runs where no
consumer falls below threshold skip classification entirely — zero extra LLM calls.

### Fallback

A failed or malformed classification returns `UNCLASSIFIED` and is logged at
DEBUG. Classification failure never blocks the HITL gate or affects the verdict.
The `UNCLASSIFIED` category appears in aggregate stats but carries no
reviewer-action meaning.

---

## Pipeline Flow

```
phase3 (compliance checks)
  └─ compliance_results: [{consumer, verdict, confidence, reasoning}]

aggregate_confidence_node
  └─ uncertainty_score, uncertainty_verdict computed

classify_uncertainty_node          ← Feature 9
  ├─ confidence < UNCERTAINTY_THRESHOLD?
  │    └─ secondary LLM prompt per qualifying consumer (concurrent)
  ├─ results: {consumer: {type, reason, confidence}}
  ├─ persist to uncertainty_classifications table
  └─ state.uncertainty_classifications updated

hitl_check
  ├─ HITL reason now includes: "Source: 1×DATA, 1×SCOPE"
  └─ cross_repo_human_review interrupt includes:
       "uncertainty_classifications": {
         "k11-payment-service": {
           "type": "DATA_UNCERTAINTY",
           "reason": "...",
           "confidence": 0.31
         }
       }
```

---

## HITL Reviewer Experience

Before Feature 9, the HITL interrupt contained:
```
uncertainty_score: 0.42
uncertainty_verdict: HIGH
```

After Feature 9:
```
uncertainty_score: 0.42
uncertainty_verdict: HIGH
uncertainty_classifications: {
  "k11-payment-service": {
    "type": "DATA_UNCERTAINTY",
    "reason": "Field removal affects only some usage paths — consumer's
               actual usage is ambiguous",
    "confidence": 0.31
  },
  "k11-order-service": {
    "type": "SCOPE_UNCERTAINTY",
    "reason": "Consumer uses gRPC streaming which is outside the agent's
               REST contract evaluation domain",
    "confidence": 0.28
  }
}
```

The HITL reason string becomes:
```
"Low agent confidence — uncertainty_score=0.42 >= conformal threshold 0.32
 (α=0.10, FNR≤0.148, n_wrong=19). Source: 1×DATA, 1×SCOPE."
```

A reviewer seeing `SCOPE_UNCERTAINTY` for order-service knows immediately that
the right action is escalation, not closer examination of the diff.

---

## Taxonomy Validation

The two-category taxonomy is deliberately minimal. Adding more categories
(e.g., `CONTRACT_QUALITY_UNCERTAINTY` for incomplete specs,
`DEPENDENCY_UNCERTAINTY` for transitive consumers) would increase descriptive
power but reduce inter-rater reliability — reviewers would disagree more
often on which category applies. The DATA/SCOPE split maps cleanly onto the
aleatoric/epistemic split from the ML uncertainty literature and has a
natural action associated with each type.

**Inter-rater reliability design note:** The taxonomy can be validated by
presenting the same low-confidence reasoning to multiple raters and measuring
Cohen's κ. For paper evaluation, we report the distribution of classifications
in the controlled study and note cases where the category was debatable.

---

## API

### `GET /calibration/uncertainty-sources`

Returns aggregate counts across all classified runs.

```json
{
  "total_classified": 42,
  "totals": {
    "DATA_UNCERTAINTY":  28,
    "SCOPE_UNCERTAINTY": 11,
    "UNCLASSIFIED":       3
  },
  "per_consumer": {
    "k11-payment-service": {
      "DATA_UNCERTAINTY":  12,
      "SCOPE_UNCERTAINTY":  4
    },
    "k11-order-service": {
      "DATA_UNCERTAINTY":   8,
      "SCOPE_UNCERTAINTY":  7
    }
  }
}
```

A high SCOPE_UNCERTAINTY count for a specific consumer signals a systematic
agent coverage gap for that service's technology stack.

---

## Configuration

```
UNCERTAINTY_THRESHOLD=0.35    # classifies all consumers with confidence < 0.35
CALIBRATION_ENABLED=true      # false skips classification and DB write
LLM_PROVIDER=anthropic        # same LLM as compliance agents
LLM_MODEL=claude-sonnet-4-6
```

No new env vars are required — Feature 9 reuses the existing pipeline LLM
configuration.

---

## Files Changed

| File | Change |
|------|--------|
| `pipeline/uncertainty_classifier.py` | **New file.** `UncertaintyType`, `UncertaintyClassification`, `_parse_classification()`, `classify_uncertainty()`, `classify_all_low_confidence()` |
| `pipeline/orchestrator.py` | `classify_uncertainty_node`; wired between `aggregate_confidence` and `hitl_check` |
| `pipeline/hitl.py` | `_format_classification_summary()`; enriched HITL reason string and interrupt payload |
| `pipeline/state.py` | `uncertainty_classifications: Optional[dict]` field |
| `calibration/store.py` | `uncertainty_classifications` table schema; `record_uncertainty_classifications()`, `get_uncertainty_source_stats()` |
| `api/webhook.py` | `GET /calibration/uncertainty-sources` endpoint |
| `tests/unit/test_uncertainty_classifier.py` | 23 unit tests |

---

## Paper Section Placement

Feature 9 belongs in **Section 3 — System Design** as
*"3.9 Uncertainty Source Classification"* or
*"3.9 A Taxonomy of Agent Uncertainty"*.

**Primary theoretical contribution:** The first taxonomy of uncertainty types
specifically for AI-based API contract compliance checking, operationalised
without labeled training data (secondary prompt classification). This has no
direct precedent in the existing contract testing or API compatibility literature.

**Connection to existing literature:**
- Kendall & Gal (2017) — *What Uncertainties Do We Need in Bayesian Deep
  Learning for Computer Vision?* — establishes aleatoric/epistemic split in DL;
  Feature 9 applies this framing to LLM-based structured QA
- Hullermeier & Waegeman (2021) — *Aleatoric and Epistemic Uncertainty in
  Machine Learning: An Introduction to Concepts and Methods* — provides
  formal definitions that ground the taxonomy
- The secondary-prompt classification approach is related to introspective
  uncertainty elicitation (Kadavath et al. 2022: "Language Models
  (Mostly) Know What They Know") — asking the model to reflect on its own
  knowledge state

**Key evaluation claim (Section 4):** What fraction of uncertain verdicts in
the controlled study are DATA vs SCOPE? Hypothesis: s06 (enum UNCERTAIN case)
produces SCOPE_UNCERTAINTY because the agents were not prompted to reason about
semantic invariants of enum values — a well-defined knowledge gap. s01/s03
borderline scenarios (if any) would produce DATA_UNCERTAINTY.
