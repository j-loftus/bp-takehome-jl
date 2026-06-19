# Task 2.2 — Document Classification Module: Implementation Requirements

> **Purpose:** Specifies the document classification module for the contract intelligence pipeline.
> This module runs as Step 1 — after PDF parsing (Task 2.1) and before LLM extraction (Task 2.3).
> Its output, a `doc_type` enum value and confidence level, is the direct input to the extraction
> prompt's field injection logic (Task 1.3). A misclassification here propagates silently into
> extraction; classifier accuracy is therefore a first-class dependency of the entire pipeline.
>
> **Dependencies:** Task 2.1 (PDF parsing module) — consumes the `text`, `filename`,
> `is_scanned`, and `page_count` fields from the parse result dict.
> Downstream consumers: Task 2.3 (LLM extractor), Task 1.3 (prompt builder).

---

## 1. Architecture Decision

### Approach: Hybrid — Rule-Based First, LLM Fallback

Classification uses a two-stage hybrid approach:

1. **Stage 1 — Rule-based detector.** Evaluate a set of high-precision keyword and structural
   rules against a 2,000-character text window from the start of the extracted document text.
   Rules are ordered to prevent overlap errors. If a rule matches, assign the doc type immediately
   and skip Stage 2.

2. **Stage 2 — LLM classifier.** If no rule matches, send the same 2,000-character window to
   the LLM with a lightweight classification prompt. The LLM returns a `doc_type` enum value,
   a confidence level, and a brief reasoning string. If the LLM call fails or returns low
   confidence, assign `other` as the conservative fallback.

