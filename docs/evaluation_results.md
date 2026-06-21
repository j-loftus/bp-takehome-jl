# Evaluation Results — Task 3 Write-Up

> **Status:** Complete. This document tells the full story of the evaluation framework's design,
> implementation, manual labeling process, and the actual results from running it — for use in
> the Phase 4 findings slide and as a standalone record of what was proven vs. assumed.
>
> Source spec: `docs/evaluation.md`. Source code: `eval/`. Raw outputs: `outputs/eval_report.md`,
> `eval_results.json`, `eval_judge_raw.json`, `eval_token_summary.txt`.

---

## 1. Why this framework, and what it had to prove

The contract intelligence pipeline (parsing → classification → extraction → SQLite + ChromaDB →
chat) produces structured fields and chat answers from LLM calls at every stage. Before trusting
any of it in front of Berkshire, we needed a way to answer three questions with evidence rather
than assertion:

1. **Is the document classification correct?** Wrong `doc_type` cascades into the wrong extraction
   prompt and wrong null-pattern expectations downstream.
2. **Is the extracted structured data trustworthy?** Specifically: when a field should have a
   value, how often is it right (**field accuracy**), and how often does the model invent a value
   that isn't actually in the document (**hallucination rate**) — the dangerous failure mode for
   contract data feeding financial/legal decisions.
3. **Is the chat interface giving grounded answers?** Both the deterministic SQL path and the RAG
   path needed a way to be checked without a human re-reading every contract for every question.

A pure human-labeled test set answers these questions with high trust but doesn't scale — we
can't hand-label hundreds of documents or every chat query in production. A pure LLM-judge
approach scales but is unfalsifiable on its own — "trust the judge" isn't a defensible answer in
a walkthrough. The design had to bridge both.

---

## 2. Design we landed on

**Two layers, bridged by a calibration number.**

- **Layer 1 — Ground truth (the anchor).** A small, manually labeled set: every populated field on
  6 documents, the `doc_type` on 24 documents, and 8 authored chat Q&A cases. Small, high-trust,
  catches gross failures. Manually verified directly against source PDFs — not pre-filled from
  the pipeline's own output, so it can't be circular.
- **Layer 2 — LLM-as-judge (the scale layer).** Two reference-free judges — an extraction
  faithfulness judge and a chat answer judge — that read source text (or page images for scanned
  docs) and the model's output, and score whether the output is *supported by the source*. No
  gold answer required, so this generalizes to documents and queries with no ground truth at all.
- **The bridge — judge calibration.** On the documents where both ground truth and a judge verdict
  exist, we compare the judge's `supported`/`unsupported` call against the ground-truth
  correct/incorrect bucket. The resulting **agreement percentage** is the number that licenses
  leaning on the judge everywhere else, including in production monitoring.

**Other decisions baked into the design:**

| Decision | Choice | Why |
|---|---|---|
| Headline metric count | ~5 numbers across 3 surfaces (classification, extraction, chat) | Memorable scorecard; the diagnostics underneath carry the rigor |
| Extraction headline pair | Field accuracy + hallucination rate, over **gold-populated cells only** | Avoids dilution from null-by-design fields; the right pair for contracts, where a fabricated term is the dangerous failure mode |
| Null vs. unlabeled | JSON `null` = a real, asserted gold value ("I checked, it's correctly empty"); the string `"__UNLABELED__"` = not yet labeled, skipped with a warning | Without this distinction, hallucination rate and null-rate metrics are meaningless |
| Labeling independence | The labeling helper renders only source text next to blank fields — never pre-fills from the pipeline's own predicted values | Pre-filling from model output would anchor the labeler to the model and destroy the eval's credibility |
| Inferred fields | `service_category`, `auto_renewal_flag`, `price_escalator_terms` scored on a separate accuracy line, never folded into the extracted-field headline | These are judgment calls the LLM makes, not literal extractions — a reasonable disagreement isn't a hard error |
| Structured vs. semantic chat | Structured (SQL) checked deterministically against a verified expected answer; semantic (RAG) scored by the chat judge | Use exact checks where they're possible; reserve the judge for genuinely unstructured output |
| Judge scope | Built batch-capable (`--sample N`, full-corpus-capable) but run on a small sample for the PoC | Demonstrates production capability without paying for (or surfacing a backlog from) a 100+ document run mid-PoC |
| Scanned documents | Judged via a **vision path** (page images + `call_llm_with_images`) rather than skipped | 3 of our 6 labeled extraction docs turned out to be scanned — skipping them would have hidden the vision pipeline's only direct quality check |
| Monitoring | `eval/monitoring.py`'s `monitoring_snapshot()` reuses `scoring.py` + `judge.py` verbatim | "Same code, live inputs" — monitoring is a thin wrapper over the eval, not a second system to maintain |

