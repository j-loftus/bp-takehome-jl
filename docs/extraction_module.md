# Task 2.3 — LLM Extraction Pipeline: Implementation Requirements

> **Purpose:** Specifies the LLM extraction pipeline for the contract intelligence pipeline.
> This module runs as Step 2 — after PDF parsing (Task 2.1) and document classification
> (Task 2.2). Its output is a validated dict per document matching the SQLite schema exactly,
> ready for the database writer (Task 2.5) to consume without further transformation.
>
> **Dependencies:**
> - Task 2.1 (PDF parsing module) — consumes `text`, `filename`, `is_scanned`
> - Task 2.2 (document classification module) — consumes `doc_type`, `confidence`, `filename`
> - Task 1.3 (extraction prompt strategy) — prompt template and injected field blocks
> - Task 1.1/1.2 (extraction schema) — field definitions, nullability rules, coverage matrix
>
> **Downstream consumers:** Task 2.4 (manual prompt iteration), Task 2.5 (SQLite writer),
> Task 3.1 (eval harness)

---

## 1. Module Structure

This task produces two Python modules:

| Module | Responsibility |
|--------|---------------|
| `src/llm_client.py` | Shared LLM API wrapper — used by classifier (Task 2.2), extractor (Task 2.3), and LLM-as-judge (Task 3.3) |
| `src/extractor.py` | Extraction orchestration — prompt building, LLM call, output validation, failure handling |

These are kept separate so the LLM client can be imported independently by any module that
needs to call the API. Neither module owns the other; both are utilities consumed by the
pipeline orchestrator.

---

## 2. Model Configuration

Model selection is explicit and centrally controlled. All model identifiers live in a single
configuration block at the top of `llm_client.py`. Switching models for any task requires
changing exactly one line.

```python
# src/llm_client.py — Model Configuration
# ------------------------------------------------
# Change these values to switch models across runs.
# All callers pass a task name; the client resolves it to the model string below.

MODEL_CONFIG = {
    "classification": "claude-haiku-4-5",   # Lightweight; rule-based handles most cases
    "extraction":     "claude-sonnet-4-5",  # Heavier task; complex prompt + long documents
    "judge":          "claude-sonnet-4-5",  # Eval harness LLM-as-judge (Task 3.3)
}

# Anthropic model string aliases for reference:
#   claude-haiku-4-5     — fast, cheap, sufficient for classification
#   claude-sonnet-4-5    — stronger instruction following; preferred for extraction
#
# To run a cost-comparison test: set "extraction" to "claude-haiku-4-5" and compare
# accuracy against sonnet outputs on the same document set.
```

Callers pass a `task` string rather than a model string directly:

```python
response = call_llm(prompt, task="extraction")
response = call_llm(prompt, task="classification")
```

This keeps model identity out of calling modules entirely. If Anthropic releases a new model,
updating `MODEL_CONFIG` propagates everywhere automatically.

---

## 3. LLM Client Module (`src/llm_client.py`)

### 3.1 Dependencies

```
pip install anthropic
```

The Anthropic SDK is the only dependency. API key is read from the environment variable
`ANTHROPIC_API_KEY` — never hardcoded. If the variable is not set, the client raises a
clear `EnvironmentError` at import time with an actionable message.

```python
import os
import anthropic

_api_key = os.environ.get("ANTHROPIC_API_KEY")
if not _api_key:
    raise EnvironmentError(
        "ANTHROPIC_API_KEY environment variable not set. "
        "Export it before running the pipeline: export ANTHROPIC_API_KEY=sk-ant-..."
    )

_client = anthropic.Anthropic(api_key=_api_key)
```

### 3.2 Primary Function

```python
def call_llm(
    prompt: str,
    task: str = "extraction",
    max_tokens: int = 1500,
) -> str:
    """
    Send a prompt to the Anthropic API and return the raw text response.

    Args:
        prompt:     The full prompt string to send as the user message.
        task:       Task name used to resolve the model from MODEL_CONFIG.
                    One of: 'classification', 'extraction', 'judge'.
        max_tokens: Maximum tokens in the response. Default 1500 is sufficient
                    for extraction JSON (~17 fields). Increase for judge responses
                    that include longer reasoning strings.

    Returns:
        Raw response text (str). Caller is responsible for JSON parsing.

    Raises:
        LLMCallError: On non-retryable failures after retry exhaustion.
                      Callers should catch this and record it as an extraction failure.
    """
```

