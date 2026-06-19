# ADR — Scanned Document Handling: Vision-Based Extraction

> **Type:** Architectural Decision Record  
> **Status:** Decided  
> **Affects:** `src/pdf_parser.py` (Task 2.1), `src/llm_client.py` (Task 2.3), `src/extractor.py` (Task 2.3)  
> **Does not affect:** `src/document_classifier.py`, `src/prompt_builder.py`, SQLite schema, ChromaDB ingestion, Streamlit UI

---

## 1. What We Found

After building the sample manifest and running a corpus recon script against the 99-document
sample, we measured page count, file size, and scanned status across every file.

**Page count distribution:**

| Bucket | Count | Share |
|--------|-------|-------|
| 1–2 pages | 59 | 59.6% |
| 3–10 pages | 15 | 15.2% |
| 11–30 pages | 14 | 14.1% |
| 31–100 pages | 9 | 9.1% |
| 100+ pages | 2 | 2.0% |

Median page count: 2. Mean: 15.5. Max: 405 (one outlier; confirmed fully executed agreement
with large exhibits at ~189K tokens). The distribution is heavily right-skewed — the vast
majority of documents are thin (renewal letters, VDS forms, award letters), with a long tail
of large fully executed agreements.

**Scanned document detection** (PyMuPDF text extraction < 100 chars + embedded images present):

```
26 / 99 documents flagged as scanned (26.3%)
```

Full list of scanned files:

```
23173_2_Fully_Executed_Agreement_with_Exhibits.pdf       (62 pages)
19028_Fully_Executed_Agreement.pdf                       (35 pages)
23173_1_Task_Order_C__Fully_Executed.pdf                 (19 pages)
23173_1_Task_Order_D__Fully_Executed.pdf                 (19 pages)
23173_1_Task_Order_E__Fully_Executed.pdf                 (19 pages)
23173_1_Task_Order_F__Fully_Executed.pdf                 (17 pages)
22128_2_Fully_Executed_Agreement.pdf                     (16 pages)
24213_Sympro_Fully_Executed_Agreement.pdf                (16 pages)
23173_1_Task_Order_A__Fully_Executed.pdf                 (15 pages)
2025_08_27__Agreement_25263__Fully_Executed.pdf          (15 pages)
22128_1_Fully_Executed_Agreement.pdf                     (14 pages)
22162_Fully_Executed_Contract.pdf                        (14 pages)
22153_FMLA_Services_Agreement_EXECUTED_3_1_23.pdf        (12 pages)
22153_FMLA_Services_Agreement_EXECUTED_3_1_231.pdf       (12 pages)
24213_Fully_Executed_Master_Agreement__PTG.pdf           (11 pages)
22143_Fully_Executed_Agreement.pdf                       (10 pages)
23173_3_Agreement_Final__Fully_Executed.pdf              (10 pages)
19028_SOW_2__Microfilm_Microfiche_EXECUTED_7_28_23...    (8 pages)
16069_Agreement_fully_executed.pdf                       (7 pages)
2024_05_15_Contract_22041_Modification_2_EXECUTED.pdf    (6 pages)
23173_1_Task_Order_B__Fully_Executed.pdf                 (5 pages)
22120_Contract_Modification_1_Flow_Technics_EXECUTED...  (3 pages)
2023_10_05_Contract_22143_Modification_1_EXECUTED.pdf    (2 pages)
19028_Contract_Modification_EXECUTED_7_28_23.pdf         (1 page)
09_18_2023_Contract_22041_Amendment_1_..._EXECUTED.pdf   (1 page)
2026_03_19__Agreement_25263_Modification_1__Fully...     (1 page)
```

---

## 2. Why It Matters

Every scanned document in the sample is a fully executed agreement, task order, or
modification/amendment. Not one renewal letter, VDS form, or award letter is scanned.
The scanned documents are concentrated in exactly the doc types that carry the highest-value
fields in the extraction schema.

**Downstream analyses at risk if scanned docs are skipped:**

| Analysis | Impact |
|----------|--------|
| Renewal cliff dashboard | Loses `contract_end_date` and `total_contract_value` from base agreements |
| Auto-renewal liability scan | Loses `auto_renewal_flag` and `termination_notice_days` — both only extractable from fully executed agreements |
| Spend concentration map | Loses `total_contract_value` for a quarter of the vendor portfolio |
| True total commitment | Loses `modification_financial_delta` from scanned amendments |
| Price escalation exposure | Loses `price_escalator_terms` — only present in fully executed agreements |

Skipping 26% of documents was acceptable as a PoC simplification when scanned docs were
assumed to be a small minority of low-value document types. That assumption is wrong for
this corpus. Silently dropping a quarter of fully executed agreements would materially
misrepresent the pipeline's analytical coverage and undermine the downstream analyses
that are the primary value proposition of the tool.

