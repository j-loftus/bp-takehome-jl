"""
Quick data-quality diagnostics for contracts.db after a full pipeline run.

Run from the repo root (wherever contracts.db lives, default data/contracts.db):
    python eval_quick_checks.py
    python eval_quick_checks.py --db path/to/contracts.db

This is NOT the formal eval harness (Task 3.x) — it's a fast pre-flight gut check
to catch systematic problems before building RAG/chat/eval on top of the table.
"""

import re
import sqlite3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.prompts import INJECTED_FIELDS

DOC_TYPES = [
    "fully_executed_agreement",
    "renewal_letter",
    "modification_amendment",
    "award_letter",
    "vendor_disclosure_statement",
    "other",
]

SPINE_FIELDS = ["contract_number", "doc_type", "vendor_name", "doc_date", "county_department"]

def _fields_for_doc_type(doc_type: str) -> list[str]:
    """Parse field names from the ### headers in the injected prompt block."""
    block = INJECTED_FIELDS.get(doc_type, "")
    return re.findall(r"^### (\w+)", block, re.MULTILINE)

FIELDS_BY_DOC_TYPE: dict[str, list[str]] = {
    dt: _fields_for_doc_type(dt) for dt in DOC_TYPES
}


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def check_row_count_and_doc_type_distribution(conn):
    print("=" * 70)
    print("1. ROW COUNT & DOC TYPE DISTRIBUTION")
    print("=" * 70)
    total = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    print(f"Total rows written: {total}\n")

    rows = conn.execute(
        "SELECT doc_type, COUNT(*) as n FROM contracts GROUP BY doc_type ORDER BY n DESC"
    ).fetchall()
    for r in rows:
        pct = 100 * r["n"] / total if total else 0
        print(f"  {r['doc_type']:30s} {r['n']:4d}  ({pct:.1f}%)")

    # Compare against expected share from extraction_schema.md Section 3
    expected_share = {
        "fully_executed_agreement": 22, "renewal_letter": 27,
        "modification_amendment": 13, "award_letter": 11,
        "vendor_disclosure_statement": 12, "other": 15,
    }
    print("\n  Expected (~%) per corpus recon: ", expected_share)
    print("  Large deviations here are a classification red flag, not just noise.\n")


def check_spine_completeness(conn):
    print("=" * 70)
    print("2. UNIVERSAL SPINE COMPLETENESS (should be ~100% — these are non-nullable)")
    print("=" * 70)
    total = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    for field in SPINE_FIELDS:
        n_null = conn.execute(
            f"SELECT COUNT(*) FROM contracts WHERE {field} IS NULL OR TRIM({field}) = ''"
        ).fetchone()[0]
        flag = "  <-- INVESTIGATE" if n_null > 0 else ""
        print(f"  {field:25s} null/empty: {n_null:4d} / {total}{flag}")
    print()


def check_missingness_by_doctype(conn):
    print("=" * 70)
    print("3. MISSINGNESS BY DOC TYPE (the check that actually matters)")
    print("=" * 70)
    print("Nulls are BY DESIGN for fields outside a doc type's expected coverage.")
    print("Watch for: dense fields with high null rate WITHIN their own doc type.\n")

    for doc_type in DOC_TYPES:
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM contracts WHERE doc_type = ?", (doc_type,)
        ).fetchone()[0]
        if n_rows == 0:
            print(f"--- {doc_type}: 0 rows, skipping ---\n")
            continue

        print(f"--- {doc_type} (n={n_rows}) ---")
        fields = FIELDS_BY_DOC_TYPE.get(doc_type, [])
        if not fields:
            print("    (no type-specific fields for this doc type)\n")
            continue
        for field in fields:
            n_null = conn.execute(
                f"SELECT COUNT(*) FROM contracts WHERE doc_type = ? AND {field} IS NULL",
                (doc_type,),
            ).fetchone()[0]
            pct_null = 100 * n_null / n_rows
            marker = "  <-- investigate" if pct_null > 50 else ""
            print(f"    {field:28s} {n_null:3d}/{n_rows} null ({pct_null:5.1f}%){marker}")
        print()