---

## 3. What we built

All new code lives in a standalone `eval/` package (≈1,400 lines), consistent with the project's
separate-modules-for-separate-concerns pattern. No logic was added to the pipeline modules except
one additive parameter (see §3.6).

| Module | Responsibility |
|---|---|
| `eval/scoring.py` | Pure functions, no LLM calls, no I/O. Six-bucket field classification (`correct` / `wrong_value` / `missed` / `hallucinated` / `true_negative` / `skipped`, plus a `review` bucket for ambiguous `renewal_options` text), per-field match rules, metric aggregation, classification accuracy + confusion matrix. Unit-tested against 26 synthetic cases before any real label existed. |
| `eval/make_labeling_templates.py` | Labeling helper. Generates blank ground-truth skeletons (null-by-design cells pre-nulled from the schema's own coverage matrix, never from model output) and dumps source text to `eval/labeling/*.txt` for the operator to read. Flags scanned documents with no cached text instead of silently leaving them blank. |
| `eval/judge.py` | The two judge prompts plus batch-capable runners. Extraction judge branches to a vision call (`call_llm_with_images` + `extract_page_images`) when the document is scanned. Both judges run at `temperature=0` for reproducibility. Includes the calibration computation that bridges judge verdicts to ground-truth buckets. |
| `eval/run_eval.py` | Orchestrator / CLI entry point. Runs classification scoring → extraction scoring → chat cases → sampled judge → calibration → report, in that order. Writes all four `outputs/` artifacts. |
| `eval/monitoring.py` | `monitoring_snapshot()` + `compare_to_baseline()` for production use — reuses `scoring.py`/`judge.py` on a live batch, with logged drift alerts (no scheduler or alert channel — out of PoC scope). |

### 3.1 Per-field match rules (`eval/scoring.py`)

| Field(s) | Rule |
|---|---|
| Enums (`doc_type`, `service_category`, `procurement_vehicle`, `price_escalator_terms`) | Exact match after trim/lowercase |
| Booleans (`auto_renewal_flag`, `insurance_requirements_flag`) | Exact (0/1) |
| `termination_notice_days` | Exact integer |
| `total_contract_value`, `modification_financial_delta` | Numeric match within `abs(diff) ≤ max($1, 0.5%)` |
| Dates (`doc_date`, `contract_start_date`, `contract_end_date`) | Parsed to ISO date, exact equality |
| `contract_number`, `parent_contract_number` | Exact after whitespace trim |
| `vendor_name` | Normalized (lowercase, strip legal suffixes, punctuation, whitespace), then `rapidfuzz` token-sort ratio ≥ 90 |
| `county_department` | Same normalize-then-fuzzy approach, threshold ≥ 85 — added mid-review after the original rule set raised an unhandled-field error; see §6.4 |
| `renewal_options` | Normalized to a `(count, unit)` pattern via regex (e.g. "3 x 1-year" → `(3, "1-year")`); if either side fails to normalize, the cell is deferred to the judge as a `review` case rather than scored as wrong |

### 3.2 The required upstream change

`src/llm_client.py`'s `call_llm()` and `call_llm_with_images()` both gained an optional
`temperature: float | None = None` parameter, passed through to the Anthropic API call only when
not `None` — default behavior is unchanged for every existing caller. The judge calls pass
`temperature=0` for reproducible scores. This was the one sanctioned change to pipeline code,
mirroring how Task 2.7 extended `MODEL_CONFIG`.

A second small additive change: `src/chat_router.py`'s `answer_semantic()` now also returns
`retrieved_chunks` in its result dict (previously `answer`/`sources`/`low_confidence`/`error`
only) — needed so the chat judge has the actual retrieved context to score faithfulness against,
without the eval re-implementing retrieval itself.

---

## 4. Manual labeling — what we labeled and how we decided it

This was the one part of the framework that couldn't be automated. Three ground-truth files, all
in `eval/`:

### 4.1 Extraction ground truth — 6 documents, every populated field

Selected to maximize labeled cells per minute of effort while covering every document type and
both extraction paths (text and vision):

| Document | doc_type | Why selected |
|---|---|---|
| `19028_Fully_Executed_Agreement.pdf` | fully_executed_agreement | Large value ($2.65M), field-rich; turned out to be scanned |
| `16069_Agreement_fully_executed.pdf` | fully_executed_agreement | Scanned/vision path |
| `2023_10_05_Contract_22143_Modification_1_EXECUTED.pdf` | modification_amendment | Has `modification_financial_delta` + `parent_contract_number` populated; turned out to be scanned |
| `23036_Award_Letter.pdf` | award_letter | Populated `total_contract_value`/`procurement_vehicle`; substituted for the "award letter with bid tab" criterion — no literal bid-tab document exists in the sampled corpus |
| `16069_Renewal_Letter_20_21.pdf` | renewal_letter | The ~27% workhorse type |
| `18018_Vendor_Disclosure_Form__signed.pdf` | vendor_disclosure_statement | Confirms the near-empty-by-design behavior |

**A correction made mid-labeling:** the doc selection originally flagged only one of these six as
scanned. Two more (`19028_Fully_Executed_Agreement.pdf`, 35 pages, and the 22143 modification, 2
pages) turned out to have no cached text either. Rather than re-pick documents, we treated this as
a better test of the system — half the labeled set ended up exercising the vision path instead of
one-sixth, which is a stronger demonstration of vision-judge coverage than originally planned. For
the 35-page document, a vision transcription was generated as a reading aid (not fed into any
label directly) to make manual labeling tractable.

**Notable judgment calls made together while labeling** (each one is a real rule worth keeping for
future labeling, not a one-off):

- **Dates** — always normalized to ISO `YYYY-MM-DD`, never the document's raw format, because
  `scoring.py`'s date match rule calls `date.fromisoformat()` directly.
- **Money fields** — bare JSON numbers, never `"$X,XXX.XX"` strings — `float()` on a string with
  `$`/commas throws.
- **`modification_financial_delta` vs. `total_contract_value`** — the delta field captures only the
  *net change* introduced by a modification; `total_contract_value` stays null-by-design on
  modification documents even when the new total is stated in the text, because the running total
  is meant to be derived (`original + Σ deltas`) at query time, not duplicated per document.
- **Execution date with two signatures** — when a fully executed agreement has two different
  signature dates (one per party), `doc_date` is the *later* one — the agreement isn't binding
  until the last party signs.
- **`contract_start_date`/`contract_end_date` on modifications** — only populate when the
  modification itself changes the term, not when an unrelated recital happens to mention the
  original agreement's dates as background.
- **`auto_renewal_flag`** — `null` is reserved for genuine ambiguity, not for "explicitly stated as
  discretionary." A clause saying the County "reserves the right to renew" is an unambiguous
  `False` (0), not a null.
- **`service_category`** (an inferred field) — populate it even on documents where the coverage
  matrix marks it `Expected` but the literal category word doesn't appear in the text; infer from
  the nature of the work described. Don't default to null just because a modification doesn't
  restate the original contract's purpose.
- **`parent_contract_number` on the originating document** — a fully executed agreement *is* the
  parent; this field should be null-by-design there, not self-referencing.

### 4.2 Classification ground truth — 24 documents, stratified

Auto-sampled proportionally to the actual corpus mix (vendor_disclosure_statement 34,
fully_executed 20, modification 12, other 11, award 9, renewal 7 out of 93 classified documents)
via `make_labeling_templates.py --classification-sample 25 --stratify`. One borderline case worth
recording as a labeling precedent: a "60-day extension" letter was labeled `other` per the
classification spec's own explicit example list (`docs/doc_classification.md` enumerates 60-day
extensions, price-increase letters, and bid documents as `other`), even though it modifies a
contract date — it doesn't carry the WHEREAS-recital/amendment structure that defines
`modification_amendment`.

