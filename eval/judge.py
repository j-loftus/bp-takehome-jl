"""
LLM-as-judge — the scale layer of the two-layer eval (docs/evaluation.md §5).

Two reference-free judges, both temperature=0 for reproducibility:
  - Extraction faithfulness judge: is each extracted field value supported by
    the source document? Text path reads cached parse text; scanned/vision
    documents are judged from page images via call_llm_with_images (mirrors
    the extractor's own vision path) rather than skipped.
  - Chat answer judge: is the generated answer grounded in retrieved context
    and does it address the question?

Both are batch-capable (`sample_n`) — built full-corpus-capable, run on a
sample for the PoC (§7). Judge calibration (§5.3) compares the extraction
judge's verdicts against ground truth on the labeled docs — the number that
justifies leaning on the judge where no ground truth exists.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from src.llm_client import LLMCallError, call_llm, call_llm_with_images, extract_json
from src.pipeline.pdf_parser import extract_page_images

REPO_ROOT = Path(__file__).resolve().parent.parent
PARSE_CACHE_PATH = REPO_ROOT / "data" / "parse_cache.json"
CONTRACTS_DB_PATH = REPO_ROOT / "data" / "contracts.db"
PDF_DIR = REPO_ROOT / "contracts"
GROUND_TRUTH_EXTRACTION_PATH = REPO_ROOT / "eval" / "ground_truth_extraction.json"

TEXT_CHAR_BUDGET = 20_000

UNLABELED = "__UNLABELED__"


# ---------------------------------------------------------------------------
# Extraction faithfulness judge
# ---------------------------------------------------------------------------

_EXTRACTION_JUDGE_PROMPT = """You are auditing an automated contract-data-extraction pipeline. You will be \
shown a source document ({source_kind}) and a set of field values the pipeline extracted from it. For each \
populated field, determine whether the value is supported by the document text — i.e. you can find clear \
textual (or visual) evidence for it.

Document type: {doc_type}

Extracted fields (JSON):
{extracted_fields_json}

Rules:
- Judge ONLY whether each value is supported by the source. Do not judge whether the value is "correct" by \
some external standard — only whether the document itself supports it.
- Do NOT penalize fields with a null value — null is not in scope for this judgment.
- Be skeptical of values that are plausible-sounding but not actually stated in the source.

Return ONLY raw JSON (no prose, no code fences) in exactly this shape:
{{
  "per_field": {{
    "<field_name>": {{"supported": true|false, "reason": "<one sentence>"}}
    // one entry per populated field above
  }},
  "doc_faithfulness_score": <integer 1-5, overall faithfulness of the extraction for this document>,
  "summary": "<one or two sentences>"
}}
"""


def _populated_fields(extracted_fields: dict) -> dict:
    return {k: v for k, v in extracted_fields.items() if v is not None}


def _build_extraction_judge_prompt(doc_type: str, extracted_fields: dict, source_kind: str) -> str:
    return _EXTRACTION_JUDGE_PROMPT.format(
        source_kind=source_kind,
        doc_type=doc_type,
        extracted_fields_json=json.dumps(_populated_fields(extracted_fields), indent=2, default=str),
    )


def judge_extraction_text(source_text: str, doc_type: str, extracted_fields: dict) -> dict:
    truncated = len(source_text) > TEXT_CHAR_BUDGET
    body = source_text[:TEXT_CHAR_BUDGET]
    prompt = _build_extraction_judge_prompt(doc_type, extracted_fields, "text excerpt below") + (
        f"\nSource text{' (truncated)' if truncated else ''}:\n{body}\n"
    )
    raw = call_llm(prompt, task="judge", max_tokens=2000, temperature=0)
    result = extract_json(raw)
    result["judge_method"] = "text"
    result["truncated"] = truncated
    return result


def judge_extraction_vision(filepath: str, doc_type: str, extracted_fields: dict) -> dict:
    images = extract_page_images(filepath, max_pages=20)
    prompt = _build_extraction_judge_prompt(doc_type, extracted_fields, "page images attached")
    raw = call_llm_with_images(prompt, images, task="judge", max_tokens=2000, temperature=0)
    result = extract_json(raw)
    result["judge_method"] = "vision"
    result["truncated"] = False
    return result


def judge_extraction(record: dict) -> dict:
    """record: {source_filename, doc_type, extracted_fields, is_scanned, source_text, filepath}"""
    try:
        if record.get("is_scanned"):
            result = judge_extraction_vision(record["filepath"], record["doc_type"], record["extracted_fields"])
        else:
            result = judge_extraction_text(record["source_text"], record["doc_type"], record["extracted_fields"])
    except (LLMCallError, json.JSONDecodeError) as e:
        result = {"per_field": {}, "doc_faithfulness_score": None, "summary": f"judge error: {e}", "judge_method": "error"}
    result["source_filename"] = record["source_filename"]
    return result


def run_extraction_judge(records: list[dict], sample_n: int | None = None) -> list[dict]:
    sample = records[:sample_n] if sample_n is not None else records
    return [judge_extraction(r) for r in sample]


# ---------------------------------------------------------------------------
# Chat answer judge
# ---------------------------------------------------------------------------

_CHAT_JUDGE_PROMPT = """You are auditing a contract-Q&A chatbot. You will be shown the user's question, the \
contract excerpts retrieved to answer it, and the chatbot's generated answer.

Question: {question}

Retrieved context:
{context}

Generated answer:
{answer}

Score on two dimensions, 1-5 each:
- faithfulness: is the answer fully grounded in the retrieved context, with nothing invented or added?
- relevance: does the answer actually address the question asked?