### 3.3 Retry Logic

Retry on transient errors only: rate limits (`RateLimitError`) and server errors
(`APIStatusError` with 5xx status). Do not retry on authentication errors or invalid
request errors — these indicate configuration problems that won't resolve with retries.

```python
import time

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [2, 5, 10]  # Wait before attempt 2, 3, 4

def call_llm(prompt: str, task: str = "extraction", max_tokens: int = 1500) -> str:
    model = MODEL_CONFIG.get(task, MODEL_CONFIG["extraction"])
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = _client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            _log_token_usage(response, task)
            return response.content[0].text

        except anthropic.RateLimitError as e:
            last_error = e
            wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(f"[llm_client] Rate limit on attempt {attempt + 1}; retrying in {wait}s")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(f"[llm_client] Server error {e.status_code} on attempt {attempt + 1}; retrying in {wait}s")
                time.sleep(wait)
            else:
                # 4xx errors are not retryable
                raise LLMCallError(f"Non-retryable API error: {e.status_code} — {str(e)}") from e

        except anthropic.AuthenticationError as e:
            raise LLMCallError("Authentication failed — check ANTHROPIC_API_KEY") from e

    raise LLMCallError(f"LLM call failed after {MAX_RETRIES} attempts: {str(last_error)}")
```

### 3.4 Token Usage Logging

Per-call token usage is logged at DEBUG level. Run-level aggregation is handled by the
batch function in `extractor.py` (see Section 5.3).

```python
# Module-level accumulators — reset between batch runs via reset_token_counters()
_token_totals = {
    "classification": {"input": 0, "output": 0},
    "extraction":     {"input": 0, "output": 0},
    "judge":          {"input": 0, "output": 0},
}

def _log_token_usage(response, task: str) -> None:
    usage = response.usage
    _token_totals[task]["input"]  += usage.input_tokens
    _token_totals[task]["output"] += usage.output_tokens
    logger.debug(
        f"[llm_client] {task} — "
        f"input: {usage.input_tokens}, output: {usage.output_tokens}"
    )

def get_token_totals() -> dict:
    """Return a copy of accumulated token totals across all tasks."""
    return {task: dict(counts) for task, counts in _token_totals.items()}

def reset_token_counters() -> None:
    """Reset all token accumulators. Call at the start of each batch run."""
    for task in _token_totals:
        _token_totals[task] = {"input": 0, "output": 0}
```

### 3.5 Custom Exception

```python
class LLMCallError(Exception):
    """Raised when an LLM API call fails after retry exhaustion or on a non-retryable error."""
    pass
```

---

## 4. Extraction Module (`src/extractor.py`)

### 4.1 Module Overview

The extractor takes the outputs of the parser and classifier for a single document, builds
the appropriate extraction prompt, calls the LLM, parses and validates the response, and
returns a result dict. It does not write to SQLite — the DB writer (Task 2.5) owns that step.

This separation makes the extractor independently testable during Task 2.4 prompt iteration:
call `extract_document()` on any document without needing the database to exist.

### 4.2 Skip Gate

Before invoking the LLM, the extractor checks two conditions that warrant skipping
extraction entirely:

1. **Scanned document:** `parse_result['is_scanned'] == True` — no text to extract from
2. **Low-confidence classification:** `classification_result['confidence'] == 'low'` — the
   doc type is uncertain enough that applying a typed prompt would likely produce wrong results

Skipped documents produce a **failure record** (see Section 4.6) rather than an extraction
result. This keeps the failure surface explicit and reviewable without polluting the main table
with unreliable rows.

```python
SKIP_REASONS = {
    "scanned": "Document is scanned or image-only; extraction skipped",
    "low_confidence_classification": "Classifier confidence is low; extraction skipped to avoid wrong field block",
}
```

### 4.3 Prompt Building

Prompt construction delegates entirely to the template and injected field blocks defined in
Task 1.3. The extractor imports and calls these directly:

```python
from prompt_builder import build_extraction_prompt
# build_extraction_prompt(doc_type, document_text) → str
# Defined in src/prompt_builder.py, which contains EXTRACTION_PROMPT_TEMPLATE
# and INJECTED_FIELDS from Task 1.3 spec.
```

**Full document text is always passed.** No truncation is applied. For fully executed
agreements at 10–20+ pages, this means the full parsed text (including Exhibit A content
where present) reaches the LLM. This is the correct behavior for the PoC — Exhibit A is
exactly where `total_contract_value` and scope detail live.

