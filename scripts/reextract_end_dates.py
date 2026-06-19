#!/usr/bin/env python3
"""
Identify rows where contract_end_date is null but derivation should now be possible,
re-run extraction with the updated prompt, and print a before/after comparison.

Does NOT write to the database. Outputs a summary so you can review results before
deciding how to write back.

Usage:
    python scripts/reextract_end_dates.py
    python scripts/reextract_end_dates.py --db data/db/contracts.db --contracts contracts/
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic
from dotenv import load_dotenv

from src.pipeline.pdf_parser import extract_page_images, parse_pdf
from src.pipeline.prompts import build_extraction_prompt

load_dotenv()

DB_PATH      = Path(os.getenv("DB_PATH",       "data/db/contracts.db"))
CONTRACTS_DIR = Path(os.getenv("CONTRACTS_DIR", "contracts"))
MODEL        = os.getenv("EXTRACTION_MODEL",    "claude-sonnet-4-6")

AFFECTED_QUERY = """
SELECT id, source_filename, doc_type, contract_end_date, extraction_notes
FROM contracts
WHERE contract_end_date IS NULL
  AND doc_type IN (
        'fully_executed_agreement',
        'modification_amendment',
        'award_letter',
        'other'
      )
  AND (
        extraction_notes LIKE '%duration%'
     OR extraction_notes LIKE '%calendar day%'
     OR extraction_notes LIKE '%not explicitly stated%'
      )
ORDER BY doc_type, source_filename
"""


import base64


def call_extraction_api(doc_type: str, pdf_path: Path) -> dict:
    parse_result = parse_pdf(str(pdf_path))
    client = Anthropic()

    if parse_result["is_scanned"] or not parse_result["text"].strip():
        # Vision path: send page images directly to the model
        images = extract_page_images(str(pdf_path))
        if not images:
            raise ValueError("No images extracted from scanned PDF")
        prompt = build_extraction_prompt(doc_type, "[Document provided as images above]")
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(img).decode("utf-8"),
                },
            }
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
    else:
        # Text path
        text = parse_result["text"][:100_000]
        prompt = build_extraction_prompt(doc_type, text)
        content = prompt

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    return json.loads(raw)


def main(db_path: Path, contracts_dir: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(AFFECTED_QUERY).fetchall()
    conn.close()

    if not rows:
        print("No affected rows found — null contract_end_date with duration signal.")
        return

    print(f"Found {len(rows)} affected row(s). Re-extracting with updated prompt...\n")
    print("=" * 80)

    improved, unchanged, errored = [], [], []

    for row in rows:
        pdf_path = contracts_dir / row["source_filename"]
        row_id   = row["id"]
        filename = row["source_filename"]
        doc_type = row["doc_type"]

        print(f"\nID {row_id} | {filename}")
        print(f"  doc_type             : {doc_type}")
        print(f"  old contract_end_date: {row['contract_end_date']}")
        print(f"  old extraction_notes : {row['extraction_notes']}")

        if not pdf_path.exists():
            print(f"  ERROR: PDF not found at {pdf_path}")
            errored.append(row_id)
            continue

        try:
            result = call_extraction_api(doc_type, pdf_path)

            new_end  = result.get("contract_end_date")
            new_conf = result.get("extraction_confidence")
            new_notes = result.get("extraction_notes")

            print(f"  new contract_end_date: {new_end}")
            print(f"  new extraction_conf  : {new_conf}")
            print(f"  new extraction_notes : {new_notes}")

            if new_end:
                print("  *** IMPROVED — end date now populated ***")
                improved.append((row_id, filename, new_end, new_notes))
            else:
                print("  (still null — document may genuinely lack start+duration pair)")
                unchanged.append(row_id)

        except Exception as exc:
            print(f"  ERROR during extraction: {exc}")
            errored.append(row_id)

    # Summary
    print("\n" + "=" * 80)
    print(f"Summary: {len(improved)} improved | {len(unchanged)} still null | {len(errored)} errors")

    if improved:
        print("\nRows ready to write back (review before committing):")
        print(f"  {'id':<6}  {'new_end_date':<12}  filename")
        for row_id, filename, new_end, _ in improved:
            print(f"  {row_id:<6}  {new_end:<12}  {filename}")

    if errored:
        print(f"\nFailed IDs: {errored}")

    if not improved:
        return

    answer = input("\nWrite these back to the database? [y/N] ").strip().lower()
    if answer != "y":
        print("Skipped. No changes written.")
        return

    conn = sqlite3.connect(db_path)
    written = 0
    for row_id, filename, new_end, new_notes in improved:
        conn.execute(
            "UPDATE contracts SET contract_end_date = ?, extraction_notes = ? WHERE id = ?",
            (new_end, new_notes, row_id),
        )
        written += 1
    conn.commit()
    conn.close()
    print(f"Written {written} row(s) to {db_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db",        default=str(DB_PATH),       help="Path to contracts.db")
    parser.add_argument("--contracts", default=str(CONTRACTS_DIR), help="Path to contracts directory")
    args = parser.parse_args()

    main(Path(args.db), Path(args.contracts))
