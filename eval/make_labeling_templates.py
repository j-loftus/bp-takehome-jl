"""
Labeling helper — generates blank ground-truth skeletons + source-text dumps
for the operator to fill by hand (docs/evaluation.md §3.4).

Critically: no field values are ever read from data/contracts.db or any
pipeline output. The only thing sourced from the pipeline's own design is the
coverage matrix (src/pipeline/field_coverage.py) used to pre-null
null-by-design cells — that anchors on the schema, not on model output
(§5 "Label independently of model output").

Usage:
    python -m eval.make_labeling_templates --extraction-docs \\
        "19028_Fully_Executed_Agreement.pdf:fully_executed_agreement" \\
        "16069_Agreement_fully_executed.pdf:fully_executed_agreement" ...

    python -m eval.make_labeling_templates --classification-sample 25 --stratify
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path

from src.pipeline.classifier import DocType
from src.pipeline.field_coverage import FIELD_COVERAGE, Coverage

REPO_ROOT = Path(__file__).resolve().parent.parent
PARSE_CACHE_PATH = REPO_ROOT / "data" / "parse_cache.json"
CONTRACTS_DB_PATH = REPO_ROOT / "data" / "contracts.db"
PDF_DIR = REPO_ROOT / "contracts"
LABELING_DIR = REPO_ROOT / "eval" / "labeling"
EXTRACTION_GT_PATH = REPO_ROOT / "eval" / "ground_truth_extraction.json"
CLASSIFICATION_GT_PATH = REPO_ROOT / "eval" / "ground_truth_classification.json"

UNLABELED = "__UNLABELED__"
TEXT_CHAR_BUDGET = 20_000  # matches the judge's truncation budget (§5.1)


def _load_parse_cache() -> dict[str, dict]:
    entries = json.loads(PARSE_CACHE_PATH.read_text())
    return {e["filename"]: e for e in entries}


def _write_text_dump(filename: str, entry: dict | None) -> None:
    LABELING_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LABELING_DIR / f"{filename}.txt"

    if entry is None:
        out_path.write_text(f"NO PARSE CACHE ENTRY FOUND for {filename}.\n")
        return

    if entry.get("is_scanned") and not entry.get("text"):
        out_path.write_text(
            f"SCANNED DOCUMENT — no cached text (vision-extracted, page_count="
            f"{entry.get('page_count')}).\nOpen {PDF_DIR / filename} directly to label this "
            f"document.\n"
        )
        return

    text = entry.get("text", "")
    truncated = text[:TEXT_CHAR_BUDGET]
    note = "\n\n[...truncated...]\n" if len(text) > TEXT_CHAR_BUDGET else ""
    out_path.write_text(truncated + note)


def _blank_fields_for_doc_type(doc_type_str: str) -> dict:
    doc_type = DocType(doc_type_str)
    coverage = FIELD_COVERAGE[doc_type]
    return {
        field: None if cov == Coverage.NULL_BY_DESIGN else UNLABELED
        for field, cov in coverage.items()
        if field != "doc_type"
    }


def _load_existing(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    return []


def build_extraction_skeleton(doc_specs: list[tuple[str, str]]) -> None:
    """doc_specs: list of (filename, intended_doc_type) — intended type is a
    human/operator decision used only to pick the right coverage-matrix row;
    it is NOT written into the skeleton's gold_doc_type, which stays
    UNLABELED until the operator independently confirms it from the text."""
    parse_cache = _load_parse_cache()
    existing = _load_existing(EXTRACTION_GT_PATH)
    existing_filenames = {e["source_filename"] for e in existing}

    for filename, intended_doc_type in doc_specs:
        _write_text_dump(filename, parse_cache.get(filename))

        if filename in existing_filenames:
            print(f"skip (already in skeleton): {filename}")
            continue

        existing.append({
            "source_filename": filename,
            "gold_doc_type": UNLABELED,
            "fields": _blank_fields_for_doc_type(intended_doc_type),
            "notes": "",
        })
        print(f"added skeleton: {filename} (coverage row: {intended_doc_type})")

    EXTRACTION_GT_PATH.write_text(json.dumps(existing, indent=2) + "\n")


def _stratified_sample(n: int) -> list[str]:
    con = sqlite3.connect(f"file:{CONTRACTS_DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT source_filename, doc_type FROM contracts").fetchall()
    finally:
        con.close()

    by_type: dict[str, list[str]] = {}
    for filename, doc_type in rows:
        by_type.setdefault(doc_type, []).append(filename)

    total = len(rows)
    rng = random.Random(42)
    selected: list[str] = []
    for doc_type, filenames in by_type.items():
        quota = max(1, round(n * len(filenames) / total))
        rng.shuffle(filenames)
        selected.extend(filenames[:quota])

    return selected[:n]


def build_classification_skeleton(filenames: list[str], with_snippet: bool = True) -> None:
    parse_cache = _load_parse_cache() if with_snippet else {}
    existing = _load_existing(CLASSIFICATION_GT_PATH)
    existing_filenames = {e["source_filename"] for e in existing}

    for filename in filenames:
        if filename in existing_filenames:
            continue
        entry = {"source_filename": filename, "gold_doc_type": UNLABELED}
        if with_snippet:
            cache_entry = parse_cache.get(filename, {})
            if cache_entry.get("is_scanned") and not cache_entry.get("text"):
                entry["page1_snippet"] = f"[SCANNED — open {PDF_DIR / filename} directly]"
            else:
                entry["page1_snippet"] = cache_entry.get("text", "")[:400]
        existing.append(entry)

    CLASSIFICATION_GT_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"classification skeleton: {len(existing)} total entries -> {CLASSIFICATION_GT_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extraction-docs",
        nargs="+",
        metavar="FILENAME:DOC_TYPE",
        help="e.g. 19028_Fully_Executed_Agreement.pdf:fully_executed_agreement",
    )
    parser.add_argument("--classification-sample", type=int, help="number of docs to stratify-sample")
    parser.add_argument("--stratify", action="store_true", help="stratify the classification sample by doc_type")
    args = parser.parse_args()

    if args.extraction_docs:
        specs = []
        for spec in args.extraction_docs:
            filename, _, doc_type = spec.partition(":")
            if not doc_type:
                raise SystemExit(f"--extraction-docs entries must be FILENAME:DOC_TYPE, got: {spec}")
            specs.append((filename, doc_type))
        build_extraction_skeleton(specs)

    if args.classification_sample:
        if not args.stratify:
            raise SystemExit("--classification-sample requires --stratify in this implementation")
        filenames = _stratified_sample(args.classification_sample)
        build_classification_skeleton(filenames)


if __name__ == "__main__":
    main()
