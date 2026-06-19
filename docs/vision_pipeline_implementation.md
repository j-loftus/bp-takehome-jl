# Vision Pipeline — Implementation Summary

> **Relates to:** `docs/scanned_doc_handling.md` (original ADR)  
> **Affects:** `src/pipeline/classifier.py`, `src/pipeline/extractor.py`, `src/llm_client.py`, `src/pipeline/pdf_parser.py`

---

## What Was Built

Corpus recon revealed that 26 of 99 sampled documents (26%) are scanned image PDFs with no
extractable text. The original ADR decided to handle these via vision-based extraction rather
than skipping them. This document summarizes what was actually implemented, which diverged from
the original plan in one meaningful way: the classifier was also extended to handle scanned
documents via vision, not just the extractor.

The final pipeline handles scanned documents across two stages:

### Stage 1 — Vision Classification (`classifier.py`)

The text classifier cannot classify scanned documents — there is no text to read. The original
ADR accepted this and left scanned documents classified as `other/low/rule_based`. This created
a downstream problem: the extraction prompt is selected based on `doc_type`, so all scanned
documents received the generic `"other"` field block regardless of what they actually were.

**What changed:** `classify_document()` now routes scanned documents to `_classify_by_vision()`
instead of immediately returning `other`. The function renders the first two pages of the PDF
as PNG images and sends them to the vision LLM with a short classification-only prompt. It
returns the same `(doc_type, confidence, reasoning, error)` tuple as `_classify_by_llm()`,
so the rest of `classify_document()` is unchanged.

The classification prompt is minimal — it asks only for `doc_type`, `confidence`, and
`reasoning`, with `max_tokens=256`. For the 26 scanned documents in our sample, which
are all fully executed agreements, task orders, or modifications, the model identifies the
correct type reliably from the first page alone.

**Result:** Scanned documents now return, for example:
```
doc_type:   modification_amendment
confidence: high
method:     vision
```
instead of `other/low/rule_based`.

### Stage 2 — Vision Extraction (`extractor.py`)

With the classifier now returning the correct `doc_type`, the extractor builds the targeted
extraction prompt — `modification_amendment` gets its specific field block with
`modification_financial_delta`, `parent_contract_number`, and `price_escalator_terms`;
`fully_executed_agreement` gets `contract_end_date`, `renewal_options`, `auto_renewal_flag`,
and `termination_notice_days`.

The extractor then sends the full document (up to 20 pages) as images to the vision LLM with
that targeted prompt, and receives the same structured JSON output as the text path.

**New field:** `extraction_method` is recorded as `"text"` or `"vision"` on every row in the
output CSV and SQLite table. This lets evaluation and reporting distinguish accuracy between
the two paths.

---

## Why the Two-Stage Design

The alternative — a single combined classification + extraction pass — would have worked but
would have made the classification output opaque. By keeping classification and extraction as
separate stages:

- The classification result is inspectable before extraction runs (useful in the notebook
  and for debugging)
- Classification failures fall back gracefully to `other`, which still produces an extraction
  attempt rather than a hard skip
- The token cost for classification is small (two pages, 256 max tokens), while extraction
  uses the full document at 4096 max tokens

---

## Architecture Summary

```
PDF (scanned)
    │
    ▼
pdf_parser.parse_pdf()
    is_scanned = True
    text = ""
    filepath = "/path/to/file.pdf"
    │
    ▼
classifier.classify_document()
    → _classify_by_vision(filepath)
        extract_page_images(filepath, max_pages=2)   ← first 2 pages only
        call_llm_with_images(classification_prompt, images, max_tokens=256)
    → doc_type: "modification_amendment", confidence: "high", method: "vision"
    │
    ▼
extractor.extract_document()
    build_extraction_prompt("modification_amendment", "")   ← targeted field block
    extract_page_images(filepath, max_pages=20)             ← full document
    call_llm_with_images(extraction_prompt, images, max_tokens=4096)
    → structured JSON with modification-specific fields populated
    │
    ▼
ContractRecord (Pydantic validation)
    extraction_method = "vision"
    │
    ▼
SQLite / extraction_results.csv
```

Text-based documents follow the same stages with `call_llm()` substituted at both points.

---

## What Did Not Change

- **Extraction prompt templates** — identical for text and vision paths. No prompt duplication.
- **`ContractRecord` schema** — same 25-field Pydantic model for both paths, plus `extraction_method`.
- **SQLite schema** — one new column (`extraction_method TEXT`), otherwise unchanged.
- **Low-confidence skip gate** — still applies to text documents. Does not apply to scanned
  documents (the `is_scanned` check precedes it in the extractor).

---

## Known Limitations

**Scanned documents are not semantically searchable.** The RAG layer (ChromaDB) is built from
extracted text. Scanned documents produce `text = ""` from the parser and therefore have no
vector store representation. Structured field queries work; clause-level retrieval does not.
Production hardening would add OCR as a preprocessing step.

**Extraction is capped at 20 pages.** Key fields in this corpus appear within the first 15
pages; the cap handles all sampled documents cleanly. Documents with important content past
page 20 will produce null fields with a note in `extraction_notes`.