**The stratified sample surfaced the corpus issue described in §6.1 before the eval even ran** —
9 of the 24 sampled documents predicted as `vendor_disclosure_statement` were, by filename and
content, clearly renewal letters.

### 4.3 Chat cases — 8 cases, 3 structured + 5 semantic

3 deterministic (count/value queries, hand-verified against `contracts.db` directly) and 5
semantic, each deliberately targeting one of the 6 extraction-labeled documents so the case
doubles as judge-calibration evidence. Each semantic case's "known answer" was recorded in its
`notes` field after reading the source text — including one fresh vision transcription for the
35-page scanned document, since no chat case had read that document's text before.

One deliberate omission: no semantic chat case targets a scanned document, because scanned
documents have no extracted text and are therefore absent from the RAG index — testing retrieval
against an un-indexed document would always fail for a reason that has nothing to do with answer
quality.

---

## 5. Execution

```bash
python -m eval.make_labeling_templates --extraction-docs <6 files> --classification-sample 25 --stratify
# manual labeling of the 3 ground-truth files
python -m eval.run_eval
```

`run_eval.py` ran classification scoring → extraction scoring → 8 chat cases (3 real SQL
generations + 5 real RAG retrievals/syntheses) → the sampled judge (defaulted to the 6
ground-truth-overlap documents, including a real vision-judge call on the scanned doc) →
calibration → report, end to end, with zero wiring failures. Total judge-layer token usage for
this run: 62,276 input / 4,641 output tokens (`outputs/eval_token_summary.txt`).