---

## 3. Decision

**Use the Anthropic vision API for scanned documents instead of skipping them.**

The Anthropic Messages API accepts images as content blocks alongside text. For scanned
documents where PyMuPDF cannot extract text, the pipeline renders each page as a PNG image
and passes those images to Claude with the same extraction prompt. The LLM reads the
document visually and returns the same structured JSON output as for text-based documents.

**Why this approach over alternatives:**

- **OCR (Tesseract, AWS Textract):** Adds infrastructure dependencies, additional credentials,
  and pipeline complexity. Textract in particular requires AWS setup that's inappropriate for
  a local PoC. OCR also produces intermediate text that still needs to go through the LLM
  extractor — two steps instead of one.
- **Skip and flag:** Was the original plan. Invalidated by the data — 26% scanned rate
  concentrated in the highest-value doc types makes this analytically unacceptable.
- **Vision API:** No new dependencies beyond the Anthropic SDK already in use. No additional
  credentials. One new function in `llm_client.py` and a routing branch in `extractor.py`.
  The same extraction prompt works for both paths — no prompt duplication.

---

## 4. Implementation Changes Required

### 4.1 `src/pdf_parser.py`

**Change 1 — Add `extract_page_images()` function**

Add a new public function that renders PDF pages as PNG images. This is called by the
extractor for scanned documents; the parser itself does not decide when to call it.

```python
def extract_page_images(filepath: str, dpi: int = 150, max_pages: int = 20) -> list[bytes]:
    """
    Render each page of a PDF as a PNG image.
    Used for scanned documents where text extraction fails.

    Args:
        filepath:   Absolute path to the PDF file.
        dpi:        Render resolution. 150 DPI is sufficient for text legibility
                    in the LLM vision API while keeping image size manageable.
                    Do not exceed 200 DPI — diminishing returns on accuracy,
                    significant increase in image token cost.
        max_pages:  Maximum number of pages to render. Pages beyond this limit
                    are not rendered. Default 20 covers the substantive content
                    of all fully executed agreements in this corpus — the fields
                    we need (contract number, dates, value, escalator terms,
                    termination notice) appear in the first 10–15 pages; exhibits
                    beyond page 20 are typically pricing schedules already handled
                    by null logic in the extractor.

    Returns:
        List of raw PNG bytes, one per page, in page order.
        Returns empty list if the file cannot be opened.
    """
    try:
        doc = fitz.open(filepath)
        images = []
        for page_num in range(min(len(doc), max_pages)):
            page = doc[page_num]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            images.append(pix.tobytes("png"))
        doc.close()
        return images
    except Exception as e:
        logger.warning(f"[pdf_parser] extract_page_images failed for {filepath}: {e}")
        return []
```

**Change 2 — Add `filepath` to `parse_pdf()` return dict**

The extractor needs the filepath to call `extract_page_images()` for scanned documents.
The filepath is already passed into `parse_pdf()` but was not previously included in the
return dict. Add it:

```python
# In parse_pdf() return dict — add this key:
"filepath": filepath,   # absolute path; needed by extractor for vision fallback
```

**Change 3 — Update Section 4 (Scanned Document Detection) behavior**

The original spec said: when `is_scanned = True`, set `parse_error` to an error message.
Remove that. Scanned documents are not errors — they are a handled case. Update the behavior:

```python
# Old behavior (remove):
# parse_error = "Document appears to be scanned or image-only; text extraction failed"

# New behavior:
# is_scanned = True
# text = ""
# parse_error = None   ← scanned is not an error; vision path handles it downstream
# filepath is present in the return dict so extractor can call extract_page_images()
```

Log scanned documents at INFO, not WARNING. They are expected and handled.

**Change 4 — Update `parse_status` in tracking CSV**

The `parse_status` field in `outputs/parse_results.csv` currently maps `is_scanned = True`
to `"scanned"` with an implicit "skipped" meaning. Update the description in the tracking
file header to reflect that scanned documents proceed to vision-based extraction rather
than being skipped.

No column change needed — `"scanned"` is still the correct status value. Only the
interpretation changes.

**Change 5 — Update parse summary report**

In `outputs/parse_summary.txt`, rename the `"Scanned/skipped"` label to
`"Scanned (vision path)"` to reflect that these documents are processed, not dropped.

---

### 4.2 `src/llm_client.py`

**Change 1 — Add `call_llm_with_images()` function**

Add a new public function alongside the existing `call_llm()`. It accepts the same prompt
string plus a list of PNG image bytes, builds a multimodal content block, and calls the
API. Return type and retry logic are identical to `call_llm()`.

