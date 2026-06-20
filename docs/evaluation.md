# Task 3 — Evaluation Framework: Implementation Requirements

> **Purpose:** Specifies the evaluation harness for the contract intelligence pipeline. It
> implements a **two-layer eval**: (1) a small, human-verified **ground-truth set** that anchors
> precision and catches gross failures, and (2) an **LLM-as-judge** layer that scales to outputs
> where manual review is not possible. The ground truth is not the eval — it is the thing that
> *calibrates and legitimizes the judge*. The judge is built to run across the full corpus and,
> in production, across live output; for the PoC it is run on a small sample. The same metric and
> judge code path is reused for production monitoring.
>
> **Dependencies (read / call — do not modify except where noted):**
> - `src/llm_client.py` (Task 2.3) — `call_llm(prompt, task, max_tokens)`, `MODEL_CONFIG["judge"]`
>   (`claude-sonnet-4-5`), `LLMCallError`, `get_token_totals()`, `reset_token_counters()`.
>   **Required small change:** extend `call_llm` with an optional `temperature: float | None = None`
>   passed through to `messages.create` only when not `None` (default behavior unchanged). The judge
>   calls pass `temperature=0` for reproducibility. This mirrors Task 2.7 extending `MODEL_CONFIG`.
> - `data/contracts.db` (Task 2.5) — read-only via `sqlite3` `mode=ro` or `pd.read_sql_query`. Source
>   of predicted field values and predicted `doc_type`. Columns per the Task 2.5 DDL.
> - `data/parse_cache.json` (Task 2.7 §8.1) — source document text keyed by filename; the judge and
>   the labeling helper read document text from here (not by re-parsing PDFs).
> - `src/build_vector_store.py` (Task 2.6) — `query_index(query_text, n_results, where, chroma_path)`
>   for the retrieval sanity check and for obtaining retrieved context to feed the chat judge.
> - `src/chat_router.py` (Task 2.7) — `classify_intent`, `answer_structured`, `answer_semantic`.
>   The eval calls these directly (no Streamlit). See §6 for the return-shape contract the eval relies
>   on; if the current functions return render-oriented objects, add a thin structured return.
> - `extraction_schema.md` — field definitions, nullability, and the §6 Field-to-Document-Type
>   **coverage matrix** (E / P / N). The coverage matrix drives the labeling skeleton and the
>   null-by-design logic in scoring.
> - `src/document_classifier.py` (Task 2.2) — `classify_document(parse_result)`, used only as a
>   fallback to obtain a predicted `doc_type` for a labeled document absent from `contracts.db`.
>
> **Downstream consumers:** Phase 4.2 (dedicated eval-findings slide — `outputs/eval_report.md` is
> its source), Phase 5 (walkthrough defense — "proven vs. assumed"), production monitoring (§8).

---

## 1. Design Principles

1. **Three surfaces, ~5 headline numbers.** Evaluate classification, extraction, and chat. Anchor on
   a small, memorable metric set, not a dashboard. Every headline number is either directly
   ground-truthed or produced by a judge that has been shown to agree with ground truth.
2. **Two layers with a bridge.** Ground truth = precision anchor (small, manual, high-trust).
   LLM-as-judge = scale layer (reference-free, runs where labels don't exist). The **bridge** is the
   judge-vs-ground-truth **agreement** number, which is what earns the right to lean on the judge.
3. **Build for scale, run a sample.** The judge is implemented batch-capable (full corpus, live
   output) with a `--sample N` parameter. The PoC run uses a small N. The full-corpus / live run is a
   parameter change, not new code — and is documented as the deliberate, costed next step.
4. **Label once, reuse three ways.** The ~6–8 fully field-labeled documents power extraction accuracy,
   classification accuracy, *and* the known-answer chat cases that target those same documents.
5. **Label independently of model output.** The labeling helper renders **source document text** next
   to **blank** gold fields. It must **not** pre-fill gold values from the pipeline's own output —
   that anchors the labeler to the model and destroys the eval's credibility. (The skeleton may pre-set
   null-by-design cells from the *schema's coverage matrix* — that anchors on the schema design, which
   is ours, not on model output.)
6. **Same metric code offline and online.** `eval/scoring.py` and the judge runners are pure,
   side-effect-free functions reused by both the offline eval and the production `monitoring_snapshot()`.

---

## 2. Module Layout