> **PoC scope note:** Passing full document text is appropriate at 100-document scale.
> At thousands of documents, per-call token cost and latency would warrant a chunking or
> summarization strategy for long documents. This is a known production hardening item and
> should be noted in the README.

### 4.4 Primary Function

```python
def extract_document(
    parse_result: dict,
    classification_result: dict,
) -> dict:
    """
    Extract structured fields from a single parsed and classified document.

    Args:
        parse_result:           Output dict from pdf_parser.parse_pdf()
        classification_result:  Output dict from document_classifier.classify_document()

    Returns:
        Extraction result dict. Contains either:
          - A fully populated (or partially null) extraction dict matching the SQLite schema
          - A failure record with 'extraction_status' = 'failed' or 'skipped'

        In all cases, 'filename' and 'extraction_status' are always present.
        The caller (batch function or DB writer) checks 'extraction_status' to route
        the result to the main table or the failures log.

    Extraction status values:
        'success'  — LLM returned valid JSON; non-nullable fields present; row is DB-ready
        'skipped'  — document was scanned or classification confidence was low
        'failed'   — LLM call failed, JSON parse failed, or non-nullable fields were null
    """
```

### 4.5 Extraction Orchestration (Internal Flow)

```python
def extract_document(parse_result: dict, classification_result: dict) -> dict:

    filename = parse_result.get("filename", "unknown")
    doc_type = classification_result.get("doc_type", "other")

    # --- Skip gate ---
    if parse_result.get("is_scanned"):
        return _make_failure_record(filename, doc_type, "skipped", SKIP_REASONS["scanned"])

    if classification_result.get("confidence") == "low":
        return _make_failure_record(filename, doc_type, "skipped",
                                    SKIP_REASONS["low_confidence_classification"])

    # --- Build prompt ---
    document_text = parse_result.get("text", "")
    prompt = build_extraction_prompt(doc_type, document_text)

    # --- Call LLM ---
    try:
        raw_response = call_llm(prompt, task="extraction")
    except LLMCallError as e:
        return _make_failure_record(filename, doc_type, "failed", f"LLM call failed: {str(e)}")

    # --- Parse JSON ---
    try:
        extracted = json.loads(raw_response)
    except json.JSONDecodeError as e:
        return _make_failure_record(filename, doc_type, "failed",
                                    f"LLM response not valid JSON: {str(e)}")

    # --- Validate ---
    validation_error = _validate_extraction(extracted, doc_type)
    if validation_error:
        return _make_failure_record(filename, doc_type, "failed", validation_error)

    # --- Assemble result dict ---
    result = _assemble_result(filename, doc_type, extracted)
    result["extraction_status"] = "success"
    return result
```

### 4.6 Output Validation

Validation enforces the schema rules from Task 1.2. The key design principle: **nulls are
validated against the universal spine only**. Type-specific fields (Group B–E) may be null
on any document type without triggering a failure — the coverage matrix in `extraction_schema.md`
defines which nulls are "by design." A VDS form returning nulls for `total_contract_value`,
`price_escalator_terms`, and all Group C fields is a correct extraction, not a failure.