Return ONLY raw JSON (no prose, no code fences) in exactly this shape:
{{
  "faithfulness": <integer 1-5>,
  "relevance": <integer 1-5>,
  "faithfulness_reason": "<one sentence>",
  "relevance_reason": "<one sentence>"
}}
"""


def judge_chat(question: str, retrieved_chunks: list[dict], answer: str) -> dict:
    context = "\n\n---\n\n".join(
        f"[{c.get('source_filename', 'unknown')}] {c.get('chunk_text', '')}" for c in retrieved_chunks
    )
    prompt = _CHAT_JUDGE_PROMPT.format(question=question, context=context or "(no context retrieved)", answer=answer)
    try:
        raw = call_llm(prompt, task="judge", max_tokens=600, temperature=0)
        return extract_json(raw)
    except (LLMCallError, json.JSONDecodeError) as e:
        return {"faithfulness": None, "relevance": None, "faithfulness_reason": f"judge error: {e}", "relevance_reason": ""}


def run_chat_judge(cases: list[dict], sample_n: int | None = None) -> list[dict]:
    sample = cases[:sample_n] if sample_n is not None else cases
    results = []
    for case in sample:
        result = judge_chat(case["question"], case.get("retrieved_chunks", []), case["answer"])
        result["id"] = case["id"]
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Judge calibration (§5.3) — the bridge between ground truth and the judge
# ---------------------------------------------------------------------------

# Ground-truth buckets that should align with judge supported=True / False.
_SUPPORTED_BUCKETS = {"correct"}
_UNSUPPORTED_BUCKETS = {"wrong_value", "missed", "hallucinated"}


def compute_extraction_calibration(judge_results: list[dict], ground_truth_field_buckets: dict) -> dict:
    """Compare judge per-field `supported` verdicts against ground-truth buckets.

    `ground_truth_field_buckets`: {(source_filename, field): bucket} from
    eval/scoring.py's score_document_fields, restricted to populated cells
    (true_negative/skipped excluded — judge only sees populated fields).
    """
    agreements = 0
    total = 0
    disagreements = []

    for judge_result in judge_results:
        filename = judge_result["source_filename"]
        for field, verdict in judge_result.get("per_field", {}).items():
            bucket = ground_truth_field_buckets.get((filename, field))
            if bucket is None or bucket not in (_SUPPORTED_BUCKETS | _UNSUPPORTED_BUCKETS):
                continue
            expected_supported = bucket in _SUPPORTED_BUCKETS
            actual_supported = verdict.get("supported")
            total += 1
            if actual_supported == expected_supported:
                agreements += 1
            else:
                disagreements.append({
                    "source_filename": filename,
                    "field": field,
                    "ground_truth_bucket": bucket,
                    "judge_supported": actual_supported,
                    "judge_reason": verdict.get("reason"),
                })

    return {
        "agreement": agreements / total if total else None,
        "n_compared": total,
        "disagreements": disagreements,
    }


def compute_chat_calibration(chat_judge_results: list[dict], chat_cases: list[dict]) -> list[dict]:
    """Pair chat judge scores with operator-recorded known answers for the
    semantic cases that target a labeled document (§3.3, §5.3).

    There's no second automated signal to compare against here — known
    answers are free text. This returns the judge scores alongside the known
    answer for human-eyeball calibration in the report, not a computed
    agreement fraction.
    """
    known_answer_cases = {c["id"]: c for c in chat_cases if c.get("notes") and "TODO" not in c["notes"]}
    paired = []
    for result in chat_judge_results:
        case = known_answer_cases.get(result["id"])
        if case is None:
            continue
        paired.append({
            "id": result["id"],
            "question": case["question"],
            "known_answer_notes": case["notes"],
            "judge_faithfulness": result.get("faithfulness"),
            "judge_relevance": result.get("relevance"),
        })
    return paired


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_parse_cache() -> dict[str, dict]:
    entries = json.loads(PARSE_CACHE_PATH.read_text())
    return {e["filename"]: e for e in entries}


def _load_extraction_records(sample_n: int | None) -> list[dict]:
    """Build judge input records from contracts.db + parse_cache.json for the
    ground-truth-overlap docs (default judge sample, §7)."""
    parse_cache = _load_parse_cache()
    gt = json.loads(GROUND_TRUTH_EXTRACTION_PATH.read_text()) if GROUND_TRUTH_EXTRACTION_PATH.exists() else []
    filenames = [e["source_filename"] for e in gt]

    con = sqlite3.connect(f"file:{CONTRACTS_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT * FROM contracts WHERE source_filename IN ({','.join('?' * len(filenames))})",
            filenames,
        ).fetchall() if filenames else []
    finally:
        con.close()

    records = []
    for row in rows:
        row_dict = dict(row)
        filename = row_dict["source_filename"]
        cache_entry = parse_cache.get(filename, {})
        records.append({
            "source_filename": filename,
            "doc_type": row_dict["doc_type"],
            "extracted_fields": {
                k: v for k, v in row_dict.items()
                if k not in ("id", "source_filename", "pipeline_run_timestamp", "doc_type",
                             "extraction_confidence", "extraction_notes")
            },
            "is_scanned": cache_entry.get("is_scanned", False),
            "source_text": cache_entry.get("text", ""),
            "filepath": str(PDF_DIR / filename),
        })
    return records[:sample_n] if sample_n is not None else records


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LLM-as-judge layer standalone.")
    parser.add_argument("--sample", type=int, default=None, help="number of ground-truth-overlap docs to judge")
    args = parser.parse_args()

    records = _load_extraction_records(args.sample)
    print(f"Judging {len(records)} document(s)...")
    results = run_extraction_judge(records)
    for r in results:
        print(f"  {r['source_filename']}: faithfulness={r.get('doc_faithfulness_score')} method={r.get('judge_method')}")


if __name__ == "__main__":
    main()
