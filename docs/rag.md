# Task 2.6 — Vector Store / RAG Layer: Implementation Requirements

> **Purpose:** Specifies the vector store ingestion module for the contract intelligence
> pipeline. This module chunks extracted document text, embeds the chunks, and stores them
> in a local ChromaDB collection to support semantic queries the structured SQLite table
> cannot answer (e.g., "what are the termination terms across our facilities contracts?").
> It is a standalone, independently runnable module — not tightly coupled to the Streamlit
> app — so it can be built and tested locally before Task 2.7 exists.
>
> **Dependencies:**
> - Task 2.1 (PDF parsing module) — consumes `text`, `filename`, `filepath`, `is_scanned`
>   from `parse_directory()` output
> - Task 2.5 (SQLite database setup) — consumes `contract_number`, `doc_type`, `vendor_name`,
>   `doc_date`, `extraction_method` from the `contracts` table, joined by `source_filename`,
>   to attach as chunk metadata
> - `adr_scanned_document_vision_extraction.md` — the accepted-limitation decision that
>   scanned documents (empty `text`) do not get RAG coverage; this module implements that
>   decision by skipping them, not working around it
>
> **Downstream consumers:** Task 2.7 (Streamlit chat UI — semantic query path), Task 2.8
> (error handling — "no relevant context" failure mode), Task 3.x (eval harness — retrieval
> precision metric)

---

## 1. Module to Produce

| Module | Responsibility |
|--------|---------------|
| `src/build_vector_store.py` | Standalone module: chunk document text, embed, upsert into ChromaDB. Exposes a `build_index()` function callable from the pipeline orchestrator, from a CLI test entry point, and later from the Streamlit app's startup sequence. Also exposes a `query_index()` function for retrieval. |

This module does not depend on Streamlit and must be runnable and testable from the command
line in isolation, before Task 2.7 is built. This mirrors the `test_single_document()` pattern
established in Task 2.3.

---

## 2. Library and Setup

```
pip install chromadb
```

Embeddings: **local `sentence-transformers` via ChromaDB's default embedding function**
(already decided in Task 2.3 — no second API provider, no additional credentials, zero
marginal cost). Do not pass an `embedding_function` override unless explicitly testing an
alternative; rely on Chroma's default (`all-MiniLM-L6-v2`).

```python
import chromadb

CHROMA_PATH = "data/chroma"  # configurable via env var or config.py, not hardcoded
COLLECTION_NAME = "contract_chunks"

def get_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(path=CHROMA_PATH)
```

**Persistence:** `PersistentClient` writing to `data/chroma/`, not an in-memory client.
The on-disk path supports local debugging between runs (inspecting collection contents
without re-embedding). This does not conflict with "rebuilt at startup" in the Streamlit
deployment context — the rebuild step clears and re-populates the collection idempotently
(Section 5) rather than relying on persisted state surviving between deploys. On Streamlit
Community Cloud's ephemeral filesystem, the on-disk path simply becomes a same-process
scratch location that gets rebuilt every cold start; locally, it persists across runs so you
are not re-embedding ~100 documents every time you test a query.

---

## 3. Chunking Strategy

**Method:** Fixed-size character chunking with overlap, applied uniformly regardless of
document length or type.

- **Chunk size:** 700 characters
- **Overlap:** 100 characters (~14%)
- **Splitter:** simple sliding window over the cleaned text — no semantic or sentence-boundary
  splitting required for PoC scope. Splitting mid-sentence is acceptable; the overlap window
  mitigates context loss at boundaries.

```python
def chunk_text(text: str, chunk_size: int = 700, overlap: int = 100) -> list[str]:
    """
    Split text into overlapping fixed-size chunks.

    Args:
        text:       Full document text (from pdf_parser output).
        chunk_size: Characters per chunk.
        overlap:    Characters of overlap between consecutive chunks.

    Returns:
        List of chunk strings, in original document order. Empty list if text is empty
        or shorter than... actually, short documents still produce one chunk (the whole
        text) — see note below.
    """
```

