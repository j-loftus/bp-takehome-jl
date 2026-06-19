"""
Document type classifier — Step 1 before field extraction.

Classifies each document into one of the defined taxonomy types using a
hybrid approach: rule-based detection first, LLM fallback for ambiguous cases.
"""

import csv
import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

from src.llm_client import LLMCallError, call_llm, call_llm_with_images, extract_json
from src.pipeline.pdf_parser import extract_page_images

logger = logging.getLogger("document_classifier")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [document_classifier] [%(levelname)s] %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Doc type enum
# ---------------------------------------------------------------------------

class DocType(str, Enum):
    FULLY_EXECUTED_AGREEMENT    = "fully_executed_agreement"
    RENEWAL_LETTER              = "renewal_letter"
    MODIFICATION_AMENDMENT      = "modification_amendment"
    AWARD_LETTER                = "award_letter"
    VENDOR_DISCLOSURE_STATEMENT = "vendor_disclosure_statement"
    OTHER                       = "other"

    @property
    def label(self) -> str:
        """Human-readable display string for use in prompts and UI."""
        return {
            "fully_executed_agreement":    "Fully Executed Agreement",
            "renewal_letter":              "Renewal Letter",
            "modification_amendment":      "Modification/Amendment",
            "award_letter":                "Award/Intent-to-Award Letter",
            "vendor_disclosure_statement": "Vendor Disclosure Statement",
            "other":                       "Other",
        }[self.value]


# ---------------------------------------------------------------------------
# Text window extraction
# ---------------------------------------------------------------------------

def _get_classification_window(text: str, max_chars: int = 2000) -> str:
    """Return the first max_chars characters of extracted text, stripped."""
    return text[:max_chars].strip()


# ---------------------------------------------------------------------------
# Stage 1 — Rule-based detector
# ---------------------------------------------------------------------------

def _is_vds(window: str) -> bool:
    w = window.lower()
    has_header = "vendor disclosure statement" in w
    has_section = "familial relationships" in w or "campaign contributions" in w
    return has_header and has_section


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


AWARD_PHRASES = [
    "intent to award",
    "intent-to-award",
    "this is not an order",
    "award of contract",
]

def _is_award_letter(window: str) -> bool:
    w = window.lower()
    return any(phrase in w for phrase in AWARD_PHRASES)


def _is_modification(window: str) -> bool:
    w = window.lower()
    if "amendment number" in w or "modification number" in w:
        return True
    if "amendment to" in w and "agreement" in w:
        return True
    if "modification to" in w and "agreement" in w:
        return True
    if "whereas, the parties entered into" in w:
        return True
    if "whereas, the county and" in w and "entered into" in w:
        return True
    return False


AGREEMENT_IDENTITY = ["agreement", "contract"]
SCOPE_SIGNALS     = ["scope of work", "exhibit a", "scope of services"]
TERM_SIGNALS      = ["initial term", "effective date", "term of agreement", "term of contract"]

def _is_fully_executed(window: str) -> bool:
    w = window.lower()
    has_identity = any(term in w for term in AGREEMENT_IDENTITY)
    has_scope    = any(term in w for term in SCOPE_SIGNALS)
    has_term     = any(term in w for term in TERM_SIGNALS)
    return has_identity and has_scope and has_term


def _classify_by_rules(window: str) -> tuple[str | None, str | None]:
    """
    Run rules in priority order. Returns (doc_type, confidence) if matched,
    or (None, None) if no rule matches.
    """
    if _is_vds(window):
        logger.debug("Rule match: vendor_disclosure_statement")
        return "vendor_disclosure_statement", "high"
    if _is_renewal_letter(window):
        logger.debug("Rule match: renewal_letter")
        return "renewal_letter", "high"
    if _is_award_letter(window):
        logger.debug("Rule match: award_letter")
        return "award_letter", "high"
    if _is_modification(window):
        logger.debug("Rule match: modification_amendment")
        return "modification_amendment", "medium"
    if _is_fully_executed(window):
        logger.debug("Rule match: fully_executed_agreement")
        return "fully_executed_agreement", "medium"
    return None, None


# ---------------------------------------------------------------------------
# Stage 2 — LLM fallback
# ---------------------------------------------------------------------------

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
{{
  "doc_type": "renewal_letter",
  "confidence": "high",
  "reasoning": "Document contains 'is hereby renewed' language and references a specific contract period with a Purchasing Agent signature block."
}}

## Document Excerpt

{text_window}
"""

_ALLOWED_DOC_TYPES = {
    "fully_executed_agreement", "renewal_letter", "modification_amendment",
    "award_letter", "vendor_disclosure_statement", "other"
}


_VISION_CLASSIFICATION_PROMPT = """
You are classifying a scanned contract document from a municipal procurement archive.
Based on the document images provided, identify which of the following document types best describes this document.

Return a JSON object with exactly three fields:
{"doc_type": "<value>", "confidence": "<value>", "reasoning": "<one sentence>"}