All new code lives under a single `eval/` package, consistent with the project's
separate-modules-for-separate-concerns pattern. No new logic is added to the pipeline modules
(except the one `call_llm` temperature parameter in §1 dependencies).

| Module | Responsibility |
|--------|----------------|
| `eval/ground_truth_extraction.json` | Human-labeled gold field values, one entry per labeled document. Committed; filled by the operator. |
| `eval/ground_truth_classification.json` | Human-labeled gold `doc_type`, one entry per document in the (larger) classification label set. Committed; filled by the operator. |
| `eval/chat_cases.json` | Authored chat Q&A cases (structured + semantic), with expected answers / target documents. Committed. |
| `eval/make_labeling_templates.py` | Labeling helper. Generates the blank skeletons above + renders source text for the operator to read. **No pre-fill from pipeline output.** |
| `eval/scoring.py` | Pure scoring: per-field match rules, four-bucket classification, and all metric computation. **No LLM, no I/O.** Shared by the eval and by monitoring. |
| `eval/judge.py` | The two judge prompts + batch-capable judge runners (`--sample N`, `temperature=0`). Reference-free. |
| `eval/run_eval.py` | Orchestrator. Loads ground truth, reads `contracts.db` + `parse_cache.json`, scores classification + extraction, runs the chat cases via `chat_router`, runs the sampled judge, computes calibration, writes the report. CLI entry point. |
| `eval/monitoring.py` | `monitoring_snapshot()` + baseline-compare + drift stub. Reuses `eval/scoring.py` and `eval/judge.py`. |

**Outputs written to `outputs/`:** `eval_report.md` (human-readable, the shareable deliverable),
`eval_results.json` (raw per-case results for reproducibility), `eval_judge_raw.json` (raw judge
responses), `eval_token_summary.txt` (judge token usage via `get_token_totals()`).

---

## 3. Ground-Truth Sets — Schemas & Labeling Workflow

### 3.1 Extraction ground truth (`ground_truth_extraction.json`)

The expensive label. Target **~6–8 documents**, selected to maximize labeled *populated* cells per
minute of operator time — weight toward field-rich types:

- ≥2 `fully_executed_agreement` (field-rich; the core)
- 1 `modification_amendment` (exercises `modification_financial_delta`, linkage)
- 1 `award_letter` **with bid tab** (exercises `total_contract_value`, `procurement_vehicle`)
- 1 `renewal_letter` (the 27% workhorse type)
- 1 **scanned / vision-extracted** document (proves the vision path is evaluated, not just text)
- 1 `vendor_disclosure_statement` (confirms the near-empty-by-design behavior — fast to label)

Schema (list of entries):

```jsonc
{
  "source_filename": "agreement_22847.pdf",
  "gold_doc_type": "fully_executed_agreement",
  "fields": {
    "contract_number": "22847",
    "vendor_name": "Johnson Controls Inc.",
    "doc_date": "2023-04-12",
    "total_contract_value": 125000.00,
    "contract_end_date": "2026-04-11",
    "termination_notice_days": 60,
    "auto_renewal_flag": false,
    "service_category": "facilities_maintenance",
    "procurement_vehicle": "direct_rfp",
    "parent_contract_number": null,          // null = asserted correctly-empty (a real gold value)
    "renewal_options": "3 × 1-year options"
    // ... every schema field key present
  },
  "notes": ""
}
```

**Sentinel discipline (important).** Each field value is one of:
- a concrete gold value,
- JSON `null` — the operator **asserts** this field is correctly empty for this document (a real gold
  value; participates in the hallucination-rate denominator), or
- the string `"__UNLABELED__"` — **not yet labeled**; the scorer **skips** the cell and emits a
  "ground truth incomplete" warning. `null` and `"__UNLABELED__"` must never be conflated.

### 3.2 Classification ground truth (`ground_truth_classification.json`)

The cheap label (`doc_type` is eyeball-fast; the operator does not read the whole document). Target
**~25 documents** for a credible accuracy number, stratified across all six types. The ~6–8 extraction
docs are a subset of this set.

```jsonc
{ "source_filename": "renewal_23159.pdf", "gold_doc_type": "renewal_letter" }
```

### 3.3 Chat cases (`chat_cases.json`)

Target **~8 cases**, deliberately overlapping the labeled documents so semantic cases have known
answers and known target documents.