```python
ALLOWED_DOC_TYPES = {
    "fully_executed_agreement", "renewal_letter", "modification_amendment",
    "award_letter", "vendor_disclosure_statement", "other"
}

ALLOWED_PRICE_ESCALATOR = {"fixed", "cpi_capped", "fixed_percentage",
                            "negotiated_at_renewal", "not_specified", None}

ALLOWED_SERVICE_CATEGORY = {
    "professional_services", "technology_software", "facilities_maintenance",
    "public_safety", "infrastructure", "staffing", "supplies_goods",
    "behavioral_health", "other", None
}

ALLOWED_PROCUREMENT_VEHICLE = {"direct_rfp", "cooperative_piggyback", "sole_source", "other", None}

ALLOWED_CONFIDENCE = {"high", "medium", "low"}

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_extraction(extracted: dict, doc_type: str) -> str | None:
    """
    Validate extracted JSON against schema rules.
    Returns an error message string if validation fails, or None if valid.

    Validation only fails (blocks the row) on:
      1. Missing or null non-nullable spine fields (contract_number, vendor_name, doc_type)
      2. Invalid enum values for controlled vocabulary fields
      3. Invalid date format for date fields that are present (non-null)

    Null values on type-specific fields (Group B–E) are always valid — they represent
    "not present in this document type" per the coverage matrix, not extraction failures.
    """

    # --- Non-nullable spine fields ---
    if not extracted.get("contract_number"):
        return "contract_number is null or missing — row not written"
    if not extracted.get("vendor_name"):
        return "vendor_name is null or missing — row not written"
    if not extracted.get("doc_type"):
        return "doc_type is null or missing — row not written"

    # --- Enum validation (only when non-null) ---
    pe = extracted.get("price_escalator_terms")
    if pe is not None and pe not in ALLOWED_PRICE_ESCALATOR:
        return f"price_escalator_terms invalid value: '{pe}'"

    sc = extracted.get("service_category")
    if sc is not None and sc not in ALLOWED_SERVICE_CATEGORY:
        return f"service_category invalid value: '{sc}'"

    pv = extracted.get("procurement_vehicle")
    if pv is not None and pv not in ALLOWED_PROCUREMENT_VEHICLE:
        return f"procurement_vehicle invalid value: '{pv}'"

    dt = extracted.get("doc_type")
    if dt not in ALLOWED_DOC_TYPES:
        return f"doc_type invalid value: '{dt}'"

    conf = extracted.get("extraction_confidence")
    if conf is not None and conf not in ALLOWED_CONFIDENCE:
        return f"extraction_confidence invalid value: '{conf}'"

    # --- Date format validation (only when non-null) ---
    for date_field in ["doc_date", "contract_start_date", "contract_end_date"]:
        val = extracted.get(date_field)
        if val is not None and not DATE_PATTERN.match(str(val)):
            return f"{date_field} invalid format: '{val}' — expected YYYY-MM-DD"

    # --- Numeric type coercion check ---
    for float_field in ["total_contract_value", "modification_financial_delta"]:
        val = extracted.get(float_field)
        if val is not None:
            try:
                float(val)
            except (TypeError, ValueError):
                return f"{float_field} could not be coerced to float: '{val}'"

    for int_field in ["termination_notice_days"]:
        val = extracted.get(int_field)
        if val is not None:
            try:
                int(val)
            except (TypeError, ValueError):
                return f"{int_field} could not be coerced to int: '{val}'"

    return None  # Validation passed
```

### 4.7 Result Assembly

`_assemble_result()` maps the validated extraction dict to a flat dict whose keys match the
SQLite column names exactly. This is the handoff contract to the DB writer (Task 2.5) — no
further key mapping or transformation happens downstream.

```python
def _assemble_result(filename: str, doc_type: str, extracted: dict) -> dict:
    """
    Map validated extraction output to a flat dict matching SQLite column names.
    All 19 schema columns are present as keys; missing values default to None.
    """
    return {
        # Row identity (DB writer adds id and pipeline_run_timestamp)
        "source_filename":              filename,

        # Group A — Universal Spine
        "contract_number":              extracted.get("contract_number"),
        "doc_type":                     extracted.get("doc_type"),
        "vendor_name":                  extracted.get("vendor_name"),
        "doc_date":                     extracted.get("doc_date"),
        "county_department":            extracted.get("county_department"),

        # Group B — Financial Exposure
        "total_contract_value":         _to_float(extracted.get("total_contract_value")),
        "price_escalator_terms":        extracted.get("price_escalator_terms"),
        "modification_financial_delta": _to_float(extracted.get("modification_financial_delta")),

        # Group C — Term and Renewal Exposure
        "contract_start_date":          extracted.get("contract_start_date"),
        "contract_end_date":            extracted.get("contract_end_date"),
        "renewal_options":              extracted.get("renewal_options"),
        "auto_renewal_flag":            _to_bool_int(extracted.get("auto_renewal_flag")),
        "termination_notice_days":      _to_int(extracted.get("termination_notice_days")),

        # Group D — Vendor and Compliance Risk
        "service_category":             extracted.get("service_category"),
        "procurement_vehicle":          extracted.get("procurement_vehicle"),
        "insurance_requirements_flag":  _to_bool_int(extracted.get("insurance_requirements_flag")),

        # Group E — Document Linkage
        "parent_contract_number":       extracted.get("parent_contract_number"),

        # Pipeline metadata
        "extraction_confidence":        extracted.get("extraction_confidence"),
        "extraction_notes":             extracted.get("extraction_notes"),
    }


# --- Type coercion helpers ---

def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

def _to_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None

def _to_bool_int(val) -> int | None:
    """Convert boolean to SQLite integer (1/0) or None."""
    if val is None:
        return None
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, int) and val in (0, 1):
        return val
    if isinstance(val, str):
        if val.lower() in ("true", "1", "yes"):
            return 1
        if val.lower() in ("false", "0", "no"):
            return 0
    return None
```

