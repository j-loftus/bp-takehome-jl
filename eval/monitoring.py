"""
Production monitoring — a thin wrapper over the eval, not a second system
(docs/evaluation.md §8). `monitoring_snapshot()` reuses eval/scoring.py's
field-coverage logic and eval/judge.py's judge runners on a live batch (no
ground truth required — these are reference-free signals). A baseline-compare
flags drift and logs alerts.

PoC scope: snapshot + baseline-compare + logged alerts only. No scheduler,
no alert channel, no metric store (§8, §11) — `save_baseline`/`load_baseline`
are a flat JSON file, just enough to demonstrate the comparison.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

from src.pipeline.classifier import DocType
from src.pipeline.field_coverage import FIELD_COVERAGE, Coverage

from eval.judge import judge_chat, judge_extraction

logger = logging.getLogger("eval.monitoring")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [monitoring] [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DEFAULT_BASELINE_PATH = Path(__file__).resolve().parent.parent / "outputs" / "monitoring_baseline.json"


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _distribution(values: list[int]) -> dict[int, int]:
    return dict(Counter(values))


def _null_rate_by_doc_type(extraction_records: list[dict]) -> dict[str, float]:
    """Fraction of non-null-by-design fields that are null, per doc_type.
    Descriptive only — no gold value, so this isn't a hallucination/missed
    split (that requires ground truth); it's the drift signal (§8)."""
    counts: dict[str, dict[str, int]] = {}
    for rec in extraction_records:
        doc_type = rec["doc_type"]
        coverage = FIELD_COVERAGE.get(DocType(doc_type), {})
        bucket = counts.setdefault(doc_type, {"null": 0, "total": 0})
        for field, cov in coverage.items():
            if cov == Coverage.NULL_BY_DESIGN or field == "doc_type":
                continue
            bucket["total"] += 1
            if rec["extracted_fields"].get(field) is None:
                bucket["null"] += 1
    return {dt: (b["null"] / b["total"] if b["total"] else None) for dt, b in counts.items()}


def monitoring_snapshot(batch: dict, judge_sample_n: int | None = None) -> dict:
    """batch: {
        "extraction_records": [{source_filename, doc_type, extracted_fields,
                                 is_scanned, source_text, filepath, extraction_failed}],
        "chat_records": [{question, retrieved_chunks, answer}],
        "classification_records": [{source_filename, confidence}],  # high/medium/low
    }
    Any key may be omitted/empty — the snapshot reports what it can.
    """
    extraction_records = [r for r in batch.get("extraction_records", []) if not r.get("extraction_failed")]
    chat_records = batch.get("chat_records", [])
    classification_records = batch.get("classification_records", [])

    sample = extraction_records[:judge_sample_n] if judge_sample_n is not None else extraction_records
    extraction_judge_results = [judge_extraction(r) for r in sample]
    chat_judge_results = [
        judge_chat(r["question"], r.get("retrieved_chunks", []), r["answer"]) for r in chat_records
    ]

    extraction_faithfulness = [
        r["doc_faithfulness_score"] for r in extraction_judge_results if r.get("doc_faithfulness_score") is not None
    ]
    chat_faithfulness = [r["faithfulness"] for r in chat_judge_results if r.get("faithfulness") is not None]

    hallucination_flags = [
        {"source_filename": r["source_filename"], "field": field, "reason": verdict.get("reason")}
        for r in extraction_judge_results
        for field, verdict in r.get("per_field", {}).items()
        if verdict.get("supported") is False
    ]

    all_records = batch.get("extraction_records", [])
    confidence_counts = Counter(r["confidence"] for r in classification_records)

    return {
        "n_extraction_records": len(all_records),
        "n_chat_records": len(chat_records),
        "extraction_judge_faithfulness": {
            "mean": _mean(extraction_faithfulness),
            "distribution": _distribution(extraction_faithfulness),
        },
        "chat_judge_faithfulness": {
            "mean": _mean(chat_faithfulness),
            "distribution": _distribution(chat_faithfulness),
        },
        "null_rate_by_doc_type": _null_rate_by_doc_type(extraction_records),
        "hallucination_flags": hallucination_flags,
        "classification_confidence_distribution": dict(confidence_counts),
        "scanned_document_rate": _mean([1.0 if r.get("is_scanned") else 0.0 for r in all_records]),
        "extraction_failure_rate": _mean([1.0 if r.get("extraction_failed") else 0.0 for r in all_records]),
    }


DEFAULT_THRESHOLDS = {
    "judge_score_mean_drop": 0.5,       # absolute drop in mean faithfulness (1-5 scale)
    "null_rate_spike": 0.10,            # absolute increase, e.g. 0.10 = +10pp
    "scanned_rate_spike": 0.10,
    "high_confidence_drop": 0.10,       # drop in fraction of "high" classification confidence
    "failure_rate_rise": 0.05,
}


def compare_to_baseline(snapshot: dict, baseline: dict, thresholds: dict | None = None) -> list[str]:
    """Flag drift vs. a stored baseline snapshot. Logs each alert; returns the list."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    alerts: list[str] = []

    for key in ("extraction_judge_faithfulness", "chat_judge_faithfulness"):
        cur_mean, base_mean = snapshot[key]["mean"], baseline.get(key, {}).get("mean")
        if cur_mean is not None and base_mean is not None and base_mean - cur_mean > t["judge_score_mean_drop"]:
            alerts.append(f"{key} mean dropped {base_mean:.2f} -> {cur_mean:.2f} (prompt/format drift?)")

    for doc_type, cur_rate in snapshot["null_rate_by_doc_type"].items():
        base_rate = baseline.get("null_rate_by_doc_type", {}).get(doc_type)
        if cur_rate is not None and base_rate is not None and cur_rate - base_rate > t["null_rate_spike"]:
            alerts.append(f"null rate for {doc_type} spiked {base_rate:.1%} -> {cur_rate:.1%} (document-format change?)")

    cur_scanned, base_scanned = snapshot["scanned_document_rate"], baseline.get("scanned_document_rate")
    if cur_scanned is not None and base_scanned is not None and cur_scanned - base_scanned > t["scanned_rate_spike"]:
        alerts.append(f"scanned-document rate spiked {base_scanned:.1%} -> {cur_scanned:.1%}")

    cur_conf, base_conf = snapshot["classification_confidence_distribution"], baseline.get("classification_confidence_distribution", {})
    cur_total, base_total = sum(cur_conf.values()), sum(base_conf.values())
    if cur_total and base_total:
        cur_high = cur_conf.get("high", 0) / cur_total
        base_high = base_conf.get("high", 0) / base_total
        if base_high - cur_high > t["high_confidence_drop"]:
            alerts.append(f"high-confidence classification share dropped {base_high:.1%} -> {cur_high:.1%}")

    cur_fail, base_fail = snapshot["extraction_failure_rate"], baseline.get("extraction_failure_rate")
    if cur_fail is not None and base_fail is not None and cur_fail - base_fail > t["failure_rate_rise"]:
        alerts.append(f"extraction failure rate rose {base_fail:.1%} -> {cur_fail:.1%}")

    for alert in alerts:
        logger.warning("DRIFT ALERT: %s", alert)
    if not alerts:
        logger.info("No drift detected vs. baseline.")

    return alerts


def save_baseline(snapshot: dict, path: Path = DEFAULT_BASELINE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, default=str))


def load_baseline(path: Path = DEFAULT_BASELINE_PATH) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}