---

## 6. Results

### 6.1 Headline scorecard

| Metric | Value |
|---|---|
| Classification accuracy | **58.3%** (n=24) |
| Extraction field accuracy | **93.3%** |
| Extraction hallucination rate | **6.1%** |
| Structured chat correctness | **3/3** |
| RAG faithfulness (mean / pass-rate@4) | **5.00/5** / 100% |
| RAG relevance (mean) | **4.80/5** |
| **Judge–ground-truth agreement (extraction)** | **89.1%** (n=55 populated fields) |

### 6.2 Extraction detail

Four-bucket breakdown: `correct: 50, true_negative: 40, missed: 1, hallucinated: 2, wrong_value: 3`

By field group: Group A (spine) 95.2%, Group B (financial) 100%, Group C (term/renewal) 92.9%,
Group D (vendor/compliance) 90.0%, Group E (linkage) 66.7%.

By doc_type: fully_executed_agreement 96.2% (16.7% hallucination), modification_amendment 71.4%
(11.1% hallucination), award_letter 90.0%, renewal_letter 100%, vendor_disclosure_statement 100%.

Inferred-field accuracy (`service_category`, `auto_renewal_flag`, `price_escalator_terms`,
reported separately from the headline per design): **88.9%**.

### 6.3 Classification detail

The confusion matrix is the headline finding here:

```
gold renewal_letter        -> predicted renewal_letter: 2,  vendor_disclosure_statement: 9
gold fully_executed_agreement -> predicted fully_executed_agreement: 5, other: 1
gold award_letter           -> predicted award_letter: 2
gold modification_amendment -> predicted modification_amendment: 3
gold other                  -> predicted other: 2
```

### 6.4 Chat detail

Router accuracy 8/8. Retrieval sanity hit rate 4/5 (one semantic case missed retrieving its
target document — worth a closer look, not yet root-caused). All 3 structured/deterministic cases
correct, with the executed SQL captured in `eval_results.json` for inspection.