### 4.8 Failure Record Schema

Documents that are skipped or fail produce a failure record rather than an extraction result.
Failure records are written to `outputs/extraction_failures.csv` by the batch function.

```python
def _make_failure_record(
    filename: str,
    doc_type: str,
    status: str,     # 'skipped' or 'failed'
    reason: str,
) -> dict:
    return {
        "source_filename":    filename,
        "doc_type":           doc_type,
        "extraction_status":  status,
        "failure_reason":     reason,
        # All schema fields are None on failure records — they are not written to the DB
    }
```

---

## 5. Batch Function and Run Outputs

### 5.1 Batch Function

```python
def extract_batch(
    parse_results: list[dict],
    classification_results: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Run extraction over a list of parsed and classified documents.

    Args:
        parse_results:          List of parse result dicts from pdf_parser.parse_directory()
        classification_results: List of classification result dicts from
                                document_classifier.classify_directory()
                                Must be in the same order as parse_results (matched by filename).

    Returns:
        (successes, failures) — two lists:
          successes: extraction result dicts with extraction_status='success', DB-ready
          failures:  failure record dicts with extraction_status='skipped' or 'failed'

    Token usage summary is logged and written after all documents are processed.
    """
```

Implementation note: match parse and classification results by `filename` rather than assuming
list order, to be robust against any reordering in the pipeline orchestrator.

### 5.2 Tracking Files

After `extract_batch()` completes, write two CSV files:

**`outputs/extraction_results.csv`** — one row per successfully extracted document

| Column | Description |
|--------|-------------|
| `source_filename` | Source PDF filename |
| `contract_number` | Extracted contract number |
| `doc_type` | Document type |
| `vendor_name` | Extracted vendor name |
| `extraction_confidence` | `high` / `medium` / `low` |
| `extraction_notes` | Flagged issues, if any |

**`outputs/extraction_failures.csv`** — one row per failed or skipped document

| Column | Description |
|--------|-------------|
| `source_filename` | Source PDF filename |
| `doc_type` | Classified doc type (or `unknown` if classification also failed) |
| `extraction_status` | `failed` or `skipped` |
| `failure_reason` | Human-readable reason string |

### 5.3 Run-Level Token Usage Summary

After `extract_batch()` completes, call `get_token_totals()` from `llm_client.py` and log
the run summary at INFO level and write it to `outputs/extraction_token_summary.txt`.

```
=== Extraction Run — Token Usage Summary ===
Run timestamp:      2024-01-15 10:45:00
Documents processed: 100
  Successful:        87
  Skipped:            8
  Failed:             5

--- Token Usage by Task ---
  Classification (LLM fallback only):
    Input tokens:    12,450
    Output tokens:     1,820

  Extraction:
    Input tokens:   842,300
    Output tokens:    28,140

  Total input tokens:   854,750
  Total output tokens:   29,960

--- Estimated Cost (indicative) ---
  Model (extraction):   claude-sonnet-4-5
  Model (classification): claude-haiku-4-5
  Note: Check current Anthropic pricing at https://www.anthropic.com/pricing

Tracking files written to:
  outputs/extraction_results.csv
  outputs/extraction_failures.csv
  outputs/extraction_token_summary.txt
```

Cost is noted as indicative rather than computed, since pricing changes over time and is
better looked up than hardcoded. The token totals give the implementer enough information
to calculate it manually in seconds.

---

## 6. Single-Document Test Mode

To support Task 2.4 (manual prompt iteration before full-scale run), the extractor exposes
a simple test entry point callable without any batch infrastructure or SQLite setup.

