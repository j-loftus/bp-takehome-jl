"""
Pure scoring functions for the evaluation harness — no LLM calls, no I/O.

Classifies each (gold, pred) field cell into one of six buckets (§4 of
docs/evaluation.md), applies per-field match rules (§4.1), and aggregates
into the headline extraction metrics (§4.2-4.4) and classification metrics.
Shared, unmodified, by both the offline eval (run_eval.py) and production
monitoring (monitoring.py) — "same metric code offline and online".
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any

from rapidfuzz import fuzz

# Sentinel for "not yet labeled" — distinct from a real null gold value.
# See docs/evaluation.md §3.1 "Sentinel discipline".
UNLABELED = "__UNLABELED__"

# Four-bucket-plus scoring outcomes (§4).
CORRECT = "correct"
WRONG_VALUE = "wrong_value"
MISSED = "missed"
HALLUCINATED = "hallucinated"
TRUE_NEGATIVE = "true_negative"
SKIPPED = "skipped"
REVIEW = "review"  # renewal_options only — normalization failed on one side

# Field-to-group map (extraction_schema.md groups A-E).
FIELD_GROUPS: dict[str, str] = {
    "contract_number": "A",
    "doc_type": "A",
    "vendor_name": "A",
    "doc_date": "A",
    "county_department": "A",
    "total_contract_value": "B",
    "price_escalator_terms": "B",
    "modification_financial_delta": "B",
    "contract_start_date": "C",
    "contract_end_date": "C",
    "renewal_options": "C",
    "auto_renewal_flag": "C",
    "termination_notice_days": "C",
    "service_category": "D",
    "procurement_vehicle": "D",
    "insurance_requirements_flag": "D",
    "parent_contract_number": "E",
}

# Inferred fields — reported on a separate accuracy line, never folded into
# the headline extracted-field accuracy (§4.3).
INFERRED_FIELDS = {"service_category", "auto_renewal_flag", "price_escalator_terms"}

ENUM_FIELDS = {"doc_type", "service_category", "procurement_vehicle", "price_escalator_terms"}
BOOL_FIELDS = {"auto_renewal_flag", "insurance_requirements_flag"}
INT_FIELDS = {"termination_notice_days"}
MONEY_FIELDS = {"total_contract_value", "modification_financial_delta"}
DATE_FIELDS = {"doc_date", "contract_start_date", "contract_end_date"}
EXACT_STRING_FIELDS = {"contract_number", "parent_contract_number"}

_LEGAL_SUFFIXES = re.compile(r"\b(inc|llc|corp|co|ltd)\b\.?", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_vendor_name(name: str) -> str:
    """Lowercase, strip legal suffixes/punctuation, collapse whitespace."""
    s = name.lower()
    s = _LEGAL_SUFFIXES.sub("", s)
    s = _PUNCT.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def _vendor_name_match(gold: str, pred: str) -> bool:
    g, p = normalize_vendor_name(str(gold)), normalize_vendor_name(str(pred))
    return fuzz.token_sort_ratio(g, p) >= 90.0


def _county_department_match(gold: str, pred: str) -> bool:
    """Free-text department names tolerate minor phrasing/abbreviation
    differences (e.g. 'Dept.' vs 'Department') that an exact match would
    wrongly flag as wrong_value."""
    g = _WHITESPACE.sub(" ", _PUNCT.sub(" ", gold.lower())).strip()
    p = _WHITESPACE.sub(" ", _PUNCT.sub(" ", pred.lower())).strip()
    return fuzz.token_sort_ratio(g, p) >= 85.0


_RENEWAL_PATTERN = re.compile(
    r"(\d+)\s*(?:x|×)?\s*(\d+)?\s*[-\s]?\s*(year|yr|month)s?", re.IGNORECASE
)


def normalize_renewal_options(text: str) -> tuple[int, str] | None:
    """Normalize free-text renewal terms to (count, unit), e.g. '3 x 1-year' -> (3, '1-year').

    Best-effort heuristic regex over free text. Returns None when the text
    doesn't match a recognizable count/unit pattern — the caller treats that
    as a REVIEW case for the judge to resolve, not a hard failure (§4.1).
    """
    m = _RENEWAL_PATTERN.search(text.lower())
    if not m:
        return None
    count = int(m.group(1))
    unit_count, unit = m.group(2), m.group(3)
    unit_label = f"{unit_count}-{unit}" if unit_count else unit
    return (count, unit_label)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def match_field(field: str, gold: Any, pred: Any) -> str:
    """Apply the field's match rule to two non-null values.

    Returns "match", "no_match", or "review" (renewal_options ambiguity only).
    """
    if field in ENUM_FIELDS:
        return "match" if str(gold).strip().lower() == str(pred).strip().lower() else "no_match"

    if field in BOOL_FIELDS:
        return "match" if bool(int(gold)) == bool(int(pred)) else "no_match"

    if field in INT_FIELDS:
        return "match" if int(gold) == int(pred) else "no_match"

    if field in MONEY_FIELDS:
        g, p = float(gold), float(pred)
        tolerance = max(1.0, 0.005 * abs(g))
        return "match" if abs(g - p) <= tolerance else "no_match"

    if field in DATE_FIELDS:
        g, p = _parse_date(gold), _parse_date(pred)
        if g is None or p is None:
            return "no_match"
        return "match" if g == p else "no_match"

    if field in EXACT_STRING_FIELDS:
        return "match" if str(gold).strip() == str(pred).strip() else "no_match"

    if field == "vendor_name":
        return "match" if _vendor_name_match(gold, pred) else "no_match"

    if field == "county_department":
        return "match" if _county_department_match(str(gold), str(pred)) else "no_match"

    if field == "renewal_options":
        g_norm, p_norm = normalize_renewal_options(str(gold)), normalize_renewal_options(str(pred))
        if g_norm is None or p_norm is None:
            return "review"
        return "match" if g_norm == p_norm else "no_match"

    raise ValueError(f"No match rule defined for field: {field}")


def classify_cell(field: str, gold: Any, pred: Any) -> str:
    """Classify a single (gold, pred) cell into one of the §4 buckets."""
    if gold == UNLABELED:
        return SKIPPED

    gold_null = gold is None
    pred_null = pred is None

    if gold_null and pred_null:
        return TRUE_NEGATIVE
    if gold_null and not pred_null:
        return HALLUCINATED
    if not gold_null and pred_null:
        return MISSED

    outcome = match_field(field, gold, pred)
    if outcome == "match":
        return CORRECT
    if outcome == "review":
        return REVIEW
    return WRONG_VALUE


def score_document_fields(gold_fields: dict[str, Any], pred_fields: dict[str, Any]) -> dict[str, str]:
    """Score every gold-labeled field for one document. Returns {field: bucket}."""
    return {
        field: classify_cell(field, gold_value, pred_fields.get(field))
        for field, gold_value in gold_fields.items()
        if field in FIELD_GROUPS  # ignore non-schema keys (e.g. doc_type handled separately)
    }


def _accuracy_and_halluc(buckets: list[str]) -> tuple[float | None, float | None]:
    counts = {b: buckets.count(b) for b in (CORRECT, WRONG_VALUE, MISSED, HALLUCINATED, TRUE_NEGATIVE)}
    populated = counts[CORRECT] + counts[WRONG_VALUE] + counts[MISSED]
    field_accuracy = counts[CORRECT] / populated if populated else None
    null_denom = counts[HALLUCINATED] + counts[TRUE_NEGATIVE]
    hallucination_rate = counts[HALLUCINATED] / null_denom if null_denom else None
    return field_accuracy, hallucination_rate


def aggregate_extraction(results: list[dict]) -> dict:
    """Aggregate per-document field-bucket results into headline + diagnostic metrics.

    Each entry in `results` is {"source_filename": str, "doc_type": str,
    "field_buckets": {field: bucket}} as produced by score_document_fields,
    with doc_type attached by the caller.
    """
    all_extracted: list[str] = []
    all_inferred: list[str] = []
    by_group: dict[str, list[str]] = defaultdict(list)
    by_doc_type: dict[str, list[str]] = defaultdict(list)
    null_counts_by_doc_type: dict[str, dict[str, int]] = defaultdict(lambda: {"null": 0, "total": 0})
    warnings: list[str] = []
    four_bucket_breakdown: dict[str, int] = defaultdict(int)
    review_cases: list[dict] = []

    for doc in results:
        doc_type = doc["doc_type"]
        for field, bucket in doc["field_buckets"].items():
            if bucket == SKIPPED:
                warnings.append(
                    f"ground truth incomplete: {doc['source_filename']}.{field} is __UNLABELED__"
                )
                continue
            if bucket == REVIEW:
                review_cases.append({"source_filename": doc["source_filename"], "field": field})
                continue

            four_bucket_breakdown[bucket] += 1
            by_doc_type[doc_type].append(bucket)
            group = FIELD_GROUPS.get(field)
            if group:
                by_group[group].append(bucket)

            if field in INFERRED_FIELDS:
                all_inferred.append(bucket)
            else:
                all_extracted.append(bucket)

            null_counts_by_doc_type[doc_type]["total"] += 1
            if bucket in (MISSED, TRUE_NEGATIVE):
                null_counts_by_doc_type[doc_type]["null"] += 1

    overall_accuracy, overall_halluc = _accuracy_and_halluc(all_extracted)
    inferred_accuracy, _ = _accuracy_and_halluc(all_inferred)

    null_rate_by_doc_type = {
        dt: (counts["null"] / counts["total"] if counts["total"] else None)
        for dt, counts in null_counts_by_doc_type.items()
    }

    return {
        "field_accuracy": overall_accuracy,
        "hallucination_rate": overall_halluc,
        "inferred_field_accuracy": inferred_accuracy,
        "four_bucket_breakdown": dict(four_bucket_breakdown),
        "by_group": {g: _accuracy_and_halluc(b) for g, b in by_group.items()},
        "by_doc_type": {dt: _accuracy_and_halluc(b) for dt, b in by_doc_type.items()},
        "null_rate_by_doc_type": null_rate_by_doc_type,
        "review_cases": review_cases,
        "warnings": warnings,
    }


def aggregate_classification(labels: list[dict]) -> dict:
    """Compute classification accuracy + 6x6 confusion matrix.

    Each entry in `labels` is {"source_filename": str, "gold_doc_type": str,
    "pred_doc_type": str}. Entries with gold_doc_type == UNLABELED are skipped.
    """
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    correct = 0
    total = 0
    warnings: list[str] = []

    for entry in labels:
        gold = entry["gold_doc_type"]
        if gold == UNLABELED:
            warnings.append(f"ground truth incomplete: {entry['source_filename']} classification")
            continue
        pred = entry["pred_doc_type"]
        confusion[gold][pred] += 1
        total += 1
        if gold == pred:
            correct += 1

    accuracy = correct / total if total else None
    return {
        "accuracy": accuracy,
        "confusion_matrix": {g: dict(p) for g, p in confusion.items()},
        "n_labeled": total,
        "warnings": warnings,
    }