```jsonc
{
  "id": "chat_03",
  "question": "What are the termination terms for contract 22847?",
  "expected_intent": "semantic",            // "structured" | "semantic" — also scores the router
  "eval_mode": "judge",                     // "deterministic" | "judge"
  "expected_answer": "60 days written notice for convenience",  // deterministic mode only
  "target_filename": "agreement_22847.pdf", // for retrieval sanity + known-answer (semantic cases)
  "target_contract_number": "22847",
  "notes": ""
}
```

- **Structured cases** (`eval_mode: "deterministic"`): the expected answer is a checkable fact
  ("top vendor by total value = Actalent, ≈ $2.3M"; "count = 14"). Scored exactly (§6).
- **Semantic cases** (`eval_mode: "judge"`): no clean ground truth → scored by the chat judge (§5.2).
  Where the target document is one of the labeled docs, the operator records the known answer in
  `notes` so the case doubles as judge-calibration evidence.

### 3.4 Labeling helper (`make_labeling_templates.py`)

CLI utility. For a configured (or stratified auto-selected) list of filenames, it:

1. Reads `data/parse_cache.json` and writes each document's source text to
   `eval/labeling/<filename>.txt` for the operator to read.
2. Generates the **blank** skeleton for `ground_truth_extraction.json`: every schema field key present;
   coverage-matrix **N** cells (null-by-design for that `doc_type`) pre-set to `null`; coverage-matrix
   **E** / **P** cells set to `"__UNLABELED__"`. **No values are read from `contracts.db` or any
   pipeline output.**
3. Generates the `ground_truth_classification.json` skeleton (filename + `"__UNLABELED__"`), optionally
   with the first ~400 chars of page 1 inline as a labeling aid.

```
python -m eval.make_labeling_templates --extraction-docs <f1.pdf> <f2.pdf> ...
python -m eval.make_labeling_templates --classification-sample 25 --stratify
```

The operator then fills the skeletons by reading the `.txt` files. This manual step is the only
human-in-the-loop work in the eval.

---

## 4. Scoring Rules (`eval/scoring.py`)

Pure functions. Given a gold field value, a predicted value (from `contracts.db`), and the field name,
classify each cell into exactly one **bucket**:

| Bucket | Condition |
|--------|-----------|
| `correct` | gold non-null, pred non-null, values match per the field's match rule |
| `wrong_value` | gold non-null, pred non-null, values do **not** match |
| `missed` | gold non-null, pred is null |
| `hallucinated` | gold **null**, pred non-null |
| `true_negative` | gold null, pred null (correctly empty by design) |
| `skipped` | gold is `"__UNLABELED__"` (excluded + warned) |

### 4.1 Per-field match rules

| Field(s) | Rule |
|----------|------|
| enums (`doc_type`, `service_category`, `procurement_vehicle`, `price_escalator_terms`) | exact match after trim/lowercase |
| booleans (`auto_renewal_flag`, `insurance_requirements_flag`) | exact (0/1) |
| `termination_notice_days` | exact integer |
| `total_contract_value`, `modification_financial_delta` | numeric match within `abs(diff) ≤ max($1, 0.5%)` (absorbs rounding) |
| dates (`doc_date`, `contract_start_date`, `contract_end_date`) | parse to ISO `YYYY-MM-DD`, exact date equality |
| `contract_number`, `parent_contract_number` | exact after stripping surrounding whitespace |
| `vendor_name` | normalize (lowercase, strip legal suffixes `inc/llc/corp/co/ltd`, strip punctuation, collapse whitespace), then token-sort ratio ≥ 0.90 → `correct`, else `wrong_value`. Deterministic; **no LLM**. |
| `renewal_options` | normalize to `(count, unit)` pattern (e.g. `3 × 1-year` → `(3, "1-year")`); if both normalize cleanly, compare normalized; if either fails to normalize, record `review` and let the judge resolve equivalence. |

### 4.2 Headline extraction metrics

Computed over **extracted (non-inferred)** fields only; inferred fields are reported separately (§4.3).

- **Field accuracy** = `correct / (correct + wrong_value + missed)` over gold-populated cells.
  → *"When a field should have a value, how often is it right."*
- **Hallucination rate** = `hallucinated / (count of gold-null cells)`.
  → *"How often the model invents a value that isn't in the document."* (The headline pair.)

