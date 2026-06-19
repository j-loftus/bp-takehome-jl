"""
Vector store ingestion and retrieval module.

Chunks parsed document text, embeds via ChromaDB's default sentence-transformers
embedding function, and stores chunks in a local ChromaDB collection. Exposes
build_index() for ingestion and query_index() for semantic retrieval.

Standalone — does not depend on Streamlit. Run from the CLI before Task 2.7 exists:
    python -m src.build_vector_store --build
    python -m src.build_vector_store --query "auto-renewal clauses"
    python -m src.build_vector_store --query "termination" --filter doc_type=fully_executed_agreement
"""

import argparse
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("build_vector_store")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [build_vector_store] [%(levelname)s] %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

CHROMA_PATH: str = os.environ.get("VECTOR_STORE_DIR", "data/chroma")
COLLECTION_NAME: str = "contract_chunks"

_DEFAULT_DB_PATH: str = os.environ.get("DB_PATH", "data/db/contracts.db")
_OUTPUTS_DIR: Path = Path("outputs")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def get_client(chroma_path: str = CHROMA_PATH) -> chromadb.PersistentClient:
    Path(chroma_path).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=chroma_path)


def chunk_text(text: str, chunk_size: int = 700, overlap: int = 100) -> list[str]:
    """Split text into overlapping fixed-size chunks."""
    if not text:
        return []
    step = chunk_size - overlap
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        if start + chunk_size >= len(text):
            break
        start += step
    return chunks


def _load_sqlite_metadata(db_path: str) -> dict[str, dict]:
    """Return a dict keyed by source_filename with contract metadata from SQLite."""
    if not Path(db_path).exists():
        logger.warning("SQLite database not found at %s — all chunks will have 'unknown' metadata", db_path)
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source_filename, contract_number, doc_type, vendor_name, "
            "doc_date, extraction_method FROM contracts"
        ).fetchall()
        conn.close()
        return {
            row["source_filename"]: {
                "contract_number": row["contract_number"] or "unknown",
                "doc_type": row["doc_type"] or "unknown",
                "vendor_name": row["vendor_name"] or "unknown",
                "doc_date": row["doc_date"] or "unknown",
                "extraction_method": row["extraction_method"] or "unknown",
            }
            for row in rows
        }
    except Exception:
        logger.exception("Failed to load SQLite metadata from %s", db_path)
        return {}