**Short-document handling:** A document shorter than `chunk_size` (true for most renewal
letters and award letters at median ~2 pages) produces exactly one chunk containing the full
text. The sliding window naturally handles this — no special-case branch needed: if
`len(text) <= chunk_size`, the loop runs once and returns `[text]`.

**Page break separator:** The `'\n\n--- PAGE BREAK ---\n\n'` marker inserted by the parser
(Task 2.1) is left in place during chunking — do not strip it before splitting. It is
low-frequency relative to chunk size and does not need special handling; treating it as
ordinary text is simpler and avoids introducing a second chunking code path. If it lands
inside a chunk, it serves as a mild visual cue when inspecting chunks during debugging.

---

## 4. Chunk Metadata

Each chunk is stored with the following metadata fields, joined from the SQLite `contracts`
table by `source_filename`:

| Field | Source | Purpose |
|-------|--------|---------|
| `source_filename` | parser output | Primary join key back to SQLite; identifies origin document |
| `contract_number` | SQLite (joined) | Links chunk to a contract family; enables filtering RAG results to a specific contract |
| `doc_type` | SQLite (joined) | Enables metadata-filtered queries (e.g., "search only fully executed agreements") |
| `vendor_name` | SQLite (joined) | Enables vendor-scoped semantic queries |
| `doc_date` | SQLite (joined) | Supports time-aware filtering if needed downstream |
| `extraction_method` | SQLite (joined) | Surfaces text-vs-vision provenance; useful for retrieval debugging and for explaining coverage gaps during the walkthrough |
| `chunk_index` | computed at chunk time | Position of this chunk within the document (0-indexed) |

**Join handling:** If a `source_filename` has no matching row in the `contracts` table
(e.g., the document failed extraction validation but parsed fine — see Section 5), the chunk
is still embedded, but the SQLite-sourced metadata fields (`contract_number`, `doc_type`,
`vendor_name`, `doc_date`, `extraction_method`) are set to `"unknown"` rather than omitted.
ChromaDB metadata values must be consistently typed across the collection — using a sentinel
string rather than `None` avoids mixed-type filtering issues. Log a warning when this occurs
so unjoined chunks are visible in the build log, not silently present.

ChromaDB requires a unique `id` string per entry. Use:

```python
chunk_id = f"{source_filename}::chunk_{chunk_index}"
```

This is deterministic and human-readable, which helps when inspecting collection contents
during debugging (you can tell at a glance which document and position a chunk came from).

---

## 5. Source of Truth for Chunking Input

**Decision: chunk from `parse_directory()` output directly (Task 2.1), independent of
extraction success/failure.** Do not gate vector store ingestion on `extraction_status ==
"success"`.

**Rationale:** The vector store and the structured table serve different purposes and can
legitimately diverge. A document that parsed cleanly but failed extraction validation (e.g.,
missing `contract_number`) still has perfectly good text — its content is searchable even if
it could not be cleanly mapped into the structured schema. Gating on extraction success would
silently drop searchable text for no benefit; RAG retrieval doesn't require the structured
fields to function, only the metadata join described in Section 4 (which degrades gracefully
to `"unknown"` rather than failing).

**Scanned documents are skipped, not embedded — by design, not by accident.** Per the vision
extraction ADR, scanned documents produce `text = ""` from the parser. This module:

- Checks `is_scanned` and/or `len(text.strip()) == 0` for every document before chunking
- Skips documents meeting either condition — does **not** attempt to chunk or embed an empty
  string
- Logs every skipped filename at `WARNING` level, and includes the full list in the build
  summary report (Section 7)
- Cross-checks the skipped list against `outputs/parse_results.csv`'s `is_scanned` column as
  a sanity check — if a document is skipped here but `parse_results.csv` does not mark it
  scanned, log this discrepancy explicitly (it indicates a doc with very short but non-empty
  text, which is a different and worth-flagging case)