**Why hybrid over LLM-only:** Several doc types are structurally unambiguous — their
distinguishing signals are fixed phrases that appear in every instance (e.g., "Vendor Disclosure
Statement" header, "is hereby renewed" body language). Burning LLM tokens on these is wasteful.
The rule-based path handles them at near-100% accuracy with zero API cost. The LLM handles only
the genuinely ambiguous cases.

**Why 2,000 characters, not page 1:** A strict page-1 cutoff risks missing substantive content
when a document opens with a cover page, logo block, or address header. A 2,000-character window
from the start of the full extracted text reaches the first substantive section regardless of
where the page break falls, and is short enough to keep classification calls cheap.

---

## 2. Module Interface

**File:** `src/document_classifier.py`

**Primary function:**

```python
def classify_document(parse_result: dict) -> dict:
    """
    Classify a single parsed document into one of six doc type categories.

    Input: parse_result dict from pdf_parser.parse_pdf(), containing at minimum:
        text        (str)   — full extracted text with PAGE BREAK separators
        filename    (str)   — basename of the source file
        is_scanned  (bool)  — True if the document is image-only
        page_count  (int)   — total page count

    Returns a classification result dict with the following keys:
        filename        (str)   — passed through from parse_result
        doc_type        (str)   — one of the six allowed enum values (see Section 3)
        confidence      (str)   — 'high' / 'medium' / 'low'
        classification_method (str) — 'rule_based' or 'llm'
        reasoning       (str | None) — LLM reasoning string if llm path; None for rule_based
        classification_error (str | None) — error message if classification failed; None on success
    """
```

**Batch function:**

```python
def classify_directory(parse_results: list[dict]) -> list[dict]:
    """
    Classify a list of parse result dicts. Returns a list of classification result
    dicts (one per document) in the format returned by classify_document().
    Documents flagged as scanned (is_scanned=True) are assigned doc_type='other'
    and confidence='low' without invoking rules or LLM.
    """
```

---

## 3. Allowed Doc Type Values

The classifier must return exactly one of these six enum values:

| Enum Value | Human Label |
|------------|-------------|
| `fully_executed_agreement` | Fully Executed Agreement |
| `renewal_letter` | Renewal Letter |
| `modification_amendment` | Modification / Amendment |
| `award_letter` | Award / Intent-to-Award Letter |
| `vendor_disclosure_statement` | Vendor Disclosure Statement |
| `other` | Other |

---

## 4. Text Window Preparation

Before running either stage, extract the classification text window:

```python
def _get_classification_window(text: str, max_chars: int = 2000) -> str:
    """
    Return the first max_chars characters of the extracted document text.
    No page boundary logic — slice from position 0 regardless of PAGE BREAK markers.
    Strip leading/trailing whitespace from the result.
    """
    return text[:max_chars].strip()
```

This window is used as input to both the rule-based detector and the LLM classifier prompt.
Do not apply any further normalization — preserve original casing, punctuation, and whitespace,
as the rule patterns depend on them.

---

## 5. Stage 1 — Rule-Based Detector

### Evaluation Order

Rules must be evaluated in the following order. The first match wins; subsequent rules are
not evaluated. This ordering prevents overlap errors, particularly between
`modification_amendment` and `fully_executed_agreement`.

```
1. vendor_disclosure_statement
2. renewal_letter
3. award_letter
4. modification_amendment
5. fully_executed_agreement
```

A document that does not match any rule passes to Stage 2 (LLM).

---

### Rule Definitions

All pattern matching is **case-insensitive** unless noted otherwise.

---

#### Rule 1 — `vendor_disclosure_statement`

Match if **both** of the following are present in the text window:

- The phrase `"vendor disclosure statement"` appears anywhere in the text window
- At least one of the following appears: `"familial relationships"` OR `"campaign contributions"`

**Rationale:** The VDS form has a fixed header and two fixed section labels that appear in every
instance. Both signals together constitute a near-certain identification. Either signal alone
could theoretically appear in other document types.

```python
def _is_vds(window: str) -> bool:
    w = window.lower()
    has_header = "vendor disclosure statement" in w
    has_section = "familial relationships" in w or "campaign contributions" in w
    return has_header and has_section
```

---

#### Rule 2 — `renewal_letter`

Match if **at least one** of the following phrases is present in the text window:

- `"is hereby renewed"`
- `"is being renewed"`
- `"extended for one additional year"`
- `"renewal of contract"`
- `"contract renewal"`

**Rationale:** Renewal letters are highly templated. These phrases appear in the body of every
renewal letter and are unlikely to appear in any other doc type in this corpus. The rule uses
an OR condition because the exact phrasing varies slightly across purchasing agents and time
periods.

```python
RENEWAL_PHRASES = [
    "is hereby renewed",
    "is being renewed",
    "extended for one additional year",
    "renewal of contract",
    "contract renewal",
]

def _is_renewal_letter(window: str) -> bool:
    w = window.lower()
    return any(phrase in w for phrase in RENEWAL_PHRASES)
```

---

#### Rule 3 — `award_letter`

Match if **at least one** of the following is present in the text window:

- `"intent to award"` or `"intent-to-award"`
- `"this is not an order"`
- `"award of contract"`

**Rationale:** These phrases are standard closing and header language for award letters and do
not appear in other doc types. "This is not an order" is the most reliable signal — it is
boilerplate closing language present in virtually all simple award letters.

```python
AWARD_PHRASES = [
    "intent to award",
    "intent-to-award",
    "this is not an order",
    "award of contract",
]

def _is_award_letter(window: str) -> bool:
    w = window.lower()
    return any(phrase in w for phrase in AWARD_PHRASES)
```

---

#### Rule 4 — `modification_amendment`

Match if **at least one** of the following is present in the text window:

- `"amendment number"` or `"modification number"`
- `"amendment to"` in combination with `"agreement"` (both present in window)
- `"modification to"` in combination with `"agreement"` (both present in window)
- `"whereas, the parties entered into"` or `"whereas, the county and"` (recital patterns
  referencing a prior agreement)

**Rationale:** Amendments reference a prior agreement in their opening recitals. The WHEREAS
recital pattern combined with past-tense framing ("entered into") is the most reliable structural
signal. The "amendment number" / "modification number" phrase catches formally numbered
amendments that lead with their own identifier.

**Important:** This rule is evaluated *before* the fully executed agreement rule specifically
to avoid misclassifying a well-structured amendment as a base agreement.

```python
def _is_modification(window: str) -> bool:
    w = window.lower()
    # Explicitly numbered amendment or modification
    if "amendment number" in w or "modification number" in w:
        return True
    # "Amendment to [the] Agreement" phrasing
    if "amendment to" in w and "agreement" in w:
        return True
    # "Modification to [the] Agreement" phrasing
    if "modification to" in w and "agreement" in w:
        return True
    # WHEREAS recital referencing a prior agreement
    if "whereas, the parties entered into" in w:
        return True
    if "whereas, the county and" in w and "entered into" in w:
        return True
    return False
```

---

#### Rule 5 — `fully_executed_agreement`

Match if **all three** of the following are present in the text window:

- `"agreement"` or `"contract"` (as a standalone word, not just within a phrase)
- At least one of: `"scope of work"`, `"exhibit a"`, `"scope of services"`
- At least one of: `"initial term"`, `"effective date"`, `"term of agreement"`, `"term of contract"`

**Rationale:** Fully executed agreements are the broadest category and are checked last.
Requiring all three signals together (agreement/contract identity + scope reference + term
language) reduces false positives from documents that use subset of these terms. The rule
does not fire on modifications or renewals because those were matched earlier.

```python
AGREEMENT_IDENTITY = ["agreement", "contract"]
SCOPE_SIGNALS = ["scope of work", "exhibit a", "scope of services"]
TERM_SIGNALS = ["initial term", "effective date", "term of agreement", "term of contract"]

def _is_fully_executed(window: str) -> bool:
    w = window.lower()
    has_identity = any(term in w for term in AGREEMENT_IDENTITY)
    has_scope = any(term in w for term in SCOPE_SIGNALS)
    has_term = any(term in w for term in TERM_SIGNALS)
    return has_identity and has_scope and has_term
```

---

### Rule-Based Confidence Assignment

All rule-based matches are assigned `confidence = 'high'` by default, with two exceptions:

- The `fully_executed_agreement` rule is assigned `confidence = 'medium'` — it is the broadest
  rule and most likely to produce edge-case matches.
- The `modification_amendment` rule is assigned `confidence = 'medium'` — amendment formats vary
  across the corpus and some may not carry all expected signals.

| Rule | Default Confidence |
|------|--------------------|
| `vendor_disclosure_statement` | `high` |
| `renewal_letter` | `high` |
| `award_letter` | `high` |
| `modification_amendment` | `medium` |
| `fully_executed_agreement` | `medium` |

---

### Rule-Based Orchestration

```python
def _classify_by_rules(window: str) -> tuple[str | None, str | None]:
    """
    Run rules in priority order. Returns (doc_type, confidence) if matched,
    or (None, None) if no rule matches.
    """
    if _is_vds(window):
        return "vendor_disclosure_statement", "high"
    if _is_renewal_letter(window):
        return "renewal_letter", "high"
    if _is_award_letter(window):
        return "award_letter", "high"
    if _is_modification(window):
        return "modification_amendment", "medium"
    if _is_fully_executed(window):
        return "fully_executed_agreement", "medium"
    return None, None
```

---

## 6. Stage 2 — LLM Classifier

### When the LLM Is Invoked

The LLM classifier is invoked only when `_classify_by_rules()` returns `(None, None)`.
It is never invoked for scanned documents (`is_scanned = True`).

---

### Classification Prompt

```python
CLASSIFICATION_PROMPT = """
You are a document classification specialist. Your task is to identify the type of a
procurement contract document based on a short text excerpt and return a structured
JSON response.

## Document Types

Classify the document as exactly one of the following six types:

1. **fully_executed_agreement** — A signed contract between the issuing organization
   (Lake County, IL) and a vendor. Typically 10–20+ pages. Contains numbered sections
   covering scope of work, term, price, insurance, termination, and signatures from
   both parties. The originating document in a contract family.

2. **renewal_letter** — A short templated letter from the purchasing division to a
   vendor extending an existing contract for one additional year. Always references a
   specific contract number and states the new contract period. Signed by the
   Purchasing Agent. Page 2 (not shown here) is always a blank VDS form — ignore it.

3. **modification_amendment** — A formal amendment to an existing executed agreement.
   Opens with WHEREAS recitals referencing the prior agreement. Contains new or
   revised terms (scope, pricing, or term extension). Signed by both parties.

4. **award_letter** — A letter from the purchasing division notifying a vendor they
   have been awarded a contract. May be a simple 1-page award letter or a longer
   intent-to-award letter with a full bid tab and unit pricing.

5. **vendor_disclosure_statement** — A 1-page compliance form requiring vendors to
   disclose familial relationships with county officials and campaign contributions.
   Contains "Vendor Disclosure Statement" in the header.

6. **other** — Any document that does not clearly fit the above types. Includes price
   increase letters, task orders, statements of work filed separately, 60-day
   extensions, bid documents, cooperative procurement references, and redacted files.

## Instructions

- Read the document excerpt below carefully.
- Assign the single best-fitting doc type from the six options above.
- If you are not confident, assign "other" rather than guessing.
- Return your response as a valid JSON object with exactly three keys:
    - "doc_type": one of the six enum values above (string)
    - "confidence": "high", "medium", or "low" (string)
    - "reasoning": a single sentence explaining the primary signal that drove your
      classification (string) — this is used for debugging and evaluation only

## Confidence Guidelines

- "high": Strong, unambiguous signals. You are confident in the classification.
- "medium": Plausible classification but some signals are missing or ambiguous.
- "low": Limited signals; document is unusual or content is sparse.

## Output Format

Return only a valid JSON object. No preamble, no explanation, no markdown fences.

Example:
{
  "doc_type": "renewal_letter",
  "confidence": "high",
  "reasoning": "Document contains 'is hereby renewed' language and references a
                specific contract period with a Purchasing Agent signature block."
}

## Document Excerpt

{text_window}
"""
```

---

### LLM Call and Response Handling

```python
def _classify_by_llm(window: str) -> tuple[str, str, str | None, str | None]:
    """
    Send text window to LLM for classification.

    Returns:
        doc_type        (str) — classified doc type enum value
        confidence      (str) — 'high' / 'medium' / 'low'
        reasoning       (str | None) — LLM reasoning string
        error           (str | None) — error message if call failed
    """
    prompt = CLASSIFICATION_PROMPT.format(text_window=window)

    try:
        response = call_llm(prompt)  # use the project's shared LLM client
        parsed = json.loads(response)

        doc_type = parsed.get("doc_type", "other")
        confidence = parsed.get("confidence", "low")
        reasoning = parsed.get("reasoning", None)

        # Validate enum value — default to 'other' if LLM returns unexpected value
        allowed = {
            "fully_executed_agreement", "renewal_letter", "modification_amendment",
            "award_letter", "vendor_disclosure_statement", "other"
        }
        if doc_type not in allowed:
            return "other", "low", reasoning, f"LLM returned invalid doc_type: {doc_type}"

        # If LLM returned low confidence, override to 'other' as conservative fallback
        if confidence == "low":
            return "other", "low", reasoning, None

        return doc_type, confidence, reasoning, None

    except json.JSONDecodeError as e:
        return "other", "low", None, f"LLM response not valid JSON: {str(e)}"
    except Exception as e:
        return "other", "low", None, f"LLM classification call failed: {str(e)}"
```

---

## 7. Scanned Document Handling

Documents flagged as `is_scanned = True` by the PDF parser bypass both stages:

```python
if parse_result.get("is_scanned"):
    return {
        "filename": parse_result["filename"],
        "doc_type": "other",
        "confidence": "low",
        "classification_method": "rule_based",
        "reasoning": None,
        "classification_error": "Document is scanned or image-only; classification skipped"
    }
```

These documents are assigned `other` / `low` confidence and will be excluded from extraction
in Task 2.3.

---

## 8. Full Classification Orchestration

```python
def classify_document(parse_result: dict) -> dict:

    filename = parse_result.get("filename", "unknown")
    text = parse_result.get("text", "")

    # Handle scanned documents
    if parse_result.get("is_scanned"):
        return {
            "filename": filename,
            "doc_type": "other",
            "confidence": "low",
            "classification_method": "rule_based",
            "reasoning": None,
            "classification_error": "Document is scanned or image-only; classification skipped"
        }

    # Handle empty text (parse failure that wasn't flagged as scanned)
    if not text.strip():
        return {
            "filename": filename,
            "doc_type": "other",
            "confidence": "low",
            "classification_method": "rule_based",
            "reasoning": None,
            "classification_error": "No extractable text; classification skipped"
        }

    # Prepare 2,000-character window
    window = _get_classification_window(text, max_chars=2000)

    # Stage 1: Rule-based
    doc_type, confidence = _classify_by_rules(window)

    if doc_type is not None:
        return {
            "filename": filename,
            "doc_type": doc_type,
            "confidence": confidence,
            "classification_method": "rule_based",
            "reasoning": None,
            "classification_error": None
        }

    # Stage 2: LLM fallback
    doc_type, confidence, reasoning, error = _classify_by_llm(window)

    return {
        "filename": filename,
        "doc_type": doc_type,
        "confidence": confidence,
        "classification_method": "llm",
        "reasoning": reasoning,
        "classification_error": error
    }
```

---

## 9. Output for Downstream Modules

The dict returned by `classify_document()` is the handoff contract to all downstream consumers.

**Extractor (Task 2.3)** consumes: `doc_type`, `confidence`, `filename`

**Prompt builder (Task 1.3)** consumes: `doc_type` — used to select the injected field block

**Eval harness (Task 3.1)** consumes: `doc_type`, `confidence`, `classification_method`,
`reasoning` — all four fields are needed for classifier accuracy evaluation

The `reasoning` field is not written to the SQLite database. It exists for debugging and
evaluation only.

---

## 10. Logging

Use Python's standard `logging` module. The module should emit:

- `INFO`: one line per document classified, including filename, assigned doc type,
  confidence, and method (rule_based / llm)
- `WARNING`: for documents assigned `other` via LLM fallback, for low-confidence
  classifications, and for any classification errors
- `DEBUG`: rule match details and LLM response payloads (off by default)

Log format should include the module name:

```
2024-01-15 10:23:05 [document_classifier] [INFO] renewal_letter_23159.pdf → renewal_letter (high, rule_based)
2024-01-15 10:23:06 [document_classifier] [INFO] agreement_22001.pdf → fully_executed_agreement (medium, rule_based)
2024-01-15 10:23:07 [document_classifier] [INFO] unknown_doc_99999.pdf → other (low, llm) — LLM fallback
2024-01-15 10:23:08 [document_classifier] [WARNING] redacted_file.pdf → other (low, rule_based) — scanned document
```

---

## 11. Classification Results Tracking File

After `classify_directory()` completes, write a CSV tracking file. This is the audit log for
the classification run and the primary input to classifier accuracy evaluation in Task 3.1.

**File:** `outputs/classification_results.csv`

**Columns:**

| Column | Type | Description |
|--------|------|-------------|
| `filename` | string | Basename of the source PDF file |
| `doc_type` | string | Assigned doc type enum value |
| `confidence` | string | `high` / `medium` / `low` |
| `classification_method` | string | `rule_based` or `llm` |
| `reasoning` | string | LLM reasoning string; empty for rule_based |
| `classification_error` | string | Error message if classification failed; empty if successful |

```python
def write_classification_results(results: list[dict], output_path: str) -> None:
    """
    Write classification results to a CSV tracking file. One row per document.
    Overwrites any existing file at output_path.
    """
```

Call this function at the end of `classify_directory()` automatically.

---

## 12. Classification Run Summary

After writing the tracking CSV, print a summary to the console (`logging.INFO`) and write it
as a plain text file at `outputs/classification_summary.txt`.

```
=== Document Classification Run Summary ===
Run timestamp:      2024-01-15 10:24:00
Documents processed: 100

--- Doc Type Distribution ---
  fully_executed_agreement:    22  (22.0%)
  renewal_letter:              27  (27.0%)
  modification_amendment:      13  (13.0%)
  award_letter:                11  (11.0%)
  vendor_disclosure_statement: 12  (12.0%)
  other:                       15  (15.0%)

--- Classification Method ---
  Rule-based:    81  (81.0%)
  LLM fallback:  19  (19.0%)

--- Confidence Distribution ---
  High:    68  (68.0%)
  Medium:  24  (24.0%)
  Low:      8   (8.0%)

--- Low-Confidence Documents (review recommended) ---
  unknown_format_99999.pdf   — other (low, llm)
  redacted_agreement_22001.pdf — other (low, rule_based) [scanned]
  [list all low-confidence documents]

--- Classification Errors ---
  corrupted_file.pdf — LLM classification call failed: [error message]
  [list all documents with classification_error populated]

Tracking file written to: outputs/classification_results.csv
```

```python
def print_classification_summary(results: list[dict], output_path: str) -> None:
    """
    Print and write a classification run summary.
    results: list of dicts from classify_directory()
    output_path: path to write classification_summary.txt
    """
```

Call this function at the end of `classify_directory()` immediately after
`write_classification_results()`.

---

## 13. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Classification approach | Hybrid: rules first, LLM fallback | Minimizes LLM calls; high-confidence types handled cheaply by rules; LLM reserved for genuinely ambiguous cases |
| Text window | First 2,000 characters of full extracted text | Character limit is more robust than page-1 cutoff; catches substantive content even when page 1 is a cover or logo block |
| Rule evaluation order | VDS → Renewal → Award → Modification → Agreement | Prevents overlap; Modification checked before Agreement to avoid misclassifying structured amendments as base contracts |
| Low-confidence LLM response | Override to `other` | Conservative fallback; a wrong doc type fed to the extractor is worse than an `other` that gets flagged for review |
| Confidence for rule matches | `high` for VDS/Renewal/Award; `medium` for Modification/Agreement | Reflects observed reliability; Agreement rule is broadest and most prone to edge cases |
| `reasoning` field | Captured but not written to SQLite | Debugging and eval use only; not a structured data field |
| Scanned document handling | Assign `other` / `low`, skip both stages | No text to classify; consistent with PDF parser's scanned flag |

---

## 14. PoC Scope Boundaries (Not In Scope)

The following are explicitly out of scope for the PoC and should be noted in the README as
production hardening items:

- **Filename-based classification signals.** Filenames in this corpus are often informative
  (e.g., `renewal_letter_23159.pdf`) but are inconsistent and cannot be relied upon. The
  classifier uses document text only.
- **Multi-label classification.** Each document receives a single doc type. Documents that
  span categories (rare) are assigned the primary type.
- **Confidence calibration.** The `high` / `medium` / `low` scale is heuristic for the PoC.
  A production system would calibrate confidence scores against a labeled validation set.
- **Active learning loop.** Low-confidence and `other` documents flagged by this module could
  seed a human-review queue that feeds back into rule refinement or fine-tuning. Out of scope
  for PoC.
- **Classifier fine-tuning.** The LLM is used zero-shot. A production hardening item would be
  few-shot examples added to the classification prompt based on observed failure modes from
  the eval harness.