```python
import base64

def call_llm_with_images(
    prompt: str,
    page_images: list[bytes],
    task: str = "extraction",
    max_tokens: int = 1500,
) -> str:
    """
    Send a prompt plus page images to the Anthropic API for vision-based extraction.
    Used for scanned documents where text extraction is unavailable.

    Args:
        prompt:       The full extraction prompt string (same prompt used for text extraction).
        page_images:  List of raw PNG bytes, one per page, from pdf_parser.extract_page_images().
                      Images are prepended to the prompt text as separate content blocks.
        task:         Task name for model resolution. Use "extraction" for document extraction.
        max_tokens:   Maximum response tokens. Same default as call_llm().

    Returns:
        Raw response text (str). Caller is responsible for JSON parsing.

    Raises:
        LLMCallError: On non-retryable failures after retry exhaustion.
    """
    model = MODEL_CONFIG.get(task, MODEL_CONFIG["extraction"])

    # Build content: images first, prompt text last
    content = []
    for img_bytes in page_images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(img_bytes).decode("utf-8")
            }
        })
    content.append({"type": "text", "text": prompt})

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = _client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}]
            )
            _log_token_usage(response, task)
            return response.content[0].text

        except anthropic.RateLimitError as e:
            last_error = e
            wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(f"[llm_client] Rate limit (vision) on attempt {attempt + 1}; retrying in {wait}s")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(f"[llm_client] Server error {e.status_code} (vision) on attempt {attempt + 1}; retrying in {wait}s")
                time.sleep(wait)
            else:
                raise LLMCallError(f"Non-retryable API error (vision): {e.status_code} — {str(e)}") from e

        except anthropic.AuthenticationError as e:
            raise LLMCallError("Authentication failed — check ANTHROPIC_API_KEY") from e

    raise LLMCallError(f"Vision LLM call failed after {MAX_RETRIES} attempts: {str(last_error)}")
```

**Change 2 — No changes to `MODEL_CONFIG`, retry logic, or token logging**

`call_llm_with_images()` uses the same `MODEL_CONFIG["extraction"]` model, the same
`MAX_RETRIES` / `RETRY_BACKOFF_SECONDS` constants, and the same `_log_token_usage()`
function. No duplication of configuration.

---

### 4.3 `src/extractor.py`

**Change 1 — Remove the scanned document skip gate**

The original spec skipped scanned documents before invoking the LLM:

```python
# REMOVE this block:
if parse_result.get("is_scanned"):
    return _make_failure_record(filename, doc_type, "skipped", SKIP_REASONS["scanned"])
```

Replace it with vision routing (see Change 2 below).

**Change 2 — Add vision routing in `extract_document()`**

After the low-confidence classification skip gate (which stays unchanged), route scanned
documents to `call_llm_with_images()` instead of `call_llm()`:

```python
# In extract_document(), replace the scanned skip gate with:

document_text = parse_result.get("text", "")
is_scanned = parse_result.get("is_scanned", False)
prompt = build_extraction_prompt(doc_type, document_text)

# Route based on whether document has extractable text
if is_scanned:
    filepath = parse_result.get("filepath", "")
    if not filepath:
        return _make_failure_record(
            filename, doc_type, "failed",
            "Scanned document has no filepath in parse result; cannot render images"
        )
    page_images = extract_page_images(filepath, dpi=150, max_pages=20)
    if not page_images:
        return _make_failure_record(
            filename, doc_type, "failed",
            "Scanned document: extract_page_images() returned no images"
        )
    try:
        raw_response = call_llm_with_images(prompt, page_images, task="extraction")
    except LLMCallError as e:
        return _make_failure_record(filename, doc_type, "failed",
                                    f"Vision LLM call failed: {str(e)}")
else:
    try:
        raw_response = call_llm(prompt, task="extraction")
    except LLMCallError as e:
        return _make_failure_record(filename, doc_type, "failed",
                                    f"LLM call failed: {str(e)}")
```

**Change 3 — Add import for `extract_page_images`**

At the top of `extractor.py`, add:

```python
from pdf_parser import parse_pdf, extract_page_images
```

**Change 4 — Add `extraction_method` to result dict**

Add a field to the assembled result dict indicating whether text or vision extraction was
used. This is useful for eval analysis (are vision extractions less accurate than text
extractions?) and for the walkthrough defense.

```python
# In _assemble_result(), add:
"extraction_method": "vision" if is_scanned else "text",
```

Add `extraction_method` as a TEXT column in the SQLite schema and to
`outputs/extraction_results.csv`. Pass `is_scanned` into `_assemble_result()` as a
parameter.