This is the accepted, documented limitation from the ADR: scanned documents remain fully
present in the structured table (via vision extraction) but have zero presence in the
semantic search layer. This module enforces that boundary cleanly rather than working around
it.

---

## 6. Build Function

```python
def build_index(
    parse_results: list[dict],
    db_path: str = "data/contracts.db",
    chroma_path: str = CHROMA_PATH,
) -> dict:
    """
    Build (or rebuild) the ChromaDB vector store from parsed document text.

    Args:
        parse_results: List of parse result dicts from pdf_parser.parse_directory().
        db_path:       Path to the SQLite database, for metadata join.
        chroma_path:   Path to the ChromaDB persistent store.

    Behavior:
        - Idempotent: clears the existing collection (if present) before rebuilding,
          so repeated calls do not accumulate duplicate or stale entries.
        - Skips documents with is_scanned=True or empty text (Section 5).
        - Joins each remaining document's SQLite row by source_filename for metadata
          (Section 4); unjoined documents get "unknown" sentinel metadata.
        - Chunks, embeds (via Chroma's default embedding function), and upserts all
          chunks in batches.

    Returns:
        Summary dict: {
            "documents_processed": int,
            "documents_skipped_scanned": int,
            "documents_skipped_empty": int,
            "documents_unjoined": int,
            "total_chunks_created": int,
            "collection_name": str,
        }

    Writes outputs/vector_store_build_summary.txt (see Section 7).
    """
```

**Idempotent rebuild:** Before inserting, delete and recreate the collection
(`client.delete_collection(name)` wrapped in a try/except for the not-yet-exists case, then
`client.create_collection(name)`). This is what "rebuilt at startup" means operationally —
a full clear-and-rebuild, not an incremental diff. At ~100 documents this completes in
seconds and avoids any staleness or duplicate-id bugs from partial reruns.

**Batching:** ChromaDB's `collection.add()` accepts lists of ids, documents, and metadatas
in a single call. Accumulate all chunks across all documents into one set of lists and call
`add()` once (or in batches of a few hundred if the corpus grows), rather than calling
`add()` once per chunk — this avoids unnecessary per-call overhead.

---

## 7. Build Summary Report

After `build_index()` completes, write a summary to `outputs/vector_store_build_summary.txt`
and log it at `INFO` level, following the same pattern as the parse and extraction summary
reports:

```
=== Vector Store Build Summary ===
Run timestamp:        2024-01-15 11:02:10
Source:               parse_directory() output (99 documents)
Chroma path:           data/chroma
Collection name:       contract_chunks

--- Document Processing ---
  Embedded:                  83  (83.8%)
  Skipped (scanned):         15  (15.2%)
  Skipped (empty text):       1   (1.0%)
  Unjoined (no SQLite row):   3   (3.0%)  ← embedded, but metadata fields set to "unknown"

--- Chunk Volume ---
  Total chunks created:    412
  Avg chunks per document: 4.96
  Min chunks (1 doc):       1
  Max chunks (1 doc):      47

--- Skipped: Scanned Documents ---
  redacted_agreement_22001.pdf
  old_contract_scan_18334.pdf
  [list all]

--- Unjoined Documents (embedded with "unknown" metadata) ---
  contract_renewal_22001.pdf  — no matching row in contracts table
  [list all]

Collection ready for queries: 412 chunks across 83 documents.
```

```python
def print_build_summary(summary: dict, skipped_scanned: list[str],
                         skipped_empty: list[str], unjoined: list[str],
                         output_path: str) -> None:
    """
    Print and write the vector store build summary report.
    """
```

---

## 8. Query Function

