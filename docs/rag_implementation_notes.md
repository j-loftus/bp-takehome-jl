# RAG Implementation Notes

Implementation decisions made during Task 2.6 that differ from or extend the spec in `rag.md`.

---

## 1. Sample-scoped indexing

The spec calls for chunking from `parse_directory()` output across the full contracts corpus. We scope the build to the sample manifest (`data/sample_manifest.csv`) instead.

**Why:** The structured SQLite table only covers the ~99 sampled documents. Indexing all 389 PDFs would let the chat interface surface content from documents the structured view knows nothing about, creating inconsistency during the walkthrough (e.g., a vendor appearing in RAG answers but absent from the portfolio table).

**How:** `main()` loads the manifest first and calls `parse_pdf()` per file rather than `parse_directory()` over the full corpus. `build_index()` itself is unchanged — the filtering happens at the call site, so `build_index()` remains general-purpose for any caller.

---

## 2. Env var key: `VECTOR_STORE_DIR` not `CHROMA_PATH`

The spec suggests a `CHROMA_PATH` env var. We use the existing `VECTOR_STORE_DIR` key already defined in `.env`.

**Why:** Avoids introducing a second env var for the same path. `VECTOR_STORE_DIR=./data/vector_store` was already present.

---

## 3. Batched `collection.add()` at 500 chunks per call

The spec says to accumulate all chunks and call `add()` once. ChromaDB enforces a max batch size of 5,461 — exceeded at ~99 docs with verbose text. We batch at 500 chunks per call.

**Why:** Hit `chromadb.errors.InternalError: Batch size of 10631 is greater than max batch size of 5461` on first run against the full corpus. 500 is a safe size well under the limit.
