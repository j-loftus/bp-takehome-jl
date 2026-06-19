# Task 2.1 — PDF Parsing Module: Implementation Requirements

> **Purpose:** Specifies the PDF parsing module for the contract intelligence pipeline. This is a
> standalone module that runs as Step 0 of the pipeline, before document classification (Task 2.2)
> and before LLM extraction (Task 2.3). Its output — extracted text and document metadata — feeds
> both the LLM extraction prompt and the ChromaDB vector store (Task 2.6).
>
> **Dependencies:** None upstream. Downstream consumers: Task 2.2 (classifier), Task 2.3
> (extractor), Task 2.6 (vector store ingestion).

---

## 1. Decision: Why a Parsing Module (Not Multimodal PDF Input)

The pipeline requires extracted plain text for two independent reasons:

1. **Vector store ingestion (Task 2.6).** ChromaDB embeds text chunks — it cannot ingest raw
   PDF blobs. Extracted text is required regardless of the extraction approach.
2. **Consistent extraction input.** Sending extracted text to the LLM extraction prompt is cheaper,
   faster, and more debuggable than passing raw PDFs via a multimodal API. When extraction fails,
   having the intermediate text makes it straightforward to isolate whether the problem is in
   parsing or prompting.

With text extraction required anyway, a dedicated parsing module serves double duty and eliminates
the need for two separate API call patterns in the pipeline.

**Scanned/image-only documents** are handled by detection and flagging only — no OCR fallback.
This is a deliberate PoC scope decision. See Section 4.

---

## 2. Library

Use **PyMuPDF** (`import fitz`).

```
pip install pymupdf
```

PyMuPDF is preferred over pdfplumber for this corpus because:
- Faster text extraction on multi-page documents (relevant for fully executed agreements at 10–20+
  pages)
- More reliable handling of multi-column layouts and header/footer regions
- Better support for detecting image-only pages via the page content inspection API

---

## 3. Module Interface

**File:** `src/pdf_parser.py`

**Primary function:**

```python
def parse_pdf(filepath: str) -> dict:
    """
    Extract text and metadata from a single PDF file.

    Returns a dict with the following keys:
        filepath        (str)   — absolute path to source file
        filename        (str)   — basename of the file
        text            (str)   — full extracted text, pages joined with '\n\n--- PAGE BREAK ---\n\n'
        page_count      (int)   — total number of pages in the document
        is_scanned      (bool)  — True if the document appears to be image-only (no extractable text)
        parse_error     (str | None) — error message if parsing failed; None on success
    """
```

**Batch function:**

```python
def parse_directory(dirpath: str) -> list[dict]:
    """
    Parse all PDF files in a directory. Returns a list of parse result dicts
    (one per file) in the format returned by parse_pdf().
    Non-PDF files are ignored. Files that fail to open are logged and included
    with parse_error populated.
    """
```

---

## 4. Scanned Document Detection

A document is flagged as `is_scanned = True` when **all** of the following are true:
- The total extracted text length across all pages is below a threshold (use 100 characters as the
  threshold — generous enough to catch near-empty extractions)
- At least one page contains an embedded image (detectable via `page.get_images()`)

Do **not** attempt OCR. When `is_scanned = True`:
- Set `text = ""` (empty string, not None)
- Set `is_scanned = True`
- Set `parse_error = "Document appears to be scanned or image-only; text extraction failed"`
- Log the filename to the console
- Include the result in the returned list so downstream steps can handle it explicitly (classifer
  and extractor should skip scanned docs gracefully)

---

## 5. Text Extraction Details

- Join pages with `'\n\n--- PAGE BREAK ---\n\n'` as a separator. This makes page boundaries
  visible in the text, which helps the downstream classifier and extractor orient themselves in
  multi-page documents (e.g., knowing that page 2 of a renewal letter is always the blank VDS
  form).
- Do **not** strip headers and footers. The contract number and department name often appear in
  running headers — removing them would drop critical linking-key signals.
- Do **not** apply any text normalization beyond standard whitespace cleanup (collapse multiple
  consecutive blank lines to a maximum of two). Do not lowercase, remove punctuation, or otherwise
  transform the text.
- Preserve the original page order. Do not sort, deduplicate, or rearrange pages.

---

## 6. Renewal Letter Page 2 Handling

Renewal letters always have a blank Vendor Disclosure Statement form on page 2. The parser should
**not** strip this page — it is the downstream classifier's job to recognize and ignore it. The
page break separator makes the two-page structure explicit in the extracted text, which is
sufficient.

---

## 7. Error Handling

| Condition | Behavior |
|-----------|----------|
| File does not exist | `parse_error` populated; `text = ""`; `is_scanned = False` |
| File is not a valid PDF | `parse_error` populated; `text = ""`; `is_scanned = False` |
| File opens but text extraction fails on one or more pages | Extract what is available; note in `parse_error`; do not raise |
| File is fully scanned / image-only | `is_scanned = True`; `text = ""`; see Section 4 |
| File opens and text extracted cleanly | `parse_error = None` |

All errors should be caught and returned in the result dict — **do not raise exceptions** from
`parse_pdf()`. The caller decides how to handle failures. This keeps the batch function resilient
across a full directory run.