```python
def query_index(
    query_text: str,
    n_results: int = 5,
    where: dict | None = None,
    chroma_path: str = CHROMA_PATH,
) -> list[dict]:
    """
    Run a semantic query against the vector store.

    Args:
        query_text: Natural language query string.
        n_results:  Number of chunks to return. Fixed top-k, no similarity score
                    threshold — always returns up to n_results chunks if the
                    collection is non-empty, even if relevance is weak. (PoC scope
                    decision — see Section 10.)
        where:      Optional ChromaDB metadata filter dict, e.g. {"doc_type": "fully_executed_agreement"}
                    or {"vendor_name": "Acme Corp"}. Passed through to Chroma's query().
        chroma_path: Path to the ChromaDB persistent store.

    Returns:
        List of dicts, one per retrieved chunk, each containing:
            "chunk_text"       (str)
            "distance"         (float) — similarity distance from Chroma (lower = closer)
            "source_filename"  (str)
            "contract_number"  (str)
            "doc_type"         (str)
            "vendor_name"      (str)
            "doc_date"         (str)
            "extraction_method" (str)
            "chunk_index"      (int)

    Returns an empty list if the collection has zero chunks (e.g., build_index() has
    not been run yet) — callers (Task 2.7) must handle this as a "no context available"
    case, not as an exception.
    """
```

This is the function Task 2.7's chat router calls when a query is classified as semantic
(vs. structured/SQL). It is also the function used directly from the CLI test entry point
(Section 9) to validate retrieval quality before any UI exists.

---

## 9. CLI Test Entry Point

To support local testing before Task 2.7 exists, expose a simple CLI mode:

```python
def main():
    """
    CLI entry point for building and querying the vector store standalone.

    Usage:
        python -m src.build_vector_store --build
        python -m src.build_vector_store --query "which contracts have auto-renewal clauses?"
        python -m src.build_vector_store --query "termination terms" --filter doc_type=fully_executed_agreement
    """
```

`--build` runs `parse_directory()` against the configured source directory, then
`build_index()`, then prints the summary. `--query` runs `query_index()` against the
existing persisted collection and prints each result's `chunk_text` (truncated to ~200
chars for console readability), `source_filename`, `contract_number`, and `distance`. This
mirrors the role `test_single_document()` plays for Task 2.3 — a fast manual-inspection loop
without standing up the full app.

---

## 10. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Chunking method | Fixed-size (700 char) with 100-char overlap, uniform across all doc types | Standard, simple, well-understood baseline; avoids building page-aware or semantic-boundary logic for a PoC at ~100 documents; overlap mitigates split-sentence context loss |
| Short-document handling | No special case — sliding window naturally yields one chunk for short docs | Avoids a second code path; the common case (median 2-page renewal letters) falls out of the same loop as the long-document case |
| Metadata fields on chunks | `source_filename`, `contract_number`, `doc_type`, `vendor_name`, `doc_date`, `extraction_method`, `chunk_index` | Supports both the join back to the structured table and metadata-filtered semantic queries (e.g., scoped to one doc_type or vendor); `extraction_method` makes scanned-doc RAG gaps debuggable |
| Collection structure | Single collection (`contract_chunks`) for all chunks | Simplest; metadata filters handle narrowing at query time; splitting by doc_type adds query-routing complexity with no real benefit at this corpus size |
| Module coupling | Standalone module (`build_vector_store.py`) with `build_index()` / `query_index()`, not embedded in Streamlit app | Enables local build-and-query testing before Task 2.7 exists; consistent with the project's separate-modules-for-separate-concerns pattern; Streamlit's startup hook becomes a thin caller of this module, not the owner of the logic |
| Persistence | `PersistentClient` writing to `data/chroma/`, rebuilt (clear + re-embed) at startup | Disk path supports local debugging between runs without re-embedding every time; idempotent rebuild avoids relying on persisted state surviving Streamlit Community Cloud's ephemeral filesystem between deploys |
| Source of truth for chunking input | `parse_directory()` output directly, independent of `extraction_status` | RAG and the structured table can legitimately diverge; a doc with good text but failed extraction validation is still worth searching; gating on extraction success would silently drop searchable content for no benefit |
| Scanned document handling | Skip — do not chunk or embed empty text; log every skip | Honors the existing ADR decision (scanned docs have `text = ""`, no OCR fallback); re-running vision extraction to backfill a summary field for RAG was considered and rejected — costly (~$5/run) for a feature outside original scope |
| Retrieval parameters | Fixed top-k (default 5), no similarity score threshold | Simplest; always returns results if the collection is non-empty; score-threshold filtering (to support a cleaner "no relevant context" signal) is deferred — Task 2.8 will need to apply its own relevance judgment downstream of this call, e.g. via distance inspection or a judge step, rather than this module silently dropping results |
| Embeddings provider | Local `sentence-transformers` via ChromaDB default (`all-MiniLM-L6-v2`) | Already decided in Task 2.3 — no second API provider, no additional credentials, zero cost; sufficient for PoC-scale semantic search over ~100 documents |

