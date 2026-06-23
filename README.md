# Berkshire Partners Take-Home — Contract Intelligence System

## Overview

A contract intelligence platform for a PE portco's procurement function (Lake County's contract
corpus stands in as the example dataset). Source PDFs are parsed and run through an LLM extraction
pipeline into a structured table of 17 fields per document (vendor, value, dates, renewal terms,
procurement vehicle, etc.), then exposed through a Streamlit app with two surfaces: a **Structured
Insights** dashboard of eight deterministic procurement analyses (renewal risk, spend concentration,
escalation exposure, vendor consolidation), and an **Ask a Question** chat that routes natural-language
queries to either generated SQL (for counts/sums/rankings) or retrieval-augmented generation over the
contract text (for clause-level questions). Every chat answer is either a traceable SQL query or a
cited excerpt — no unattributed numbers.

## Architecture

```
contracts/*.pdf
   │
   ▼
parse (PyMuPDF) ──► classify (doc_type) ──► extract (17 fields, LLM) ──► data/contracts.db
   │                                                                          │
   └─ scripts/build_parse_cache.py (one-time) ──► data/parse_cache.json      │
                                                          │                   │
                                                          ▼                   │
                                              build_index() (Chroma, cold start)
                                                          │                   │
                                                          ▼                   ▼
                                                  data/chroma (ephemeral) ──► src/app.py
                                                                                │
                                              ┌─────────────────────────────────┴──────────────┐
                                              ▼                                                 ▼
                                  Structured Insights (analyses.py)                  Ask a Question (chat_router.py)
                                  8 hardcoded SQL+pandas views, no LLM         classify_intent → text-to-SQL | RAG
                                  rendered via viz.py's fixed chart menu       rendered via the same viz.py menu
```

Two extraction passes feed `data/contracts.db`: a `fully_executed_agreement` row carries the richest
fields (value, terms, dates); `renewal_letter`, `modification_amendment`, `award_letter`, and
`vendor_disclosure_statement` rows carry whatever is present in their document type and link back via
`contract_number`. The vector store is rebuilt from `data/parse_cache.json` + `data/contracts.db` on
every cold start (no separate hosted vector DB) — only the embedding step runs at startup, not PDF
parsing.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in ANTHROPIC_API_KEY
```

## Running the Pipeline

```bash
# End-to-end: parse → classify → extract → store
python scripts/run_pipeline.py

# (Re)build vector store from extracted docs (local persistent index)
python scripts/build_vector_store.py

# One-time: parse the sampled PDFs into a committed, portable text cache
# (lets the app rebuild its index on a fresh clone without the source PDFs)
python scripts/build_parse_cache.py
```

## Running the App

```bash
streamlit run src/app.py
```

Locally, the API key is read from `.env` (via `python-dotenv`) or `.streamlit/secrets.toml`, whichever
is present. A fresh clone runs with no pipeline execution required — only `data/contracts.db` and
`data/parse_cache.json` need to be present; the vector index rebuilds once per process on cold start.

**Deploying to Streamlit Community Cloud:** push this repo to GitHub, connect it in Streamlit Cloud's
UI with `src/app.py` as the entry point, and set `ANTHROPIC_API_KEY` under the app's Secrets (TOML).
`data/contracts.db` and `data/parse_cache.json` must be committed (see `.gitignore` — everything else
under `data/` is excluded) since the corpus PDFs themselves are not shipped.

## Running Evals

```bash
pytest tests/

# Generate blank ground-truth skeletons + source-text dumps for labeling
# (no values pre-filled from pipeline output — see docs/evaluation.md §3.4-3.5)
python -m eval.make_labeling_templates --extraction-docs "<file>.pdf:<doc_type>" ...
python -m eval.make_labeling_templates --classification-sample 25 --stratify

# Fill eval/ground_truth_extraction.json, eval/ground_truth_classification.json,
# and eval/chat_cases.json by hand, then run the full eval:
python -m eval.run_eval                       # judge sample defaults to the labeled docs
python -m eval.run_eval --judge-sample 8
python -m eval.run_eval --judge-extra-unlabeled 3