### 6.5 Judge calibration — the disagreements, and what each one means

Six disagreements out of 55 compared fields (89.1% agreement). One is a tooling artifact; two are
genuine, useful findings; the rest are a defensible inferred-field disagreement and an
over-eager inference by the judge.

| Document / field | What happened | Verdict |
|---|---|---|
| `county_department` on the award letter | Gold: `"LAKE COUNTY DEPARTMENT OF PUBLIC WORKS"`; predicted: `"Department of Public Works"`. `rapidfuzz` token-sort ratio = 81.25, just under our 85 threshold. Judge correctly says the predicted value is supported by text. | **Tooling artifact**, not a real mismatch — fuzzy-match threshold tuning, not a labeling or extraction error. |
| `contract_number`/`parent_contract_number` on the 22143 modification | Predicted `parent_contract_number = 22160`; gold was `22143` (matching the document header). The judge found the document body literally states *"This second Modification to Agreement 22160"* — the source document **contradicts itself** between header and body. | **Real document-quality finding.** The pipeline extracted from the body; we labeled from the header. Neither is "wrong" — the source document has an internal numbering inconsistency. |
| `service_category` on the same modification | Gold: `facilities_maintenance`; predicted: `professional_services` (building automation design/review work, on-site staff time). Judge backs the predicted value as a reasonable read. | **Defensible disagreement**, exactly the case the schema's own design note anticipates for inferred fields — not a pipeline error. |
| `service_category` on the renewal letter | Gold: `technology_software` (inferred from "Courtroom Technology and Misc A/V Systems" in the contract description). Judge says this isn't literally stated in the renewal letter text. | **Judge being conservative on an inferred field** — correct behavior for a faithfulness judge (it's not supposed to credit inference), but means inferred-field judge scores should be read as a lower bound, not a literal accuracy number. |
| `contract_end_date` on the 16069 agreement | We labeled `null` (the "two-year period" language in the text modifies *pricing*, not the contract term — see §4.1's judgment call). Judge inferred an end date (execution + 2 years) and called it supported. | **Judge over-inferring** — it reasoned past what the text actually states. Validates the conservative labeling call as the right one; flags that the judge can be slightly too generous when a number is computable from indirect language. |

### 6.6 Chat judge calibration (qualitative)

All 5 semantic cases with a known answer scored 5/5 faithfulness; 4 scored 5/5 relevance, one
scored 4/5. No disagreements worth flagging here — the chat judge's scores track the known
answers closely across every case.

---

## 7. Findings and what to do about them

### 7.1 Renewal letters are being misclassified as Vendor Disclosure Statements (high priority)

**Evidence:** 9 of 11 gold `renewal_letter` documents in the classification sample were predicted
as `vendor_disclosure_statement` — not a few edge cases, the *majority* of the type. This is now a
quantified, reproducible number, not a filename-pattern hunch.

**Likely root cause (not yet confirmed):** `docs/doc_classification.md` notes that renewal letters
have a Vendor Disclosure Statement form *attached as page 2* of the same PDF ("Page 2... is always
a blank VDS form — ignore it"). A plausible explanation is that the rule-based or LLM classifier is
keying off content that appears on that attached VDS page rather than the renewal letter's own
page 1 content, especially for renewal letters where the VDS page is large relative to a short
renewal letter.

**Recommended next step:** pull the actual classification confidence/method (`rule_based` vs.
`llm` vs. `vision`) and reasoning for these 9 misclassified documents from `contracts.db`, and
inspect 2-3 of them directly to confirm the page-2-bleed-through hypothesis before changing the
classifier. This is exactly the kind of "found it, now go fix it" finding the eval framework was
built to produce — but the fix belongs in `src/pipeline/classifier.py`, not in the eval.

### 7.2 At least one source document has an internal numbering inconsistency

`2023_10_05_Contract_22143_Modification_1_EXECUTED.pdf`'s header says "Agreement 22143," its body
says "Agreement 22160." This isn't a pipeline bug — it's a data-quality issue in the source
corpus, surfaced by the judge comparing extraction against the actual document text. Worth a
one-line callout in the walkthrough as evidence the eval distinguishes "pipeline got it wrong"
from "the source document is internally inconsistent."

### 7.3 Inferred-field judge scores should be read as a lower bound, not a literal accuracy

Because the faithfulness judge is instructed to credit only what's literally stated, it will
systematically under-score `service_category` and similar inferred fields relative to a reasonable
human standard. This isn't a judge bug — it's the correct, conservative behavior for a
faithfulness check — but it means the 88.9% inferred-field accuracy number (computed against
ground truth, which *does* credit reasonable inference) is the more meaningful number for these
three fields, not anything the judge alone would report.

### 7.4 The fuzzy-match threshold for `county_department` is slightly too strict

One real example (81.25 vs. an 85 threshold) suggests department-name abbreviation is more
aggressive than vendor-name abbreviation in this corpus (e.g. dropping "Lake County" entirely
rather than dropping a legal suffix). A threshold of ~80 would likely close this gap without
meaningfully increasing false positives, given the small fields involved. Low priority — one
example isn't enough to recalibrate confidently, but worth revisiting if the full-corpus run
surfaces more of the same pattern.

### 7.5 One semantic chat case missed retrieval (4/5 hit rate)

The case asking about the 22143 modification didn't retrieve its target document among the chat's
cited sources, despite the chat judge still scoring the resulting answer 5/5 faithfulness (it
likely retrieved relevant-enough adjacent content). Not yet root-caused — worth a look at the
embedding/chunking for this specific document before the full-corpus run, since a 2-page scanned
modification with sparse text is a plausible explanation, but unconfirmed.

---

## 8. Proven vs. assumed (the honest framing for the walkthrough)

**Proven, with hard numbers:**
- Classification accuracy and its primary failure mode (renewal → VDS confusion), against 24
  independently human-verified labels.
- Extraction field accuracy and hallucination rate, against every populated field on 6
  fully-labeled documents (55 scored cells).
- The judge is trustworthy enough to lean on elsewhere: 89.1% agreement with ground truth, with
  every disagreement individually explained (§6.5) rather than left as an unexplained gap.
- The chat interface's structured path is exactly right on every tested case; the semantic path is
  highly faithful and relevant against known answers.

**Assumed / explicitly deferred:**
- The judge ran on a sample of 6 documents (the ground-truth-overlap set), not the full ~99-document
  corpus or live traffic. Turning that on is a `--sample N` parameter change, not new code — the
  cost and findings-backlog risk of a full run were deliberately deferred past the PoC.
- Ground truth is a single-labeler, 6-to-24-document set. No inter-annotator agreement or
  statistical confidence intervals — these are directional anchors, not statistically powered
  estimates, and are presented as such.
- The "award letter with bid tab" extraction criterion was approximated with a populated-value
  award letter; no literal bid-tab document exists in the sampled corpus.
- Scanned documents are judged via vision but excluded from the RAG/chat layer entirely (no
  extractable text to index) — a known, documented limitation, not a silent gap.

---

## 9. Recommended next steps, in priority order

1. **Investigate the renewal-letter/VDS classifier confusion** (§7.1) — highest-value fix
   candidate, since it's the single largest gap in the entire eval and affects the most-common
   contract action type (renewals, ~27% of the corpus).
2. **Run the judge at full-corpus scale** (`--judge-extra-unlabeled` already supports incremental
   unlabeled coverage; a full run is a config change) once the classifier fix lands, to confirm the
   fix and get a corpus-wide hallucination-rate read rather than a 6-document one.
3. **Root-cause the one retrieval miss** (§7.5) before scaling chat eval coverage.
4. **Revisit the `county_department` fuzzy-match threshold** (§7.4) — low priority, but cheap to
   fix once more examples exist.
5. **Stand up `monitoring_snapshot()` on a schedule** once the above are addressed — the
   infrastructure already exists (`eval/monitoring.py`), this is a config/deployment task, not new
   code.