---

## 11. Interaction with Task 2.8 (Error Handling)

This module surfaces two conditions that Task 2.8 must handle gracefully downstream, neither
of which this module treats as an error:

1. **Empty collection** — `query_index()` called before `build_index()` has ever run, or
   against a corpus where every document was scanned. Returns `[]`. Not an exception.
2. **Weak-relevance results** — because there is no score threshold (Section 10), every query
   returns up to `n_results` chunks regardless of true relevance. A query about a topic with
   no real presence in the corpus will still get back the *closest available* chunks, which
   may be poor matches. Task 2.8's "no relevant context" failure mode must be implemented by
   inspecting the returned `distance` values or by a downstream relevance check (e.g., a quick
   LLM judgment on the retrieved chunks) — it cannot rely on this module returning an empty
   list for irrelevant queries, because it won't.

This boundary is called out explicitly so it isn't rediscovered as a surprise while building
2.8.

---

## 12. PoC Scope Boundaries (Not In Scope)

The following are explicitly out of scope for the PoC. Document in the README as production
hardening items.

- **OCR backfill for scanned documents.** Scanned docs remain unsearchable via RAG; addressed
  only by the existing structured-table fallback. Adding OCR or a vision-derived summary
  field for embedding is a real option but requires re-running extraction (~$5 per run at
  this corpus size) and was deliberately deferred rather than bundled into this task.
- **Semantic or page-aware chunking.** Fixed-size character chunking only. A production
  version might chunk on sentence or clause boundaries, or respect the page-break marker as
  a hard boundary, to avoid splitting mid-clause.
- **Similarity score thresholding at retrieval time.** Top-k only, no relevance cutoff. See
  Section 11 — this pushes relevance judgment downstream to Task 2.8.
- **Hosted/production embedding provider.** Local `sentence-transformers` only. A production
  deployment serving more users or a larger corpus would likely move to a hosted embedding
  API for quality and latency improvements.
- **Incremental index updates.** The build is always a full clear-and-rebuild. At thousands
  of documents, incremental upsert-on-change would be needed to avoid re-embedding the entire
  corpus on every pipeline run.
- **Per-collection sharding by doc_type or date.** Single collection only, per Section 10.
- **Cross-encoder re-ranking of retrieved chunks.** Raw vector similarity only; no re-ranking
  step after initial retrieval.

---

## 13. Definition of Done

- `src/build_vector_store.py` exists with `build_index()`, `query_index()`, `chunk_text()`,
  and a CLI `main()` entry point
- Running `python -m src.build_vector_store --build` against the parsed corpus produces a
  populated ChromaDB collection at `data/chroma/` and writes
  `outputs/vector_store_build_summary.txt`
- Scanned documents (per `is_scanned` flag) appear in the build summary's skip list and
  contribute zero chunks — verifiable by cross-referencing `outputs/parse_results.csv`
- Running `build_index()` twice in a row produces the same chunk count both times (idempotent
  rebuild — no duplicate accumulation)
- `python -m src.build_vector_store --query "<test query>"` returns up to 5 chunks with
  readable source attribution, runnable with no Streamlit app present
- Each returned chunk includes all seven metadata fields from Section 4, with `"unknown"`
  used (not `None` or a missing key) for any field that could not be joined from SQLite
- A query against an empty or not-yet-built collection returns `[]` rather than raising an
  exception