_UNKNOWN_META = {
    "contract_number": "unknown",
    "doc_type": "unknown",
    "vendor_name": "unknown",
    "doc_date": "unknown",
    "extraction_method": "unknown",
}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_index(
    parse_results: list[dict],
    db_path: str = _DEFAULT_DB_PATH,
    chroma_path: str = CHROMA_PATH,
) -> dict:
    """
    Build (or rebuild) the ChromaDB vector store from parsed document text.

    Idempotent: clears and recreates the collection before inserting.
    Skips scanned documents and documents with empty text.
    Joins SQLite metadata by source_filename; unjoined docs get "unknown" fields.

    Returns a summary dict with counts for the build report.
    """
    sqlite_meta = _load_sqlite_metadata(db_path)

    client = get_client(chroma_path)

    try:
        client.delete_collection(name=COLLECTION_NAME)
        logger.info("Deleted existing collection '%s'", COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(name=COLLECTION_NAME)
    logger.info("Created fresh collection '%s'", COLLECTION_NAME)

    skipped_scanned: list[str] = []
    skipped_empty: list[str] = []
    unjoined: list[str] = []

    all_ids: list[str] = []
    all_documents: list[str] = []
    all_metadatas: list[dict] = []

    for doc in parse_results:
        filename: str = doc.get("filename", "")
        text: str = doc.get("text", "") or ""
        is_scanned: bool = doc.get("is_scanned", False)

        if is_scanned:
            logger.warning("Skipping scanned document: %s", filename)
            skipped_scanned.append(filename)
            continue

        if not text.strip():
            if not is_scanned:
                logger.warning(
                    "Skipping document with empty text (not flagged scanned in parse results): %s",
                    filename,
                )
            else:
                logger.warning("Skipping empty-text document: %s", filename)
            skipped_empty.append(filename)
            continue

        meta = sqlite_meta.get(filename)
        if meta is None:
            logger.warning(
                "No matching SQLite row for '%s' — embedding with 'unknown' metadata", filename
            )
            unjoined.append(filename)
            meta = _UNKNOWN_META.copy()

        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            chunk_id = f"{filename}::chunk_{i}"
            all_ids.append(chunk_id)
            all_documents.append(chunk)
            all_metadatas.append({
                "source_filename": filename,
                "contract_number": meta["contract_number"],
                "doc_type": meta["doc_type"],
                "vendor_name": meta["vendor_name"],
                "doc_date": meta["doc_date"],
                "extraction_method": meta["extraction_method"],
                "chunk_index": i,
            })

    _BATCH_SIZE = 500
    if all_ids:
        for start in range(0, len(all_ids), _BATCH_SIZE):
            collection.add(
                ids=all_ids[start : start + _BATCH_SIZE],
                documents=all_documents[start : start + _BATCH_SIZE],
                metadatas=all_metadatas[start : start + _BATCH_SIZE],
            )
        logger.info("Added %d chunks to collection", len(all_ids))

    docs_processed = len(parse_results) - len(skipped_scanned) - len(skipped_empty)
    chunk_counts = [
        sum(1 for m in all_metadatas if m["source_filename"] == doc.get("filename"))
        for doc in parse_results
        if doc.get("filename") not in skipped_scanned
        and doc.get("filename") not in skipped_empty
    ]

    summary = {
        "documents_processed": docs_processed,
        "documents_skipped_scanned": len(skipped_scanned),
        "documents_skipped_empty": len(skipped_empty),
        "documents_unjoined": len(unjoined),
        "total_chunks_created": len(all_ids),
        "collection_name": COLLECTION_NAME,
        "chroma_path": chroma_path,
        "source_count": len(parse_results),
        "chunk_counts": chunk_counts,
    }

    _outputs_dir = _OUTPUTS_DIR
    _outputs_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(_outputs_dir / "vector_store_build_summary.txt")
    print_build_summary(summary, skipped_scanned, skipped_empty, unjoined, output_path)

    return summary


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def print_build_summary(
    summary: dict,
    skipped_scanned: list[str],
    skipped_empty: list[str],
    unjoined: list[str],
    output_path: str,
) -> None:
    """Write and log the vector store build summary report."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    total = summary["source_count"]
    processed = summary["documents_processed"]
    n_scanned = summary["documents_skipped_scanned"]
    n_empty = summary["documents_skipped_empty"]
    n_unjoined = summary["documents_unjoined"]
    total_chunks = summary["total_chunks_created"]
    chunk_counts = summary.get("chunk_counts", [])

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "0.0%"

    avg_chunks = f"{total_chunks / processed:.2f}" if processed else "0.00"
    min_chunks = min(chunk_counts) if chunk_counts else 0
    max_chunks = max(chunk_counts) if chunk_counts else 0

    scanned_list = "\n".join(f"  {f}" for f in skipped_scanned) or "  (none)"
    empty_list = "\n".join(f"  {f}" for f in skipped_empty) or "  (none)"
    unjoined_list = "\n".join(f"  {f}  — no matching row in contracts table" for f in unjoined) or "  (none)"

    report = f"""=== Vector Store Build Summary ===
Run timestamp:        {ts}
Source:               parse_directory() output ({total} documents)
Chroma path:          {summary['chroma_path']}
Collection name:      {summary['collection_name']}

--- Document Processing ---
  Embedded:                  {processed:4d}  ({pct(processed)})
  Skipped (scanned):         {n_scanned:4d}  ({pct(n_scanned)})
  Skipped (empty text):      {n_empty:4d}  ({pct(n_empty)})
  Unjoined (no SQLite row):  {n_unjoined:4d}  ({pct(n_unjoined)})  <- embedded, metadata set to "unknown"

--- Chunk Volume ---
  Total chunks created:    {total_chunks}
  Avg chunks per document: {avg_chunks}
  Min chunks (1 doc):      {min_chunks}
  Max chunks (1 doc):      {max_chunks}

--- Skipped: Scanned Documents ---
{scanned_list}

--- Skipped: Empty Text (non-scanned) ---
{empty_list}

--- Unjoined Documents (embedded with "unknown" metadata) ---
{unjoined_list}

Collection ready for queries: {total_chunks} chunks across {processed} documents.
"""

    logger.info(report)
    Path(output_path).write_text(report)
    logger.info("Build summary written to %s", output_path)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def query_index(
    query_text: str,
    n_results: int = 5,
    where: dict | None = None,
    chroma_path: str = CHROMA_PATH,
) -> list[dict]:
    """
    Run a semantic query against the vector store.

    Returns a list of dicts (one per chunk) or [] if the collection is empty
    or does not exist. Never raises on an empty/missing collection.
    """
    try:
        client = get_client(chroma_path)
        collection = client.get_collection(name=COLLECTION_NAME)
    except Exception:
        logger.warning("Collection '%s' not found at %s — returning empty results", COLLECTION_NAME, chroma_path)
        return []

    if collection.count() == 0:
        logger.warning("Collection '%s' is empty — returning empty results", COLLECTION_NAME)
        return []

    kwargs: dict = {"query_texts": [query_text], "n_results": min(n_results, collection.count())}
    if where:
        kwargs["where"] = where

    try:
        results = collection.query(**kwargs)
    except Exception:
        logger.exception("Query failed for text: %r", query_text)
        return []

    chunks = []
    ids = results.get("ids", [[]])[0]
    documents = results.get("documents", [[]])[0]
    distances = results.get("distances", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]

    for doc_text, distance, meta in zip(documents, distances, metadatas):
        chunks.append({
            "chunk_text": doc_text,
            "distance": distance,
            "source_filename": meta.get("source_filename", "unknown"),
            "contract_number": meta.get("contract_number", "unknown"),
            "doc_type": meta.get("doc_type", "unknown"),
            "vendor_name": meta.get("vendor_name", "unknown"),
            "doc_date": meta.get("doc_date", "unknown"),
            "extraction_method": meta.get("extraction_method", "unknown"),
            "chunk_index": meta.get("chunk_index", -1),
        })

    return chunks


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    CLI entry point for building and querying the vector store standalone.

    Usage:
        python -m src.build_vector_store --build
        python -m src.build_vector_store --query "which contracts have auto-renewal clauses?"
        python -m src.build_vector_store --query "termination terms" --filter doc_type=fully_executed_agreement
    """
    parser = argparse.ArgumentParser(
        description="Build or query the contract vector store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true", help="Parse contracts and build the vector store index.")
    group.add_argument("--query", metavar="TEXT", help="Run a semantic query against the existing index.")
    parser.add_argument(
        "--filter",
        metavar="KEY=VALUE",
        help="Metadata filter for --query, e.g. doc_type=fully_executed_agreement",
    )
    parser.add_argument("--n", type=int, default=5, metavar="N", help="Number of results for --query (default: 5)")
    args = parser.parse_args()

    if args.build:
        import csv
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from src.pipeline.pdf_parser import parse_pdf

        manifest_path = Path("data/sample_manifest.csv")
        if not manifest_path.exists():
            logger.error("sample_manifest.csv not found at %s — cannot build index", manifest_path)
            raise SystemExit(1)

        with open(manifest_path, newline="") as f:
            sampled_rows = list(csv.DictReader(f))

        filepaths = [row["filepath"] for row in sampled_rows]
        logger.info("Parsing %d documents from sample manifest …", len(filepaths))

        parse_results = []
        for fp in filepaths:
            parse_results.append(parse_pdf(fp))
        logger.info("Parsed %d documents", len(parse_results))

        summary = build_index(parse_results)
        print(
            f"\nDone. {summary['total_chunks_created']} chunks across "
            f"{summary['documents_processed']} documents in collection '{COLLECTION_NAME}'."
        )

    else:
        where = None
        if args.filter:
            try:
                key, value = args.filter.split("=", 1)
                where = {key.strip(): value.strip()}
            except ValueError:
                print(f"Invalid --filter format '{args.filter}'. Expected KEY=VALUE.")
                raise SystemExit(1)

        results = query_index(args.query, n_results=args.n, where=where)

        if not results:
            print("No results — collection may be empty. Run --build first.")
            return

        print(f"\n--- Query: {args.query!r} ---\n")
        for i, r in enumerate(results, 1):
            snippet = r["chunk_text"][:200].replace("\n", " ")
            print(
                f"[{i}] {r['source_filename']}  (contract: {r['contract_number']}, "
                f"type: {r['doc_type']}, dist: {r['distance']:.4f})\n"
                f"    {snippet}…\n"
            )


if __name__ == "__main__":
    main()
