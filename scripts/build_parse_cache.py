"""
One-time utility: parse the sampled PDFs (data/sample_manifest.csv — the same
~99 documents that went through extraction into contracts.db) and write a
lightweight text cache to data/parse_cache.json.

Scoped to the sample manifest, not the full contracts/ directory: the
structured table only covers the sampled documents, so indexing the full
~389-doc corpus would let RAG surface vendors/contracts the dashboard and
text-to-SQL path know nothing about — an inconsistency during the walkthrough
(see docs/rag_implementation_notes.md, "Sample-scoped indexing").

The cache contains the fields build_index() needs: filename, text, is_scanned,
page_count. The filepath field (local-absolute-path) is intentionally dropped
so the cache is portable and safe to commit.

Run from the project root:
    python scripts/build_parse_cache.py

Output:
    data/parse_cache.json   — committed to the repo; loaded at app startup.
"""

import csv
import json
import sys
from pathlib import Path

# Allow importing from src/ without installation.
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from pipeline.pdf_parser import parse_pdf

CONTRACTS_DIR = _PROJECT_ROOT / "contracts"
MANIFEST_PATH = _PROJECT_ROOT / "data" / "sample_manifest.csv"
OUTPUT_PATH = str(_PROJECT_ROOT / "data" / "parse_cache.json")


def _sampled_filenames() -> list[str]:
    with open(MANIFEST_PATH) as f:
        return [row["filename"] for row in csv.DictReader(f)]


def main() -> None:
    filenames = _sampled_filenames()
    print(f"Parsing {len(filenames)} sampled PDFs (per {MANIFEST_PATH.name}) from: {CONTRACTS_DIR}")

    results = [parse_pdf(str(CONTRACTS_DIR / fname)) for fname in filenames]

    cache = [
        {
            "filename": r["filename"],
            "text": r["text"],
            "is_scanned": r["is_scanned"],
            "page_count": r["page_count"],
        }
        for r in results
    ]

    with open(OUTPUT_PATH, "w") as f:
        json.dump(cache, f)

    scanned = sum(1 for r in cache if r["is_scanned"])
    has_text = sum(1 for r in cache if r["text"])
    print(
        f"Written {len(cache)} entries to {OUTPUT_PATH}\n"
        f"  {has_text} with text  |  {scanned} scanned (empty text)"
    )


if __name__ == "__main__":
    main()
