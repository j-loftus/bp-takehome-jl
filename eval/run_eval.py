"""
Evaluation orchestrator — CLI entry point (docs/evaluation.md §7, §9).

Order: classification scoring -> extraction scoring -> chat cases -> sampled
judge (defaulted to the ground-truth-overlap docs, so the judge run IS the
calibration evidence) -> calibration -> report. Writes the four outputs/
artifacts: eval_report.md, eval_results.json, eval_judge_raw.json,
eval_token_summary.txt.

Usage:
    python -m eval.run_eval
    python -m eval.run_eval --judge-sample 8
    python -m eval.run_eval --judge-extra-unlabeled 3
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from src.chat_router import answer_semantic, answer_structured, classify_intent
from src.llm_client import get_token_totals, reset_token_counters
from src.pipeline.classifier import classify_document

from eval import scoring
from eval.judge import (
    compute_chat_calibration,
    compute_extraction_calibration,
    run_chat_judge,
    run_extraction_judge,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
OUTPUTS_DIR = REPO_ROOT / "outputs"
PDF_DIR = REPO_ROOT / "contracts"

GROUND_TRUTH_EXTRACTION_PATH = REPO_ROOT / "eval" / "ground_truth_extraction.json"
GROUND_TRUTH_CLASSIFICATION_PATH = REPO_ROOT / "eval" / "ground_truth_classification.json"
CHAT_CASES_PATH = REPO_ROOT / "eval" / "chat_cases.json"

UNLABELED = scoring.UNLABELED


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def _load_parse_cache() -> dict[str, dict]:
    entries = json.loads((DATA_DIR / "parse_cache.json").read_text())
    return {e["filename"]: e for e in entries}


def _load_contracts_by_filename() -> dict[str, dict]:
    con = sqlite3.connect(f"file:{DATA_DIR / 'contracts.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM contracts").fetchall()
    finally:
        con.close()
    by_filename: dict[str, dict] = {}
    for row in rows:
        by_filename[row["source_filename"]] = dict(row)
    return by_filename


# ---------------------------------------------------------------------------
# Classification scoring (§4.4)
# ---------------------------------------------------------------------------

def _pred_doc_type(filename: str, contracts: dict, parse_cache: dict) -> str | None:
    row = contracts.get(filename)
    if row is not None:
        return row["doc_type"]
    # Fallback for docs absent from contracts.db (extraction failed) —
    # classifies independently of extraction success (§4.4).
    cache_entry = parse_cache.get(filename)
    if cache_entry is None:
        return None
    parse_result = {
        "text": cache_entry.get("text", ""),
        "filename": filename,
        "is_scanned": cache_entry.get("is_scanned", False),
        "page_count": cache_entry.get("page_count", 0),
    }
    return classify_document(parse_result)["doc_type"]


def score_classification(contracts: dict, parse_cache: dict) -> dict:
    gt = _load_json(GROUND_TRUTH_CLASSIFICATION_PATH)
    labels = [
        {
            "source_filename": e["source_filename"],
            "gold_doc_type": e["gold_doc_type"],
            "pred_doc_type": _pred_doc_type(e["source_filename"], contracts, parse_cache),
        }
        for e in gt
    ]
    return scoring.aggregate_classification(labels), labels


# ---------------------------------------------------------------------------
# Extraction scoring (§4.1-4.3)
# ---------------------------------------------------------------------------

def score_extraction(contracts: dict) -> tuple[dict, list[dict], dict]:
    gt = _load_json(GROUND_TRUTH_EXTRACTION_PATH)
    per_doc_results = []
    field_bucket_lookup: dict[tuple[str, str], str] = {}

    for entry in gt:
        filename = entry["source_filename"]
        pred_row = contracts.get(filename, {})
        field_buckets = scoring.score_document_fields(entry["fields"], pred_row)
        for field, bucket in field_buckets.items():
            field_bucket_lookup[(filename, field)] = bucket

        doc_type = entry["gold_doc_type"] if entry["gold_doc_type"] != UNLABELED else pred_row.get("doc_type", "unknown")
        per_doc_results.append({
            "source_filename": filename,
            "doc_type": doc_type,
            "field_buckets": field_buckets,
        })

    return scoring.aggregate_extraction(per_doc_results), per_doc_results, field_bucket_lookup


# ---------------------------------------------------------------------------
# Chat evaluation (§6)
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"[\$,]")


def _check_structured_answer(dataframe, expected_answer: str) -> bool:
    """Exact for scalars; set/order-insensitive for small result sets (§6.2)."""
    cells = [str(v) for v in dataframe.values.flatten()] if not dataframe.empty else []
    expected_norm = _MONEY_RE.sub("", expected_answer).strip().lower()

    for cell in cells:
        cell_norm = _MONEY_RE.sub("", cell).strip().lower()
        if cell_norm == expected_norm:
            return True
        try:
            if abs(float(cell_norm) - float(expected_norm)) <= max(1.0, 0.005 * abs(float(expected_norm))):
                return True
        except ValueError:
            continue
    return False


def _retrieval_hit(sources: list[dict], target_filename: str | None, target_contract_number: str | None) -> bool:
    if not target_filename and not target_contract_number:
        return False
    for s in sources:
        if target_filename and s.get("source_filename") == target_filename:
            return True
        if target_contract_number and str(s.get("contract_number")) == str(target_contract_number):
            return True
    return False


def run_chat_cases() -> tuple[list[dict], list[dict]]:
    """Returns (per_case_results, judge_input_cases) — judge_input_cases carry
    the retrieved_chunks + generated answer for the chat judge to score."""
    cases = _load_json(CHAT_CASES_PATH)
    results = []
    judge_inputs = []

    for case in cases:
        router_pred = classify_intent(case["question"])
        router_match = router_pred == case["expected_intent"]

        if case["eval_mode"] == "deterministic":
            structured = answer_structured(case["question"])
            correct = (
                structured["error"] is None
                and case.get("expected_answer") is not None
                and _check_structured_answer(structured["dataframe"], case["expected_answer"])
            )
            results.append({
                "id": case["id"],
                "question": case["question"],
                "router_pred": router_pred,
                "router_match": router_match,
                "eval_mode": "deterministic",
                "sql": structured["sql"],
                "correct": correct,
                "error": structured["error"],
            })
        else:
            semantic = answer_semantic(case["question"])
            retrieval_hit = _retrieval_hit(
                semantic["sources"], case.get("target_filename"), case.get("target_contract_number")
            )
            results.append({
                "id": case["id"],
                "question": case["question"],
                "router_pred": router_pred,
                "router_match": router_match,
                "eval_mode": "judge",
                "answer": semantic["answer"],
                "sources": semantic["sources"],
                "retrieval_hit": retrieval_hit,
                "low_confidence": semantic["low_confidence"],
                "error": semantic["error"],
            })
            judge_inputs.append({
                "id": case["id"],
                "question": case["question"],
                "retrieved_chunks": semantic.get("retrieved_chunks", []),
                "answer": semantic["answer"],
            })

    return results, judge_inputs


# ---------------------------------------------------------------------------
# Judge sample selection (§7)
# ---------------------------------------------------------------------------

def _select_judge_records(contracts: dict, parse_cache: dict, extra_unlabeled: int) -> list[dict]:
    gt = _load_json(GROUND_TRUTH_EXTRACTION_PATH)
    labeled_filenames = {e["source_filename"] for e in gt}

    records = []
    for filename in labeled_filenames:
        row = contracts.get(filename)
        if row is None:
            continue
        cache_entry = parse_cache.get(filename, {})
        records.append({
            "source_filename": filename,
            "doc_type": row["doc_type"],
            "extracted_fields": {k: v for k, v in row.items() if k in scoring.FIELD_GROUPS},
            "is_scanned": cache_entry.get("is_scanned", False),
            "source_text": cache_entry.get("text", ""),
            "filepath": str(PDF_DIR / filename),
        })

    if extra_unlabeled:
        unlabeled_candidates = [f for f in contracts if f not in labeled_filenames][:extra_unlabeled]
        for filename in unlabeled_candidates:
            row = contracts[filename]
            cache_entry = parse_cache.get(filename, {})
            records.append({
                "source_filename": filename,
                "doc_type": row["doc_type"],
                "extracted_fields": {k: v for k, v in row.items() if k in scoring.FIELD_GROUPS},
                "is_scanned": cache_entry.get("is_scanned", False),
                "source_text": cache_entry.get("text", ""),
                "filepath": str(PDF_DIR / filename),
            })

    return records


# ---------------------------------------------------------------------------
# Report (§9)
# ---------------------------------------------------------------------------

def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def build_report(
    classification_metrics: dict,
    extraction_metrics: dict,
    chat_results: list[dict],
    chat_judge_results: list[dict],
    extraction_calibration: dict,
    chat_calibration: list[dict],
    judge_sample_size: int,
) -> str:
    structured_cases = [c for c in chat_results if c["eval_mode"] == "deterministic"]
    semantic_cases = [c for c in chat_results if c["eval_mode"] == "judge"]
    router_matches = sum(1 for c in chat_results if c["router_match"])
    structured_correct = sum(1 for c in structured_cases if c["correct"])
    retrieval_hits = sum(1 for c in semantic_cases if c["retrieval_hit"])
    faithfulness_scores = [r["faithfulness"] for r in chat_judge_results if r.get("faithfulness") is not None]
    relevance_scores = [r["relevance"] for r in chat_judge_results if r.get("relevance") is not None]
    mean_faithfulness = sum(faithfulness_scores) / len(faithfulness_scores) if faithfulness_scores else None
    mean_relevance = sum(relevance_scores) / len(relevance_scores) if relevance_scores else None
    pass_rate = (
        sum(1 for s in faithfulness_scores if s >= 4) / len(faithfulness_scores) if faithfulness_scores else None
    )

    lines = []
    lines.append("# Evaluation Report\n")

    lines.append("## 1. Headline Scorecard\n")
    lines.append(f"- **Classification accuracy:** {_fmt_pct(classification_metrics['accuracy'])} "
                 f"(n={classification_metrics['n_labeled']})")
    lines.append(f"- **Extraction field accuracy:** {_fmt_pct(extraction_metrics['field_accuracy'])}")
    lines.append(f"- **Extraction hallucination rate:** {_fmt_pct(extraction_metrics['hallucination_rate'])}")
    lines.append(f"- **Structured chat correctness:** {structured_correct}/{len(structured_cases)}")
    if mean_faithfulness is not None:
        lines.append(f"- **RAG faithfulness (mean / pass-rate@4):** {mean_faithfulness:.2f} / {_fmt_pct(pass_rate)}")
    else:
        lines.append("- **RAG faithfulness:** n/a")
    lines.append(f"- **RAG relevance (mean):** {mean_relevance:.2f}" if mean_relevance is not None else "- **RAG relevance:** n/a")
    lines.append(f"- **Judge-ground-truth agreement (extraction):** {_fmt_pct(extraction_calibration['agreement'])} "
                 f"(n={extraction_calibration['n_compared']})")
    lines.append("")

    lines.append("## 2. Extraction Detail\n")
    lines.append(f"Four-bucket breakdown: `{extraction_metrics['four_bucket_breakdown']}`\n")
    lines.append(f"Inferred-field accuracy (service_category, auto_renewal_flag, price_escalator_terms): "
                 f"{_fmt_pct(extraction_metrics['inferred_field_accuracy'])}\n")
    lines.append("By field group (accuracy, hallucination rate):")
    for group, (acc, halluc) in extraction_metrics["by_group"].items():
        lines.append(f"- Group {group}: accuracy={_fmt_pct(acc)}, hallucination={_fmt_pct(halluc)}")
    lines.append("\nBy doc_type (accuracy, hallucination rate):")
    for dt, (acc, halluc) in extraction_metrics["by_doc_type"].items():
        lines.append(f"- {dt}: accuracy={_fmt_pct(acc)}, hallucination={_fmt_pct(halluc)}")
    lines.append("\nNull rate by doc_type:")
    for dt, rate in extraction_metrics["null_rate_by_doc_type"].items():
        lines.append(f"- {dt}: {_fmt_pct(rate)}")
    if extraction_metrics["review_cases"]:
        lines.append(f"\nReview cases (renewal_options normalization ambiguity): {extraction_metrics['review_cases']}")
    if extraction_metrics["warnings"]:
        lines.append(f"\nWarnings: {len(extraction_metrics['warnings'])} unlabeled cells skipped.")
    lines.append("")

    lines.append("## 3. Classification Detail\n")
    lines.append("Confusion matrix (gold -> predicted counts):\n")
    lines.append("```json")
    lines.append(json.dumps(classification_metrics["confusion_matrix"], indent=2))
    lines.append("```\n")

    lines.append("## 4. Chat Detail\n")
    lines.append(f"Router accuracy: {router_matches}/{len(chat_results)}")
    if semantic_cases:
        lines.append(f"Retrieval sanity hit rate: {retrieval_hits}/{len(semantic_cases)}")
    lines.append("")
    lines.append("| ID | Mode | Router match | Result |")
    lines.append("|---|---|---|---|")
    for c in chat_results:
        if c["eval_mode"] == "deterministic":
            result = "correct" if c["correct"] else f"incorrect (sql: `{c['sql']}`)"
        else:
            result = f"retrieval_hit={c['retrieval_hit']}"
        lines.append(f"| {c['id']} | {c['eval_mode']} | {c['router_match']} | {result} |")
    lines.append("")

    lines.append("## 5. Judge Calibration\n")
    lines.append(f"Extraction judge-vs-ground-truth agreement: {_fmt_pct(extraction_calibration['agreement'])} "
                 f"over {extraction_calibration['n_compared']} populated fields on the labeled docs.\n")
    if extraction_calibration["disagreements"]:
        lines.append("Disagreements:")
        lines.append("```json")
        lines.append(json.dumps(extraction_calibration["disagreements"], indent=2))
        lines.append("```\n")
    if chat_calibration:
        lines.append("Chat judge scores on semantic cases with a known answer (qualitative comparison):")
        lines.append("```json")
        lines.append(json.dumps(chat_calibration, indent=2))
        lines.append("```\n")

    lines.append("## 6. Proven vs. Assumed\n")
    lines.append(
        f"- **Proven:** classification accuracy and extraction field accuracy/hallucination rate are "
        f"computed directly against {classification_metrics['n_labeled']} human-verified doc_type labels "
        f"and a small hand-labeled extraction set ({extraction_metrics['four_bucket_breakdown']}). The "
        f"judge-ground-truth agreement number above is what licenses leaning on the judge beyond this "
        f"labeled set.\n"
        f"- **Assumed:** the judge was run on a sample of {judge_sample_size} document(s) "
        f"(ground-truth-overlap docs, optionally plus `--judge-extra-unlabeled`), not the full corpus or "
        f"live traffic — the full-corpus / live run is a `--sample N` parameter change, deliberately "
        f"deferred for cost (§11). Ground truth itself is a single-labeler, small-N set; no inter-annotator "
        f"agreement or confidence intervals are computed — numbers here are directional anchors, not "
        f"statistically powered estimates.\n"
        f"- **Substitution:** the 'award_letter with bid tab' extraction criterion was approximated with "
        f"23036_Award_Letter.pdf (populated total_contract_value/procurement_vehicle) — no literal "
        f"bid-tab document exists in the sampled corpus.\n"
        f"- **Known limitation:** scanned documents are not in the RAG index (no extractable text), so no "
        f"semantic chat case targets a scanned document; the extraction judge does cover scanned docs via "
        f"a vision path."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judge-sample", type=int, default=None, help="explicit judge sample size override")
    parser.add_argument("--judge-extra-unlabeled", type=int, default=0, help="extra unlabeled docs to judge")
    args = parser.parse_args()

    OUTPUTS_DIR.mkdir(exist_ok=True)
    reset_token_counters()

    contracts = _load_contracts_by_filename()
    parse_cache = _load_parse_cache()

    print("Scoring classification...")
    classification_metrics, classification_labels = score_classification(contracts, parse_cache)

    print("Scoring extraction...")
    extraction_metrics, extraction_results, field_bucket_lookup = score_extraction(contracts)

    print("Running chat cases...")
    chat_results, judge_chat_inputs = run_chat_cases()

    print("Running sampled judge...")
    judge_records = _select_judge_records(contracts, parse_cache, args.judge_extra_unlabeled)
    if args.judge_sample is not None:
        judge_records = judge_records[: args.judge_sample]
    extraction_judge_results = run_extraction_judge(judge_records)
    chat_judge_results = run_chat_judge(judge_chat_inputs)

    print("Computing calibration...")
    extraction_calibration = compute_extraction_calibration(extraction_judge_results, field_bucket_lookup)
    chat_cases_raw = _load_json(CHAT_CASES_PATH)
    chat_calibration = compute_chat_calibration(chat_judge_results, chat_cases_raw)

    report = build_report(
        classification_metrics,
        extraction_metrics,
        chat_results,
        chat_judge_results,
        extraction_calibration,
        chat_calibration,
        judge_sample_size=len(judge_records),
    )

    (OUTPUTS_DIR / "eval_report.md").write_text(report)
    (OUTPUTS_DIR / "eval_results.json").write_text(json.dumps({
        "classification": {"metrics": classification_metrics, "labels": classification_labels},
        "extraction": {"metrics": extraction_metrics, "results": extraction_results},
        "chat": chat_results,
    }, indent=2, default=str))
    (OUTPUTS_DIR / "eval_judge_raw.json").write_text(json.dumps({
        "extraction_judge": extraction_judge_results,
        "chat_judge": chat_judge_results,
        "extraction_calibration": extraction_calibration,
        "chat_calibration": chat_calibration,
    }, indent=2, default=str))

    token_totals = get_token_totals()
    (OUTPUTS_DIR / "eval_token_summary.txt").write_text(
        "\n".join(f"{task}: {counts}" for task, counts in token_totals.items())
    )

    print(f"\nDone. Wrote outputs/eval_report.md, eval_results.json, eval_judge_raw.json, eval_token_summary.txt")


if __name__ == "__main__":
    main()