# Judge layer standalone (e.g. to sanity-check before a full run):
python -m eval.judge --sample 8
```

**The written-up results — scorecard, metrics, and the "proven vs. assumed" section — are committed
at [`docs/evaluation_results.md`](docs/evaluation_results.md)** (no eval run required to read them).
Running the command above regenerates the raw artifacts under `outputs/` (`eval_report.md`,
`eval_results.json`, `eval_judge_raw.json`, `eval_token_summary.txt`); those are gitignored and not
shipped. `eval/monitoring.py` exposes `monitoring_snapshot()` + `compare_to_baseline()`, reusing the
same scoring/judge code on a live batch — see docs/evaluation.md §8.

## Key Decisions

| Decision | Choice | Why |
|---|---|---|
| Sampling | ~99 of ~389 source PDFs, stratified by contract family | Full extraction over the corpus wasn't needed to demonstrate the pipeline; sampling by family (not by document) preserves lifecycle completeness (award → agreement → amendments → renewals) for the analyses that need it. |
| RAG indexing scope | Scoped to the same sampled ~99 docs, not the full corpus | Indexing all 389 PDFs would let the chat surface vendors/contracts the structured table and dashboard know nothing about — an inconsistency between the two surfaces during a walkthrough. See `docs/rag_implementation_notes.md`. |
| Vector store at startup | Rebuilt from a committed parse-text cache (`data/parse_cache.json`) + committed `data/contracts.db`, once per process via `st.cache_resource` | Keeps raw PDFs out of the repo (also the pattern a real portco would want — its contracts shouldn't live in Git) while still avoiding a separate hosted vector DB for the PoC. Only embedding runs at cold start, not PDF parsing. |
| Query routing | LLM intent classifier → text-to-SQL **or** RAG, two-way only | Sufficient for a PoC; defaults to semantic (RAG) on ambiguous classification since RAG degrades more gracefully than a bad SQL attempt. Hybrid queries (aggregate + clause text in one answer) are out of scope — see Known Limitations. |
| Text-to-SQL safety | Read-only SQLite connection (`mode=ro`) + SELECT-only validation + keyword denylist + forced `LIMIT` + one retry on execution error + the executed SQL always shown | The read-only connection is the hard backstop — no write can occur regardless of what the model generates. Showing the SQL is the trust mechanism for a skeptical, non-technical audience. |
| Chart selection (chat) | LLM proposes a chart type + column roles from a **fixed menu**; a deterministic guard in `viz.py` reconciles the request against the realized data (empty → message, 1 row → metric, >25 categories → table, missing/wrong-type column → table); user can override | Lets the model "pick the best chart" without betting render reliability on model-generated plotting code — the guard, not the model, decides what actually renders. |
| Chart selection (dashboard) | Hardcoded per analysis, no LLM | The eight analyses and their prescribed visualizations are known at design time; deterministic rendering is the reliability anchor of a live walkthrough. |
| Entity-scoped retrieval | When a chat query names a contract number or vendor that resolves in the SQL DB, retrieval is scoped via a Chroma metadata filter to that vendor's full document family before falling back to unfiltered search | Generic semantic similarity alone under-ranks the right document when query phrasing ("what services does X provide") shares more vocabulary with unrelated chunks than with the target document. An exact DB-verified entity match is a stronger relevance signal than embedding distance, so entity-scoped retrieval also uses a looser low-confidence cutoff. |
| Relevance / low-confidence gate | A simple Chroma L2 distance cutoff in `chat_router.py` (not in `query_index()`, which stays threshold-free by design) | Ships an honest "no confident match" behavior now without touching the retrieval module's contract. **The cutoff values are unvalidated placeholders** (set from a handful of manual spot checks, not a calibrated sweep) — proper calibration is explicitly deferred to the next hardening pass. |
| Model config | One `MODEL_CONFIG` dict shared by the whole pipeline and the chat (`router`→Haiku, `sql`/`synthesis`→Sonnet, plus the existing `classification`/`extraction`/`judge` entries) | Single point of control for model identity; swapping a model is a one-line change, never scattered across call sites. |
| Conversation state | Stateless per query — visible transcript only, no prior turns fed into routing/SQL/synthesis | Deliberate PoC simplification: every answer traces to exactly one inspectable query, with no stale-context failures. This is the top known limitation — see below. |
| MS Teams | Designed, not built (architecture + level-of-effort documented in `docs/27_chat_deployment.md` §10) | "Strong plus, not required" in the assignment; no Teams tenant available to develop against. Because chat logic lives in `chat_router.py` rather than the Streamlit UI, a Teams integration would be a thin FastAPI wrapper over the existing module, not a rewrite. |

## Known Limitations & Next Steps

1. **Stateless chat (top priority).** Each question is answered independently — a follow-up like "now
   just the ones over $1M" is treated as a fresh, underspecified query. The bounded fix: pass the prior
   turn's question plus the filters/columns it resolved into the next turn's router and SQL/synthesis
   prompts, without going to full multi-turn memory.
2. **Relevance gate is an unvalidated placeholder.** The low-confidence distance cutoffs in
   `chat_router.py` were set from a handful of manual spot checks, not a systematic calibration pass.
   A proper version would calibrate against a labeled set of known-relevant/known-irrelevant queries,
   or replace the distance heuristic with an LLM relevance judgment over the retrieved chunks — which
   would also feed a retrieval-precision metric for the eval harness.
3. **Scanned documents reduce RAG coverage for some vendors.** Image-only PDFs (no OCR in this PoC)
   contribute no text to the index. A vendor's most informative document (e.g. its primary executed
   agreement) being scanned means semantic search effectively can't answer questions about that vendor,
   even though it's fully present in the structured table. OCR is the natural follow-up.
4. **Two-way routing only.** Hybrid questions that need both an aggregate and clause text in one answer
   (e.g. "termination terms for our three biggest contracts") aren't supported — the router picks one
   path or the other.
5. **No persistent/managed vector store.** The index is rebuilt from scratch on every cold start, which
   is fine for a PoC but adds tens of seconds of startup latency. Production would host the index
   (managed Chroma, pgvector, or similar) and update it incrementally as documents are added.
6. **No auth or per-user data scoping.** Anyone with the app URL sees the full portfolio. Production
   needs SSO and row-level access so a user sees only the contracts they're entitled to.
7. **MS Teams is design-only.** Architecture and effort estimate are documented; not built, since no
   Teams tenant was available and it wasn't required for the assignment.
8. **No streaming chat responses.** Answers render after the full LLM call completes.