Report each **overall**, **by field group** (A–E), and **by `doc_type`**. Also emit, as the supporting
diagnostic (not headline): the full four-bucket breakdown and **null-rate by doc type** (which doubles
as the verification signal for the in-flight `contract_end_date` derivation patch).

### 4.3 Inferred fields

`service_category`, `auto_renewal_flag`, `price_escalator_terms` are reported on a **separate accuracy
line**, never folded into the headline extracted-field accuracy. For `service_category` specifically,
in addition to exact-match, run a judge "defensible vs. wrong" pass (a reasonable category disagreement
is not a hard error) and report both the strict and defensible accuracy.

### 4.4 Classification metric

- **Classification accuracy** = `correct gold_doc_type / total labeled` over the ~25-doc set.
- Diagnostic: 6×6 confusion matrix.
- Predicted `doc_type` is read from `contracts.db` joined on `source_filename`; if a labeled doc is
  absent (extraction failed), fall back to `classify_document(parse_result)` from the parse cache so
  classification is evaluated independently of extraction success.

---

## 5. LLM-as-Judge (`eval/judge.py`)

All judge calls use `call_llm(..., task="judge", temperature=0)`. Judges are **reference-free** (they
read source text / retrieved context, not gold answers), which is exactly why they generalize beyond
the labeled set. Both runners are batch-capable and accept `sample_n` (CLI `--sample N`); the PoC
invokes them with a small N (§7). Raw judge JSON is written to `outputs/eval_judge_raw.json`.

### 5.1 Extraction faithfulness judge

Input per document: `(source_text, doc_type, extracted_fields_dict)`. Source text may be truncated to a
sane character budget (e.g. first ~20k chars; note truncation in the output). Output (strict JSON):

```jsonc
{
  "per_field": {
    "total_contract_value": { "supported": true,  "reason": "..." },
    "contract_end_date":    { "supported": false, "reason": "value not found in text" }
    // one entry per populated field
  },
  "doc_faithfulness_score": 4,   // 1–5 overall, rubric-anchored
  "summary": "..."
}
```

Per-field `supported` flags drive **calibration** (§5.3). The 1–5 `doc_faithfulness_score` drives
**drift monitoring** (§8). Prompt instructs: judge only whether each value is *supported by the source
text*; do not penalize null-by-design fields; return raw JSON only (no prose, no fences).

### 5.2 Chat answer judge

Input per case: `(question, retrieved_context_chunks, generated_answer)`. Retrieved context comes from
`answer_semantic`'s return (preferred) or a direct `query_index` call. Output (strict JSON):

```jsonc
{
  "faithfulness": 5,   // 1–5: answer grounded in retrieved context, nothing invented
  "relevance":   4,    // 1–5: answer actually addresses the question
  "faithfulness_reason": "...",
  "relevance_reason": "..."
}
```

Headline reporting uses mean faithfulness, mean relevance, and a **pass rate** at threshold ≥ 4.

### 5.3 Judge calibration (the bridge — first-class output)

On the labeled documents (where ground truth exists), compare the extraction judge's per-field
`supported` verdict against the §4 ground-truth bucket:

- judge `supported = false` should align with ground-truth `wrong_value` / `missed` / `hallucinated`;
- judge `supported = true` should align with ground-truth `correct`.

