"""
LLM-based structured field extraction — Task 2.3.

Consumes outputs of pdf_parser (Task 2.1) and document_classifier (Task 2.2).
Returns one result dict per document, DB-ready on success or a failure record on skip/error.
The DB writer (Task 2.5) owns the SQLite INSERT step — this module only produces dicts.

Public API:
    extract_document(parse_result, classification_result) -> dict
    extract_batch(parse_results, classification_results)  -> (successes, failures)
    test_single_document(pdf_path)                        -> dict  (CLI entry point)

CLI usage:
    python -m src.pipeline.extractor --test path/to/document.pdf
"""

import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from src.llm_client import LLMCallError, call_llm, call_llm_with_images, extract_json, get_token_totals
from src.pipeline.models import ContractRecord
from src.pipeline.pdf_parser import extract_page_images
from src.pipeline.prompts import build_extraction_prompt

logger = logging.getLogger("extractor")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [extractor] [%(levelname)s] %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

OUTPUTS_DIR = Path("outputs")

# ---------------------------------------------------------------------------
# Skip gate constants
# ---------------------------------------------------------------------------

SKIP_REASONS = {
    "low_confidence_classification": "Classifier confidence is low; extraction skipped to avoid wrong field block",
}



# ---------------------------------------------------------------------------
# Failure record helper
# ---------------------------------------------------------------------------


def _make_failure_record(
    filename: str,
    doc_type: str,
    status: str,
    reason: str,
) -> dict:
    return {
        "source_filename":   filename,
        "doc_type":          doc_type,
        "extraction_status": status,
        "failure_reason":    reason,
    }


# ---------------------------------------------------------------------------
# Primary extraction function
# ---------------------------------------------------------------------------

def extract_document(
    parse_result: dict,
    classification_result: dict,
) -> dict:
    """
    Extract structured fields from a single parsed and classified document.

    Returns a dict that always contains 'source_filename' and 'extraction_status'.
    On success, all schema fields are present (some may be None). On skip/failure,
    only identity + status + failure_reason are present.

    Extraction status values:
        'success' — LLM returned valid JSON; Pydantic validation passed; row is DB-ready
        'skipped' — document is scanned or classification confidence is low
        'failed'  — LLM call failed, JSON parse failed, or schema validation failed
    """
    filename = parse_result.get("filename", "unknown")
    doc_type = classification_result.get("doc_type", "other")
    is_scanned = parse_result.get("is_scanned", False)

    # --- Skip gate (non-scanned low-confidence only) ---
    # Scanned docs always come back as confidence="low" from the classifier
    # (no text to classify from), so the scanned check must come first and
    # route to vision rather than skip.
    if not is_scanned and classification_result.get("confidence") == "low":
        logger.info(f"Skipping low-confidence document: {filename}")
        return _make_failure_record(
            filename, doc_type, "skipped", SKIP_REASONS["low_confidence_classification"]
        )

    # --- Call LLM (text or vision path) ---
    if is_scanned:
        logger.info(f"Scanned document — using vision path: {filename}")
        filepath = parse_result.get("filepath", "")
        if not filepath:
            return _make_failure_record(filename, doc_type, "failed",
                "Scanned document has no filepath in parse result; cannot render images")
        page_images = extract_page_images(filepath, dpi=150, max_pages=20)
        if not page_images:
            return _make_failure_record(filename, doc_type, "failed",
                "Scanned document: extract_page_images() returned no images")

        prompt = build_extraction_prompt(doc_type, "")
        try:
            raw_response = call_llm_with_images(prompt, page_images, task="extraction")
        except LLMCallError as e:
            logger.warning(f"Vision LLM call failed for {filename}: {e}")
            return _make_failure_record(filename, doc_type, "failed", f"Vision LLM call failed: {e}")
    else:
        document_text = parse_result.get("text", "")
        prompt = build_extraction_prompt(doc_type, document_text)
        try:
            raw_response = call_llm(prompt, task="extraction")
        except LLMCallError as e:
            logger.warning(f"LLM call failed for {filename}: {e}")
            return _make_failure_record(filename, doc_type, "failed", f"LLM call failed: {e}")

    # --- Parse JSON ---
    try:
        extracted = extract_json(raw_response)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse failed for {filename}: {e}")
        logger.warning(f"Raw LLM response was:\n{raw_response}")
        record = _make_failure_record(
            filename, doc_type, "failed", f"LLM response not valid JSON: {e}"
        )
        record["raw_llm_response"] = raw_response
        return record

    # --- Validate via Pydantic ---
    # ContractRecord enforces non-nullable fields, enum values, date formats, and type coercions.
    # to_db_row() handles bool→0/1, date→string, enum→.value for the DB writer.
    try:
        run_ts = datetime.now(timezone.utc).isoformat()
        record = ContractRecord(
            source_filename=filename,
            pipeline_run_timestamp=run_ts,
            extraction_method="vision" if is_scanned else "text",
            **extracted,
        )
    except ValidationError as e:
        first = e.errors()[0]
        reason = f"Schema validation failed: {e.error_count()} error(s) — {first['loc']} {first['msg']}"
        logger.warning(f"Validation failed for {filename}: {reason}")
        return _make_failure_record(filename, doc_type, "failed", reason)

    result = record.to_db_row()
    result["extraction_status"] = "success"
    return result


# ---------------------------------------------------------------------------
# Batch function
# ---------------------------------------------------------------------------