Allowed values for doc_type:
- "fully_executed_agreement" — a signed contract between the county and a vendor
- "renewal_letter" — a letter exercising a renewal option on an existing contract
- "modification_amendment" — a formal amendment or modification to an existing contract
- "award_letter" — a letter notifying a vendor of contract award or intent to award
- "vendor_disclosure_statement" — a vendor disclosure or conflict of interest form
- "other" — does not fit any category above

Allowed values for confidence: "high", "medium", "low"

Return only the JSON object. No other text.
""".strip()


def _classify_by_vision(filepath: str) -> tuple[str, str, str | None, str | None]:
    """
    Classify a scanned document by sending the first 2 page images to the vision LLM.
    Returns (doc_type, confidence, reasoning, error) — same shape as _classify_by_llm.
    Falls back to ("other", "low", None, error_message) on any failure.
    """
    if not filepath:
        return "other", "low", None, "No filepath provided for vision classification"

    page_images = extract_page_images(filepath, dpi=150, max_pages=2)
    if not page_images:
        return "other", "low", None, "Vision classification: no images could be extracted"

    try:
        raw = call_llm_with_images(
            _VISION_CLASSIFICATION_PROMPT, page_images, task="classification", max_tokens=256
        )
        logger.debug("Vision LLM raw response: %s", raw)
        parsed = extract_json(raw)

        doc_type   = parsed.get("doc_type", "other")
        confidence = parsed.get("confidence", "low")
        reasoning  = parsed.get("reasoning", None)

        if doc_type not in _ALLOWED_DOC_TYPES:
            return "other", "low", reasoning, f"Vision LLM returned invalid doc_type: {doc_type}"

        if confidence == "low":
            return "other", "low", reasoning, None

        return doc_type, confidence, reasoning, None

    except json.JSONDecodeError as e:
        return "other", "low", None, f"Vision classification response not valid JSON: {e}"
    except Exception as e:
        return "other", "low", None, f"Vision classification call failed: {e}"


def _classify_by_llm(window: str) -> tuple[str, str, str | None, str | None]:
    """
    Send text window to LLM for classification.

    Returns (doc_type, confidence, reasoning, error).
    Defaults to ('other', 'low', ...) on any failure or low-confidence response.
    """
    prompt = CLASSIFICATION_PROMPT.format(text_window=window)

    try:
        response = call_llm(prompt, task="classification", max_tokens=256)
        logger.debug("LLM raw response: %s", response)
        parsed = extract_json(response)

        doc_type   = parsed.get("doc_type", "other")
        confidence = parsed.get("confidence", "low")
        reasoning  = parsed.get("reasoning", None)

        if doc_type not in _ALLOWED_DOC_TYPES:
            return "other", "low", reasoning, f"LLM returned invalid doc_type: {doc_type}"

        if confidence == "low":
            return "other", "low", reasoning, None

        return doc_type, confidence, reasoning, None

    except json.JSONDecodeError as e:
        return "other", "low", None, f"LLM response not valid JSON: {str(e)}"
    except Exception as e:
        return "other", "low", None, f"LLM classification call failed: {str(e)}"


# ---------------------------------------------------------------------------
# Public classification interface
# ---------------------------------------------------------------------------

def classify_document(parse_result: dict) -> dict:
    """
    Classify a single parsed document into one of six doc type categories.

    Input: parse_result dict from pdf_parser.parse_pdf(), containing at minimum:
        text        (str)   — full extracted text with PAGE BREAK separators
        filename    (str)   — basename of the source file
        is_scanned  (bool)  — True if the document is image-only
        page_count  (int)   — total page count

    Returns a classification result dict with keys:
        filename             (str)        — passed through from parse_result
        doc_type             (str)        — one of the six allowed enum values
        confidence           (str)        — 'high' / 'medium' / 'low'
        classification_method (str)       — 'rule_based' or 'llm'
        reasoning            (str | None) — LLM reasoning string; None for rule_based
        classification_error (str | None) — error message if classification failed
    """
    filename = parse_result.get("filename", "unknown")
    text     = parse_result.get("text", "")

    if parse_result.get("is_scanned"):
        filepath = parse_result.get("filepath", "")
        doc_type, confidence, reasoning, error = _classify_by_vision(filepath)
        if error:
            logger.warning("%s → %s (%s, vision) — %s", filename, doc_type, confidence, error)
        else:
            logger.info("%s → %s (%s, vision)", filename, doc_type, confidence)
        return {
            "filename":              filename,
            "doc_type":              doc_type,
            "confidence":            confidence,
            "classification_method": "vision",
            "reasoning":             reasoning,
            "classification_error":  error,
        }

    if not text.strip():
        error = "No extractable text; classification skipped"
        logger.warning("%s → other (low, rule_based) — no extractable text", filename)
        return {
            "filename":              filename,
            "doc_type":              "other",
            "confidence":            "low",
            "classification_method": "rule_based",
            "reasoning":             None,
            "classification_error":  error,
        }

    window = _get_classification_window(text)

    doc_type, confidence = _classify_by_rules(window)

    if doc_type is not None:
        logger.info("%s → %s (%s, rule_based)", filename, doc_type, confidence)
        return {
            "filename":              filename,
            "doc_type":              doc_type,
            "confidence":            confidence,
            "classification_method": "rule_based",
            "reasoning":             None,
            "classification_error":  None,
        }

    doc_type, confidence, reasoning, error = _classify_by_llm(window)

    if doc_type == "other" or confidence == "low":
        logger.warning("%s → %s (%s, llm) — LLM fallback", filename, doc_type, confidence)
    else:
        logger.info("%s → %s (%s, llm)", filename, doc_type, confidence)

    if error:
        logger.warning("%s — classification error: %s", filename, error)

    return {
        "filename":              filename,
        "doc_type":              doc_type,
        "confidence":            confidence,
        "classification_method": "llm",
        "reasoning":             reasoning,
        "classification_error":  error,
    }


def classify_directory(parse_results: list[dict]) -> list[dict]:
    """
    Classify a list of parse result dicts.

    Scanned documents (is_scanned=True) are assigned other/low without invoking
    rules or LLM. Writes classification_results.csv and classification_summary.txt
    to the outputs/ directory after processing.
    """
    results = [classify_document(pr) for pr in parse_results]

    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    csv_path     = output_dir / "classification_results.csv"
    summary_path = output_dir / "classification_summary.txt"

    write_classification_results(results, str(csv_path))
    print_classification_summary(results, str(summary_path))

    return results


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_classification_results(results: list[dict], output_path: str) -> None:
    """Write classification results to a CSV tracking file. Overwrites existing file."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "filename", "doc_type", "confidence",
        "classification_method", "reasoning", "classification_error",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "filename":              r.get("filename", ""),
                "doc_type":              r.get("doc_type", ""),
                "confidence":            r.get("confidence", ""),
                "classification_method": r.get("classification_method", ""),
                "reasoning":             r.get("reasoning") or "",
                "classification_error":  r.get("classification_error") or "",
            })
    logger.info("Classification results written to %s", output_path)


