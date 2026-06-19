"""
End-to-end pipeline runner.

Usage: python scripts/run_pipeline.py [--limit N] [--manifest PATH]

Steps:
  1. Read sample manifest (data/sample_manifest.csv) to get the document list
  2. Parse PDFs → text  (Task 2.1)
  3. Classify each document  (Task 2.2)
  4. Extract structured fields  (Task 2.3 — not yet implemented)
  5. Write extracted JSON to data/extracted/  (not yet implemented)
  6. Insert into SQLite DB  (not yet implemented)
"""

import argparse
import csv
import sys
from pathlib import Path

# Ensure project root is on the path when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db_writer import DEFAULT_DB_PATH, initialize_db, write_extraction_result
from src.llm_client import reset_token_counters
from src.pipeline.classifier import classify_directory
from src.pipeline.extractor import extract_batch
from src.pipeline.pdf_parser import parse_pdf

DEFAULT_MANIFEST = Path("data/sample_manifest.csv")


def load_manifest(manifest_path: Path) -> list[str]:
    """Return a list of filepaths from the sample manifest CSV."""
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Sample manifest not found at {manifest_path}. "
            "Run scripts/sample_contracts.py first."
        )
    filepaths = []
    with open(manifest_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filepaths.append(row["filepath"])
    return filepaths


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the contract extraction pipeline.")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of documents to process (useful for testing)",
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help=f"Path to sample manifest CSV (default: {DEFAULT_MANIFEST})",
    )
    args = parser.parse_args()

    # ── Step 1: Load sample manifest ─────────────────────────────────────────
    filepaths = load_manifest(args.manifest)
    if args.limit:
        filepaths = filepaths[: args.limit]
    print(f"Manifest loaded: {len(filepaths)} documents to process")

    # ── Step 2: Parse PDFs ───────────────────────────────────────────────────
    print("\n[Step 2] Parsing PDFs...")
    parse_results = []
    for filepath in filepaths:
        result = parse_pdf(filepath)
        parse_results.append(result)
    print(f"Parsed {len(parse_results)} documents")

    # ── Step 3: Classify documents ───────────────────────────────────────────
    reset_token_counters()
    print("\n[Step 3] Classifying documents...")
    classification_results = classify_directory(parse_results)
    print(f"Classified {len(classification_results)} documents")

    # ── Step 4: Extract structured fields ───────────────────────────────────
    print("\n[Step 4] Extracting structured fields...")
    successes, failures = extract_batch(parse_results, classification_results)
    print(f"Extraction complete: {len(successes)} succeeded, {len(failures)} skipped/failed")

    # ── Step 5: Write extracted JSON ─────────────────────────────────────────
    import json

    extracted_dir = Path("data/extracted")
    extracted_dir.mkdir(parents=True, exist_ok=True)
    for row in successes:
        out_path = extracted_dir / (Path(row["source_filename"]).stem + ".json")
        out_path.write_text(json.dumps(row, indent=2, default=str))
    print(f"JSON files written to {extracted_dir}/")

    # ── Step 6: Insert into SQLite DB ───────────────────────────────────────
    print(f"\n[Step 6] Writing to database: {DEFAULT_DB_PATH}")
    initialize_db(DEFAULT_DB_PATH)
    db_successes = 0
    db_failures = 0
    for row in successes:
        if write_extraction_result(row, DEFAULT_DB_PATH):
            db_successes += 1
        else:
            db_failures += 1
    print(f"DB write complete: {db_successes} inserted, {db_failures} failed")


if __name__ == "__main__":
    main()