```python
def test_single_document(pdf_path: str) -> dict:
    """
    Parse, classify, and extract a single document end-to-end.
    Prints results to stdout for manual inspection.
    No database interaction. No tracking files written.

    Usage:
        python -m extractor --test path/to/document.pdf

    Returns the extraction result dict (success or failure record).
    """
    from pdf_parser import parse_pdf
    from document_classifier import classify_document

    print(f"\n{'='*60}")
    print(f"TEST RUN: {pdf_path}")
    print(f"{'='*60}\n")

    # Parse
    parse_result = parse_pdf(pdf_path)
    print(f"[Parser]  pages={parse_result['page_count']}  "
          f"chars={len(parse_result['text'])}  "
          f"scanned={parse_result['is_scanned']}")

    # Classify
    classification_result = classify_document(parse_result)
    print(f"[Classifier]  doc_type={classification_result['doc_type']}  "
          f"confidence={classification_result['confidence']}  "
          f"method={classification_result['classification_method']}")

    # Extract
    result = extract_document(parse_result, classification_result)
    print(f"\n[Extractor]  status={result['extraction_status']}")
    print(f"\n--- Extraction Result ---")
    print(json.dumps(result, indent=2, default=str))

    # Token summary
    totals = get_token_totals()
    print(f"\n--- Token Usage ---")
    print(json.dumps(totals, indent=2))

    return result
```

CLI invocation:
```bash
python -m src.extractor --test contracts/sample/renewal_letter_23159.pdf
python -m src.extractor --test contracts/sample/agreement_22847.pdf
```

This is the primary tool for Task 2.4 iteration. Run it on 5–10 documents across at least
3 doc types before running the full batch.

---

## 7. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Model for extraction | `claude-sonnet-4-5` | Complex prompt with 12–17 fields, long documents, inferred fields; stronger instruction following justifies the cost difference at PoC scale |
| Model for classification | `claude-haiku-4-5` | Lightweight task; rule-based handles most cases; LLM fallback only needs to choose among 6 enum values from a 2,000-char window |
| Model config location | `MODEL_CONFIG` dict at top of `llm_client.py` | Single location for all model identifiers; changing one line propagates everywhere; supports easy A/B testing across runs |
| Text truncation | None — full document text passed | Exhibit A (pricing, scope detail) typically appears late in long documents; truncation would null out `total_contract_value` on fully executed agreements. PoC scope decision; production would require a chunking or hierarchical extraction strategy at scale |
| Null validation scope | Spine fields only (`contract_number`, `vendor_name`, `doc_type`) | Type-specific nulls are "by design" per the coverage matrix; failing a VDS row because `total_contract_value` is null would produce false failure signals |
| VDS extraction | Full prompt, same as all other types | Consistent approach; avoids per-type skip logic that would need maintenance; minimal cost impact at PoC scale (~12 docs) |
| Failure handling | Failure records written to CSV; main table unaffected | DB writer sees only validated, status='success' rows; failures are reviewable without querying the DB |
| Abort on consecutive failures | Not implemented for PoC | Manual execution at small scale; operator will observe failures in real time; simpler code |
| DB write ownership | DB writer (Task 2.5), not extractor | Extractor returns dicts; writing is a separate concern; keeps extractor independently testable without DB setup |
| Embeddings provider | Local sentence-transformers via ChromaDB default | No second API provider, no additional credentials, zero cost; sufficient for PoC-scale semantic search over ~100 documents; production would upgrade to a hosted embedding API |

---

## 8. PoC Scope Boundaries (Not In Scope)

The following are explicitly out of scope for the PoC. Document in the README as production
hardening items.

- **Token-aware truncation for long documents.** At thousands of documents, per-call input
  token cost on fully executed agreements (10–20+ pages) would be material. A production
  pipeline would implement hierarchical extraction: summarize long sections first, then
  extract fields from summaries where full-text extraction is unnecessary.
- **Field-level confidence scoring.** The current schema captures document-level confidence
  only (`extraction_confidence`: high/medium/low). Production hardening would add per-field
  confidence or uncertainty flags, particularly for inferred fields (`service_category`,
  `auto_renewal_flag`, `price_escalator_terms`).
- **Streaming responses.** The extractor waits for complete responses before parsing. For
  very long documents, streaming would reduce perceived latency but complicates JSON parsing.
  Out of scope for PoC.
- **Parallel/async extraction.** Sequential processing is fine for 100 documents. At scale,
  async calls with rate-limit-aware concurrency control would be a first-class requirement.
- **Prompt versioning.** The extraction prompt is a single file for the PoC. Production would
  version prompts (hash or semver) and store the version alongside each extraction record
  to support reproducibility and degradation detection.
- **Abort-on-consecutive-failure guard.** Not implemented; see Key Decisions above.