Report **judge–ground-truth agreement** (fraction of populated fields where the judge's
supported/unsupported matches ground truth's correct/incorrect) and **enumerate the disagreements**
(the interesting cases). This single number is what justifies leaning on the judge where no ground
truth exists. Do the analogous check for the chat judge on the semantic cases that have a known answer.

---

## 6. Chat Evaluation Flow (`run_eval.py`)

For each entry in `chat_cases.json`:

1. **Router check** — call `classify_intent(question)`; record whether it matches `expected_intent`.
   Report router accuracy as a small supporting number.
2. **Structured (`eval_mode: "deterministic"`)** — call `answer_structured(question)`. From its return
   the eval needs: the generated **SQL**, the result **DataFrame**, and the **answer payload**. Check
   the answer against `expected_answer` (exact for scalars; set/order-insensitive for small result
   sets). Record `correct` / `incorrect` and capture the SQL for the report.
3. **Semantic (`eval_mode: "judge"`)** — call `answer_semantic(question)`. From its return the eval
   needs: the **answer text** and the **sources list** (filename + contract_number) and, if available,
   the **retrieved chunks**. Then:
   - **Retrieval sanity check** (lightweight, near-free): is `target_filename` /
     `target_contract_number` present in the answer's sources? Report hit rate across semantic cases.
   - **Chat judge** (§5.2) on `(question, retrieved_context, answer_text)`.

**Return-shape contract.** The eval depends on `answer_structured` returning `{sql, dataframe,
answer}` and `answer_semantic` returning `{answer, sources, retrieved_chunks}` (names illustrative).
If the live `chat_router` functions currently return render-oriented objects only, add a thin
structured return or accessor — do not duplicate routing logic in the eval.

---

## 7. PoC Run Configuration

`run_eval.py` executes, in order: classification scoring → extraction scoring → chat cases → sampled
judge → calibration → report. The judge sample is **pointed at the ground-truth-overlap documents by
default**, so the sampled run *is* the calibration evidence (not a throwaway demo). Optionally include
2–3 unlabeled documents (`--judge-extra-unlabeled 3`) to demonstrate the judge operating where no
ground truth exists — near-zero marginal cost.

```
python -m eval.run_eval                       # full eval, default judge sample = GT-overlap docs
python -m eval.run_eval --judge-sample 8      # explicit sample size
python -m eval.run_eval --judge-extra-unlabeled 3
python -m eval.judge --sample 8               # judge layer standalone
```

Reset token counters at the start of the run; write `outputs/eval_token_summary.txt` from
`get_token_totals()` at the end so judge cost is transparent.

---

## 8. Production Monitoring (`eval/monitoring.py`)

The production story is *the same code with live inputs*, not a second system. `monitoring_snapshot()`
takes a batch of records — `(source_text, doc_type, extracted_fields)` for extraction, and/or
`(question, context, answer)` for chat — and returns the same metric set the offline eval computes,
via `eval/scoring.py` and `eval/judge.py`:

- mean judge faithfulness (extraction + chat), and the score **distribution** (for drift, not just the mean);
- null-rate by `doc_type`; hallucination flags from the judge;
- classification confidence distribution; scanned-document rate; extraction failure rate.

Provide a baseline-compare that flags drift when a snapshot deviates from a stored baseline beyond
configurable thresholds (judge-score mean drop, null-rate spike, scanned-rate spike,
classification-confidence drop, failure-rate rise) — these are the signals for **prompt drift,
document-format change, and clause-language change** called out in the assignment. **Scope for PoC:**
implement `monitoring_snapshot()` + the baseline-compare and **log** alerts; do not build scheduling,
storage, or a real alerting channel. The point is to demonstrate that monitoring is a thin wrapper over
the eval, runnable on a live sample on a schedule.

---

## 9. Reporting (`outputs/eval_report.md`)

The shareable deliverable (source for the Phase 4.2 eval slide). Sections:

1. **Headline scorecard** — classification accuracy; extraction field accuracy + hallucination rate;
   structured-chat correctness; RAG faithfulness/relevance; **judge–ground-truth agreement**.
2. **Extraction detail** — four-bucket breakdown and null-rate by `doc_type`; inferred-field accuracy
   (strict + defensible) reported separately.
3. **Classification detail** — confusion matrix.
4. **Chat detail** — per-case table (intent match, deterministic result or judge scores, retrieval hit).
5. **Judge calibration** — agreement number + the enumerated disagreements.
6. **Proven vs. assumed** — what the numbers establish, the ground-truth set size and its limits, and
   the explicit note that the judge is built full-corpus-capable but run on a sample for the PoC, with
   the full run + live monitoring as the costed next step.

`eval_results.json` and `eval_judge_raw.json` carry the raw per-case detail for reproducibility.

---

## 10. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Eval shape | Two layers: ground truth (anchor) + LLM-as-judge (scale), bridged by a calibration number | The calibration is what makes leaning on the judge legitimate rather than hand-waving; directly answers "how do you know the judge is any good?" |
| Headline metric count | ~5 numbers across 3 surfaces | Memorable walkthrough story (gate → core → interface); rigor lives in the diagnostics underneath |
| Extraction headline | Field accuracy + hallucination rate, over gold-populated cells only | Sidesteps the null-by-design dilution; the right pair for contracts, where a fabricated term is the dangerous failure mode |
| Null vs. unlabeled | `null` is a real gold value; `"__UNLABELED__"` is skipped | Without this distinction, hallucination rate and null-rate metrics are meaningless |
| Labeling | Render source text against blank fields; no pre-fill from model output | Independent labeling is the defensible posture; pre-fill induces confirmation bias |
| doc_type labels decoupled | ~25 cheap `doc_type` labels vs. ~6–8 expensive field labels | `doc_type` is eyeball-fast; a credible classification number costs little extra operator time |
| Inferred fields | Reported separately; `service_category` judged "defensible vs. wrong" | A reasonable category disagreement isn't a hard error; matches the schema's stated treatment |
| Structured vs. semantic chat | Structured checked deterministically; semantic judged | Use exact checks where possible; reserve the judge precisely for unstructured output — a stronger justification than "judge everything" |
| Judge scope | Built full-corpus-capable (`--sample N`); PoC runs a small sample | Showcases the production capability without paying for 100+ calls or surfacing a backlog of findings mid-PoC; full run is a config flip |
| Judge determinism | `temperature=0` via a new optional `call_llm` param | Reproducible scores across runs; minimal, non-breaking change to `llm_client` |
| Monitoring | `monitoring_snapshot()` reuses `scoring.py` + `judge.py` | "Same code, live inputs" — monitoring is a wrapper over the eval, not a second system |
| Module placement | Standalone `eval/` package, CLI-runnable | Consistent with `build_vector_store --query` / `extractor --test`; no new logic in pipeline modules |

---

## 11. PoC Scope Boundaries (Not In Scope)

Document these in the README as production-hardening / roadmap items.

- **Full-corpus judge run.** Built and capable; run on a sample for the PoC. Turning it on across all
  ~100 docs (and live traffic) is a parameter change, deliberately deferred for cost and because the
  findings belong in a hardening phase.
- **Chunk-level retrieval relevance labeling.** Retrieval is sanity-checked via known-item source
  presence, not full precision/recall against labeled relevant chunks.
- **Inter-annotator agreement / multiple labelers.** Single labeler for the PoC ground truth.
- **Automated alerting + scheduling for monitoring.** Snapshot + baseline-compare + logged alerts only;
  no scheduler, no channel integration, no metric store.
- **Statistical confidence intervals on the metrics.** Sample sizes are small by design; numbers are
  directional anchors, disclosed as such.
- **Cost/latency benchmarking of the pipeline.** Token usage is logged; throughput/latency eval is out
  of scope.

---

## 12. Definition of Done

- `eval/` package exists with the modules in §2; `eval/scoring.py` contains no LLM calls and no I/O.
- `call_llm` accepts an optional `temperature` (default unchanged); judge calls pass `temperature=0`.
- `python -m eval.make_labeling_templates ...` writes `eval/labeling/<file>.txt` source dumps and blank
  `ground_truth_extraction.json` / `ground_truth_classification.json` skeletons, with coverage-matrix
  **N** cells pre-nulled and **E**/**P** cells set to `"__UNLABELED__"`, and **no** values sourced from
  pipeline output.
- With the ground-truth files filled, `python -m eval.run_eval` runs end-to-end and writes
  `outputs/eval_report.md`, `outputs/eval_results.json`, `outputs/eval_judge_raw.json`, and
  `outputs/eval_token_summary.txt`.
- Extraction scoring emits the four buckets, headline **field accuracy** + **hallucination rate**
  (overall / by group / by doc_type), null-rate by doc_type, and a separate inferred-field line; cells
  marked `"__UNLABELED__"` are skipped with a warning.
- Classification scoring emits accuracy + a 6×6 confusion matrix, using the DB prediction with a
  `classify_document` fallback for docs absent from the DB.
- Chat scoring: structured cases checked deterministically (SQL captured); semantic cases produce a
  retrieval-sanity hit flag + chat judge faithfulness/relevance; router accuracy reported.
- The judge runs at `temperature=0`, is batch-capable with `--sample N`, defaults its sample to the
  ground-truth-overlap docs, and supports `--judge-extra-unlabeled`.
- **Judge calibration** is reported: judge–ground-truth agreement for extraction (and chat where a
  known answer exists), with disagreements enumerated.
- `eval/monitoring.py` exposes `monitoring_snapshot(batch)` reusing `scoring.py` + `judge.py`, plus a
  baseline-compare that logs drift alerts; no scheduler/alert-channel built.
- `outputs/eval_report.md` contains the §9 sections including the explicit "proven vs. assumed" framing.