def check_extraction_method_and_confidence(conn):
    print("=" * 70)
    print("4. EXTRACTION METHOD & CONFIDENCE")
    print("=" * 70)
    total = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]

    print("  Extraction method (text vs vision):")
    rows = conn.execute(
        "SELECT extraction_method, COUNT(*) as n FROM contracts "
        "GROUP BY extraction_method ORDER BY n DESC"
    ).fetchall()
    for r in rows:
        pct = 100 * r["n"] / total if total else 0
        print(f"    {str(r['extraction_method']):8s} {r['n']:4d}  ({pct:.1f}%)")

    print("\n  Extraction confidence:")
    rows = conn.execute(
        "SELECT extraction_confidence, COUNT(*) as n FROM contracts "
        "GROUP BY extraction_confidence ORDER BY n DESC"
    ).fetchall()
    for r in rows:
        pct = 100 * r["n"] / total if total else 0
        print(f"    {str(r['extraction_confidence']):8s} {r['n']:4d}  ({pct:.1f}%)")

    print()
    print("  Confidence vs doc_type — low confidence concentrated in one doc_type")
    print("  points to a specific weak spot, not generalized noise.\n")
    rows = conn.execute(
        "SELECT doc_type, extraction_confidence, COUNT(*) as n FROM contracts "
        "GROUP BY doc_type, extraction_confidence ORDER BY doc_type, n DESC"
    ).fetchall()
    for r in rows:
        print(f"    {r['doc_type']:30s} {str(r['extraction_confidence']):8s} {r['n']:4d}")
    print()


def check_extraction_notes(conn):
    print("=" * 70)
    print("5. EXTRACTION NOTES (free-text flags — read these, don't just count them)")
    print("=" * 70)
    rows = conn.execute(
        "SELECT source_filename, doc_type, extraction_notes FROM contracts "
        "WHERE extraction_notes IS NOT NULL AND TRIM(extraction_notes) != ''"
    ).fetchall()
    print(f"  {len(rows)} rows have a non-empty extraction_notes field:\n")
    for r in rows:
        print(f"    [{r['doc_type']}] {r['source_filename']}: {r['extraction_notes']}")
    print()


def check_contract_family_linkage(conn):
    print("=" * 70)
    print("6. CONTRACT FAMILY LINKAGE")
    print("=" * 70)
    n_families = conn.execute(
        "SELECT COUNT(DISTINCT contract_number) FROM contracts"
    ).fetchone()[0]
    print(f"  Distinct contract_number values: {n_families}")

    rows = conn.execute(
        "SELECT contract_number, COUNT(*) as n FROM contracts "
        "GROUP BY contract_number HAVING n = 1"
    ).fetchall()
    print(f"  Contract families with only 1 document: {len(rows)}")
    print("  (Expected for some — e.g. a standalone award letter with no amendments —")
    print("   but if most families show n=1, contract_number linkage may be broken,")
    print("   or sampling didn't actually capture complete families.)\n")

    rows = conn.execute(
        "SELECT contract_number, COUNT(*) as n FROM contracts "
        "GROUP BY contract_number ORDER BY n DESC LIMIT 5"
    ).fetchall()
    print("  Largest families (top 5):")
    for r in rows:
        print(f"    {r['contract_number']:20s} {r['n']} documents")
    print()


def check_inferred_fields_sanity(conn):
    print("=" * 70)
    print("7. INFERRED FIELD VALUE DISTRIBUTIONS (sanity check, not accuracy check)")
    print("=" * 70)
    print("  Per extraction_schema.md Section 10, these three fields are MODEL")
    print("  INFERENCES, not direct extractions — expect some noise. This just checks")
    print("  the value distributions aren't degenerate (e.g. everything defaulting to")
    print("  one bucket, which would suggest the prompt isn't actually discriminating).\n")

    for field in ["service_category", "auto_renewal_flag", "price_escalator_terms"]:
        print(f"  --- {field} ---")
        rows = conn.execute(
            f"SELECT {field}, COUNT(*) as n FROM contracts GROUP BY {field} ORDER BY n DESC"
        ).fetchall()
        for r in rows:
            print(f"      {str(r[field]):30s} {r['n']}")
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/db/contracts.db")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        check_row_count_and_doc_type_distribution(conn)
        check_spine_completeness(conn)
        check_missingness_by_doctype(conn)
        check_extraction_method_and_confidence(conn)
        check_extraction_notes(conn)
        check_contract_family_linkage(conn)
        check_inferred_fields_sanity(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()