def print_classification_summary(results: list[dict], output_path: str) -> None:
    """Print and write a classification run summary."""
    total = len(results)
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc_type_counts = {}
    method_counts   = {"rule_based": 0, "llm": 0, "vision": 0}
    conf_counts     = {"high": 0, "medium": 0, "low": 0}
    low_conf_docs   = []
    error_docs      = []

    for r in results:
        dt   = r.get("doc_type", "other")
        meth = r.get("classification_method", "rule_based")
        conf = r.get("confidence", "low")
        err  = r.get("classification_error")

        doc_type_counts[dt] = doc_type_counts.get(dt, 0) + 1
        method_counts[meth] = method_counts.get(meth, 0) + 1
        conf_counts[conf]   = conf_counts.get(conf, 0) + 1

        if conf == "low":
            tag = " [scanned]" if err and "scanned" in err else ""
            low_conf_docs.append(f"  {r.get('filename', '')}   — {dt} (low, {meth}){tag}")

        if err:
            error_docs.append(f"  {r.get('filename', '')} — {err}")

    doc_type_order = [
        "fully_executed_agreement", "renewal_letter", "modification_amendment",
        "award_letter", "vendor_disclosure_statement", "other",
    ]

    lines = [
        "=== Document Classification Run Summary ===",
        f"Run timestamp:       {now}",
        f"Documents processed: {total}",
        "",
        "--- Doc Type Distribution ---",
    ]
    for dt in doc_type_order:
        count = doc_type_counts.get(dt, 0)
        pct   = (count / total * 100) if total else 0
        lines.append(f"  {dt:<30} {count:3d}  ({pct:.1f}%)")

    rule_count   = method_counts.get("rule_based", 0)
    llm_count    = method_counts.get("llm", 0)
    vision_count = method_counts.get("vision", 0)
    lines += [
        "",
        "--- Classification Method ---",
        f"  Rule-based:   {rule_count:3d}  ({rule_count / total * 100:.1f}%)"   if total else "  Rule-based:     0",
        f"  LLM fallback: {llm_count:3d}  ({llm_count / total * 100:.1f}%)"     if total else "  LLM fallback:   0",
        f"  Vision:       {vision_count:3d}  ({vision_count / total * 100:.1f}%)" if total else "  Vision:         0",
        "",
        "--- Confidence Distribution ---",
        f"  High:   {conf_counts.get('high', 0):3d}  ({conf_counts.get('high', 0) / total * 100:.1f}%)"   if total else "  High:     0",
        f"  Medium: {conf_counts.get('medium', 0):3d}  ({conf_counts.get('medium', 0) / total * 100:.1f}%)" if total else "  Medium:   0",
        f"  Low:    {conf_counts.get('low', 0):3d}  ({conf_counts.get('low', 0) / total * 100:.1f}%)"    if total else "  Low:      0",
    ]

    lines += ["", "--- Low-Confidence Documents (review recommended) ---"]
    if low_conf_docs:
        lines.extend(low_conf_docs)
    else:
        lines.append("  (none)")

    lines += ["", "--- Classification Errors ---"]
    if error_docs:
        lines.extend(error_docs)
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Tracking file written to: {output_path.replace('classification_summary.txt', 'classification_results.csv')}")

    summary = "\n".join(lines)

    logger.info("\n%s", summary)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(summary + "\n")