---

## 8. Logging

Use Python's standard `logging` module (not `print`). The module should emit:
- `INFO`: one line per file processed, including filename, page count, and whether it was flagged
  as scanned
- `WARNING`: for files that produced parse errors or were flagged scanned
- `DEBUG`: individual page-level extraction details (off by default)

Log format should include the module name so log output is attributable in a multi-module pipeline:
```
2024-01-15 10:23:01 [pdf_parser] [INFO] Parsed renewal_letter_23159.pdf — 2 pages, is_scanned=False
2024-01-15 10:23:02 [pdf_parser] [WARNING] Scanned document detected: redacted_agreement_22001.pdf
```

---

## 9. Parse Results Tracking File

After `parse_directory()` completes, the module must write a CSV tracking file capturing one row
per parsed file. This serves as a persistent audit log for the parsing run and as the input to the
summary report (see Section 10).

**File:** `outputs/parse_results.csv`

**Columns:**

| Column | Type | Description |
|--------|------|-------------|
| `filename` | string | Basename of the source PDF file |
| `filepath` | string | Absolute path to the source file |
| `page_count` | integer | Number of pages in the document; 0 if file failed to open |
| `char_count` | integer | Total character count of extracted text; 0 if extraction failed |
| `is_scanned` | boolean | True if flagged as image-only |
| `parse_error` | string | Error message if parsing failed; empty string if successful |
| `parse_status` | string | One of: `success`, `scanned`, `error` — derived status for easy filtering |

`parse_status` derivation logic:
- `error` — `parse_error` is non-null and `is_scanned` is False
- `scanned` — `is_scanned` is True
- `success` — all other cases

The CSV should be written by a dedicated function:

```python
def write_parse_results(results: list[dict], output_path: str) -> None:
    """
    Write parse results to a CSV tracking file. One row per parsed file.
    Overwrites any existing file at output_path.
    """
```

Call this function at the end of `parse_directory()` automatically — the tracking file should
always be written after a batch run without requiring the caller to invoke it separately.

---

## 10. Parse Run Summary Report

After writing the tracking CSV, print a summary report to the console (using `logging.INFO`) and
also write it as a plain text file at `outputs/parse_summary.txt`. The report should cover:

```
=== PDF Parse Run Summary ===
Run timestamp:      2024-01-15 10:23:45
Source directory:   /path/to/contracts
Total files found:  387

--- Status Breakdown ---
  Successful:       371  (95.9%)
  Scanned/skipped:   10   (2.6%)
  Errors:             6   (1.5%)

--- Text Volume ---
  Total chars extracted:  4,823,441
  Avg chars per doc:     13,000  (successful docs only)
  Min chars (success):      312
  Max chars (success):   84,201

--- Low-Content Documents (< 500 chars, excluding scanned) ---
  contract_renewal_22001.pdf     —  87 chars
  amendment_19847.pdf            — 204 chars
  [list all files below 500 char threshold]

--- Scanned / Image-Only ---
  redacted_agreement_22001.pdf
  old_contract_scan_18334.pdf
  [list all scanned files]

--- Parse Errors ---
  corrupted_file_99999.pdf       — [error message]
  [list all error files with their error messages]

Tracking file written to: outputs/parse_results.csv
```

The 500-character threshold for "low-content" flagging is separate from the 100-character scanned
detection threshold. Low-content docs extracted some text but suspiciously little — they may be
partially scanned, heavily redacted, or near-empty cover pages. They are not skipped; they are
flagged here so the implementing agent or analyst can spot-check them before running extraction.

```python
def print_parse_summary(results: list[dict], source_dir: str, output_path: str) -> None:
    """
    Print and write a summary report of a completed parse run.
    results: list of dicts from parse_directory()
    source_dir: the directory that was parsed (for display)
    output_path: path to write parse_summary.txt
    """
```

Call this function at the end of `parse_directory()` immediately after `write_parse_results()`.

---

## 11. Output for Downstream Modules

The dict returned by `parse_pdf()` is the handoff contract to all downstream consumers. Neither
the classifier nor the extractor should call PyMuPDF directly — they consume the output of this
module only.

**Classifier (Task 2.2)** consumes: `text`, `filename`, `is_scanned`, `page_count`

**Extractor (Task 2.3)** consumes: `text`, `filename`, `is_scanned`

**Vector store ingestion (Task 2.6)** consumes: `text`, `filename`, `filepath`

**Parse tracking** (for eval and debugging): `outputs/parse_results.csv`, `outputs/parse_summary.txt`

---

## 12. PoC Scope Boundaries (Not In Scope)

The following are explicitly out of scope for the PoC and should be noted in the README as
production hardening items:

- **OCR fallback for scanned documents.** Flag and skip; do not attempt Tesseract or similar.
- **Exhibit A blank detection.** The extractor handles this via its own null logic and
  `extraction_notes`; the parser does not need to detect blank exhibits.
- **Password-protected PDFs.** If a file fails to open due to encryption, treat as a parse error.
- **Non-PDF file formats.** Out of scope for this corpus.
- **Parallel / async processing.** Sequential is fine for 100 documents at PoC scale.