def extract_batch(
    parse_results: list[dict],
    classification_results: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Run extraction over a list of parsed and classified documents.

    Matches parse and classification results by filename (not list order).
    Returns (successes, failures) — two lists of result dicts.
    Writes three tracking files to outputs/ after completion.
    """
    # Build filename → classification lookup
    classification_by_filename: dict[str, dict] = {
        r["filename"]: r for r in classification_results
    }

    successes: list[dict] = []
    failures:  list[dict] = []
    vision_count = 0
    text_count   = 0

    for parse_result in parse_results:
        filename = parse_result.get("filename", "unknown")
        classification_result = classification_by_filename.get(filename, {
            "filename": filename,
            "doc_type": "other",
            "confidence": "low",
            "classification_method": "unknown",
            "reasoning": None,
            "classification_error": f"No classification result found for {filename}",
        })

        result = extract_document(parse_result, classification_result)

        if result.get("extraction_status") == "success":
            successes.append(result)
            if result.get("extraction_method") == "vision":
                vision_count += 1
            else:
                text_count += 1
        else:
            failures.append(result)

    _write_batch_outputs(
        successes, failures,
        total=len(parse_results),
        vision_count=vision_count,
        text_count=text_count,
    )
    return successes, failures


# ---------------------------------------------------------------------------
# Batch output writers
# ---------------------------------------------------------------------------

def _write_batch_outputs(
    successes: list[dict],
    failures: list[dict],
    total: int,
    vision_count: int = 0,
    text_count: int = 0,
) -> None:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    _write_results_csv(successes)
    _write_failures_csv(failures)
    _write_token_summary(total, len(successes), len(failures), vision_count, text_count)


def _write_results_csv(successes: list[dict]) -> None:
    path = OUTPUTS_DIR / "extraction_results.csv"
    columns = [
        "source_filename", "contract_number", "doc_type",
        "vendor_name", "extraction_confidence", "extraction_notes", "extraction_method",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(successes)
    logger.info(f"Wrote {len(successes)} rows to {path}")


def _write_failures_csv(failures: list[dict]) -> None:
    path = OUTPUTS_DIR / "extraction_failures.csv"
    columns = ["source_filename", "doc_type", "extraction_status", "failure_reason"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(failures)
    logger.info(f"Wrote {len(failures)} rows to {path}")


def _write_token_summary(
    total: int,
    successful: int,
    skipped_or_failed: int,
    vision_count: int = 0,
    text_count: int = 0,
) -> None:
    path = OUTPUTS_DIR / "extraction_token_summary.txt"
    totals = get_token_totals()
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    classification = totals["classification"]
    extraction     = totals["extraction"]
    total_input    = sum(t["input"]  for t in totals.values())
    total_output   = sum(t["output"] for t in totals.values())

    lines = [
        "=== Extraction Run — Token Usage Summary ===",
        f"Run timestamp:       {run_ts}",
        f"Documents processed: {total}",
        f"  Successful:        {successful}",
        f"  Skipped/Failed:    {skipped_or_failed}",
        "",
        "--- Extraction Method ---",
        f"  Text-based:        {text_count}",
        f"  Vision-based:      {vision_count}  (scanned documents processed via image API)",
        "",
        "--- Token Usage by Task ---",
        "  Classification (LLM fallback only):",
        f"    Input tokens:    {classification['input']:,}",
        f"    Output tokens:   {classification['output']:,}",
        "",
        "  Extraction:",
        f"    Input tokens:    {extraction['input']:,}",
        f"    Output tokens:   {extraction['output']:,}",
        "",
        f"  Total input tokens:  {total_input:,}",
        f"  Total output tokens: {total_output:,}",
        "",
        "--- Estimated Cost (indicative) ---",
        "  Model (extraction):     claude-sonnet-4-5",
        "  Model (classification): claude-haiku-4-5-20251001",
        "  Note: Check current Anthropic pricing at https://www.anthropic.com/pricing",
        "",
        "Tracking files written to:",
        "  outputs/extraction_results.csv",
        "  outputs/extraction_failures.csv",
        "  outputs/extraction_token_summary.txt",
    ]

    text = "\n".join(lines)
    path.write_text(text)
    logger.info(f"Token summary written to {path}")
    logger.info(text)


# ---------------------------------------------------------------------------
# Single-document test mode
# ---------------------------------------------------------------------------

def test_single_document(pdf_path: str) -> dict:
    """
    Parse, classify, and extract a single document end-to-end.
    Prints results to stdout for manual inspection.
    No database interaction. No tracking files written.

    CLI usage:
        python -m src.pipeline.extractor --test path/to/document.pdf
    """
    from src.pipeline.classifier import classify_document
    from src.pipeline.pdf_parser import parse_pdf

    print(f"\n{'='*60}")
    print(f"TEST RUN: {pdf_path}")
    print(f"{'='*60}\n")

    parse_result = parse_pdf(pdf_path)
    print(
        f"[Parser]     pages={parse_result['page_count']}  "
        f"chars={len(parse_result['text'])}  "
        f"scanned={parse_result['is_scanned']}"
    )

    classification_result = classify_document(parse_result)
    print(
        f"[Classifier] doc_type={classification_result['doc_type']}  "
        f"confidence={classification_result['confidence']}  "
        f"method={classification_result['classification_method']}"
    )

    result = extract_document(parse_result, classification_result)
    print(f"\n[Extractor]  status={result['extraction_status']}")
    print("\n--- Extraction Result ---")
    print(json.dumps(result, indent=2, default=str))

    totals = get_token_totals()
    print("\n--- Token Usage ---")
    print(json.dumps(totals, indent=2))

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extractor test mode")
    parser.add_argument("--test", metavar="PDF_PATH", help="Run single-document test on this PDF")
    args = parser.parse_args()

    if args.test:
        test_single_document(args.test)
    else:
        parser.print_help()
        sys.exit(1)