**Change 5 — Update extraction summary report**

In `outputs/extraction_token_summary.txt`, add a breakdown of text vs. vision extractions:

```
--- Extraction Method ---
  Text-based:    73  (73.0%)
  Vision-based:  26  (26.0%)  ← scanned documents processed via image API
  Skipped:        0   (0.0%)
  Failed:         0   (0.0%)
```

**Change 6 — Update `SKIP_REASONS` dict**

Remove the `"scanned"` entry from `SKIP_REASONS` since scanned documents are no longer
skipped. The dict entry is now dead code and should be removed to avoid confusion.

```python
# Remove:
SKIP_REASONS = {
    "scanned": "Document is scanned or image-only; extraction skipped",
    "low_confidence_classification": "...",
}

# Replace with:
SKIP_REASONS = {
    "low_confidence_classification": "Classifier confidence is low; extraction skipped to avoid wrong field block",
}
```

---

## 5. What Does Not Change

The following are explicitly unchanged by this decision. Do not modify them:

- **`src/document_classifier.py`** — The classifier still assigns `doc_type = "other"` and
  `confidence = "low"` for scanned documents and bypasses both rule and LLM classification
  stages. This is correct: without text, classification cannot run. The extractor now handles
  scanned docs via vision, but the classifier's behavior for them is unchanged. The
  `doc_type = "other"` assigned by the classifier for scanned docs will be overridden during
  extraction when the LLM reads the document visually and returns the correct `doc_type` in
  its JSON output — the extractor uses the LLM's returned `doc_type`, not the classifier's,
  when writing to SQLite.

- **`src/prompt_builder.py`** — The extraction prompt template and injected field blocks are
  identical for text and vision paths. The same prompt is passed to both `call_llm()` and
  `call_llm_with_images()`. No prompt changes needed.

- **SQLite schema** — All fields are unchanged except for the addition of `extraction_method`
  (TEXT, nullable). Add this column to the DDL in `extraction_schema.md` and to the
  `CREATE TABLE` statement in the DB setup script.

- **ChromaDB / vector store ingestion (Task 2.6)** — The vector store ingests text chunks.
  Scanned documents produce `text = ""` from the parser, meaning they contribute nothing to
  the vector store. This is an accepted limitation: scanned documents are structurally
  extractable via vision but not semantically searchable via RAG. Document this in the README
  as a known limitation.

- **Streamlit UI (Task 2.7)** — No changes. The UI queries the structured table and the
  vector store; it is agnostic to how extraction was performed.

- **Evaluation harness (Task 3.x)** — No structural changes. The eval harness should,
  however, report accuracy separately for text-based vs. vision-based extractions so the
  walkthrough can honestly characterize any accuracy differential between the two paths.
  Add `extraction_method` as a filter dimension in eval reporting.

---

## 6. Known Limitations Introduced

These should be documented in the README:

**Scanned documents are not searchable via the chat interface.** The RAG layer (ChromaDB)
is built from extracted text. Scanned documents produce no extracted text and therefore
have no vector store representation. A user asking "what are the termination terms for
contract 23173?" will get a structured table answer (from the vision-extracted fields) but
no clause-level retrieval from the chat interface. Full searchability of scanned documents
would require OCR-to-text as a preprocessing step — a production hardening item.

**Vision extraction is capped at 20 pages.** Pages beyond page 20 are not sent to the API.
For documents where key fields appear after page 20 (uncommon in this corpus but possible
for agreements with very long preambles), those fields will be null. The `extraction_notes`
field will surface this: set `extraction_notes = "Vision extraction: document truncated at
page 20"` for any scanned document with more than 20 pages.

**Vision tokens are priced differently from text tokens.** Image tokens at 150 DPI are
approximately 1,000–2,000 tokens per page. For the 26 scanned documents in the sample
averaging ~13 pages each, estimated additional cost is $5–10 on Sonnet 4.6. Acceptable
at PoC scale.

---

## 7. Decision Rationale Summary (for Walkthrough)

> "During corpus recon I discovered that 26% of my sample — all fully executed agreements,
> task orders, and modifications — were scanned image PDFs with no extractable text. The
> original spec treated scanned documents as a skip case, but with the scanned docs
> concentrated in the highest-value document types, skipping them would have dropped a
> quarter of my base agreements and most of my amendment data — directly undermining the
> renewal cliff dashboard and spend concentration analyses. I added vision-based extraction
> using the Anthropic multimodal API, which required one new function in the LLM client and
> a routing branch in the extractor. The extraction prompt is identical for both paths.
> The main limitation is that scanned documents aren't searchable through the chat interface
> since they have no text for the vector store — that would require OCR as a preprocessing
> step in a production hardening pass."