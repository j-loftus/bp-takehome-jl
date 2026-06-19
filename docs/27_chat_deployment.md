# Task 2.7 — Chat Interface + Deployment: Implementation Requirements

> **Purpose:** Specifies the Streamlit application that is the user-facing layer of the
> contract intelligence pipeline. It exposes two surfaces over the structured SQLite table
> (Task 2.5) and the ChromaDB vector store (Task 2.6): a **canned Insights dashboard** that
> renders the Task 1.4 downstream analyses as charts/tables, and a **natural-language chat**
> that routes each query to either constrained text-to-SQL (structured) or RAG (semantic),
> then renders the result as a visual, not just text. It is the integration task — it owns
> no new extraction or retrieval logic; it composes the modules already built.
>
> **Dependencies:**
> - Task 2.5 (`db_writer.py`) — the `contracts` table schema and the committed `data/contracts.db`.
>   Note: `db_writer.py` is **write-only**; this task reads the DB directly via `pd.read_sql_query`
>   on a **read-only** connection. No read interface is added to `db_writer.py`.
> - Task 2.6 (`build_vector_store.py`) — `build_index(parse_results, db_path, chroma_path)` for
>   startup index build and `query_index(query_text, n_results, where, chroma_path)` for retrieval.
> - Task 2.3 (`llm_client.py`) — the shared `call_llm(prompt, task, max_tokens)` wrapper,
>   `MODEL_CONFIG`, and `LLMCallError`. This task **extends `MODEL_CONFIG`** with new task names
>   (Section 7) and reuses `call_llm` for all LLM calls — it does not create a second client.
> - Task 1.4 (`task_1_4_downstream_analyses.md`) — the authoritative list of the 8 dashboard
>   analyses, each with a prescribed visualization. This task implements that list; it does not
>   redesign the analyses.
> - `extraction_schema.md` — the field definitions and controlled vocabularies (enums) injected
>   into the text-to-SQL prompt so generated SQL uses valid filter values.
>
> **Upstream producers:** the committed `data/contracts.db` (Task 2.5) and `data/parse_cache.json`
> (Section 8.1, produced once from Task 2.1's `parse_directory()` output).
>
> **Downstream consumers:** Task 2.8 (error handling — hardens the relevance/no-context detection
> this task stubs), Task 3.x (eval harness reuses `chat_router` query paths), Phase 5 (walkthrough).

---

## 1. Modules to Produce

The Streamlit app is thin; the logic lives in separate, CLI-testable modules, consistent with the
project's separate-modules-for-separate-concerns pattern (`build_vector_store --query`,
`extractor --test`).

| Module | Responsibility |
|--------|---------------|
| `src/app.py` | Streamlit UI only — layout, navigation, session/transcript state, rendering. Delegates all logic to the modules below. Contains no SQL, no prompt strings, no retrieval calls of its own. |
| `src/chat_router.py` | The chat brain. Intent classification → structured (text-to-SQL) or semantic (RAG) → answer. Owns the text-to-SQL guardrails, the RAG synthesis, the low-confidence gate, and the chat's chart-spec selection. CLI-testable via a `--query` entry point. |
| `src/analyses.py` | The 8 Task 1.4 analyses as pure functions, each returning a pandas DataFrame (plus, where useful, a small derived summary). SQL + pandas only — **no LLM**. Independently testable. |
| `src/viz.py` | A fixed set of chart renderers (the menu) plus a deterministic guard that reconciles a requested chart spec against the realized DataFrame and falls back to a table on any mismatch. Used by both the dashboard (hardcoded specs) and the chat (LLM-proposed specs). |
| `scripts/build_parse_cache.py` | One-time utility: runs `parse_directory()` and writes `data/parse_cache.json` (Section 8.1). Run locally; its output is committed to the repo. Not part of the app's runtime path. |

---

## 2. Application Architecture & Request Flow

Two surfaces, selected via sidebar navigation:

**A. Insights dashboard (deterministic).** Renders the Task 1.4 analyses. Every chart type is
known at design time (we own these analyses), so the dashboard is fully deterministic — no LLM,
no runtime chart inference. This is the reliability anchor of the walkthrough: it always renders,
and it directly satisfies the explicit spec requirement to "perform and visualize basic data cuts."

**B. Natural-language chat.** Per-query flow:

```
user query
   │
   ▼
[1] intent classification (chat_router.classify_intent)   ── call_llm(task="router")
   │
   ├── "structured" ──► [2a] text-to-SQL  (chat_router.answer_structured)
   │                         - generate {sql, chart_type, x, y, series, title}  ── call_llm(task="sql")
   │                         - validate SQL (SELECT-only, single stmt, LIMIT)
   │                         - execute on read-only connection → DataFrame
   │                         - viz.render(df, chart_spec)  (guarded; user can override)
   │                         - show generated SQL in an expander
   │
   └── "semantic"  ──► [2b] RAG  (chat_router.answer_semantic)
                             - query_index(query, n_results=5)
                             - low-confidence gate on distances (Section 5.4)
                             - synthesize grounded answer  ── call_llm(task="synthesis")
                             - render answer + sources list (filename, contract_number)
```

Routing is **two-way only** (structured vs. semantic). Hybrid queries that need both an aggregate
and clause text (e.g. "termination terms for our three biggest contracts") are out of scope for the
PoC and listed as roadmap (Section 12).

---

## 3. Startup Sequence & Caching

On process start (cold start on Community Cloud; first run locally), in this order:

1. **Resolve the API key before importing the router.** `llm_client.py` raises `EnvironmentError`
   at **import time** if `ANTHROPIC_API_KEY` is unset (Task 2.3 §3.1). On Streamlit the key lives in
   `st.secrets`, not the environment. Therefore `app.py` must, at the very top, copy it into the
   environment **before** importing `chat_router` (which imports `llm_client`):

   ```python
   import os, streamlit as st
   if "ANTHROPIC_API_KEY" in st.secrets:
       os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
   # only now import modules that import llm_client
   from chat_router import classify_intent, answer_structured, answer_semantic
   ```

   If the key is absent in both `st.secrets` and the environment, render a clear setup message and
   stop — do not let the import-time exception crash the app with a stack trace.

2. **Build the vector store once per process.** Wrap the build in `@st.cache_resource` so it runs
   exactly once per container lifetime, not per rerun or per query:

   ```python
   @st.cache_resource(show_spinner="Building the search index… (first load only)")
   def _ensure_index():
       parse_results = json.load(open("data/parse_cache.json"))
       summary = build_index(parse_results, db_path="data/contracts.db", chroma_path="data/chroma")
       return summary
   ```

   This is the operational meaning of "rebuilt at startup" from Task 2.6: a full clear-and-rebuild
   from the committed `parse_cache.json`, joined to the committed `contracts.db` for metadata. Only
   the embedding step runs at cold start — no PDF re-parsing (Section 8.1). Note the cold-start cost:
   Chroma's default embedding function downloads its MiniLM model on first use, then embeds
   ~400 chunks on CPU; budget tens of seconds on Community Cloud's free tier. It happens once, behind
   the spinner.

3. **DB access is per-query, read-only.** Do not hold a long-lived SQLite connection across reruns
   (Streamlit's execution model makes a single shared connection fragile). Open a fresh read-only
   connection per query and close it:

   ```python
   def read_sql(sql: str) -> pd.DataFrame:
       con = sqlite3.connect("file:data/contracts.db?mode=ro", uri=True)
       try:
           return pd.read_sql_query(sql, con)
       finally:
           con.close()
   ```

   The `mode=ro` URI is a hard backstop: even if SQL validation (Section 5.2) were bypassed, the
   connection physically cannot write. Cache dashboard DataFrames with `@st.cache_data` so repeated
   dashboard renders don't re-query.

---

## 4. The Insights Dashboard

`analyses.py` implements the 8 analyses from `task_1_4_downstream_analyses.md`. Each function takes
the read accessor (or runs its own read-only query), returns a DataFrame, and the dashboard renders
it with the **prescribed** visualization from Task 1.4 — hardcoded, one renderer per analysis.

| # | Analysis (Task 1.4) | Tier | Prescribed render |
|---|---------------------|------|-------------------|
| 1 | Renewal cliff dashboard | Act now | Bar, sorted by days-to-expiry, colored by `service_category` |
| 2 | Auto-renewal liability scan | Act now | Timeline/table of "act by" dates; flag deadlines within 30 days |
| 3 | Spend concentration map | Understand exposure | Horizontal bar, top 10–20 vendors, % of total annotations |
| 4 | True total commitment by family | Understand exposure | Table: original / amendment / true total; flag >25% over award |
| 5 | Price escalation exposure | Understand exposure | Scatter: value (Y) vs. days-to-renewal (X), colored by escalator type |
| 6 | Procurement channel mix | Improve position | Donut of spend by `procurement_vehicle` + ranked sole-source/coop table |
| 7 | Vendor consolidation map | Improve position | Category table: vendor count, total spend, avg value, fragmentation signal |
| 8 | Incumbent dependency flag | Improve position | Table ranked by relationship age, renewal count, current value |

**MVP vs. fast-follow.** If the time budget tightens, ship analyses **1, 3, 5, 6** first — they are
the most visual (bar / horizontal bar / scatter / donut) and the strongest C-suite story (the
"act now" renewal cliff plus the three exposure/leverage cuts). Analyses 2, 4, 7, 8 are
table-primary and trivially added on the same `analyses.py` pattern. Implement all 8 if time allows;
they are inexpensive once the pattern exists.

**Honesty annotations.** Where Task 1.4 attaches a caveat, surface it in the UI next to the chart
(small caption), not buried: `total_contract_value` is directional where Exhibit A was blank/redacted
(analyses 3, 5); `auto_renewal_flag` is inferred and the scan is a triage list, not an audit
(analysis 2). This mirrors the "proven vs. assumed" framing the assignment rewards.

---

## 5. The Chat Interface

### 5.1 Intent Classification

```python
def classify_intent(query: str) -> str:
    """
    Classify a natural-language query as 'structured' or 'semantic'.

    'structured'  → answerable by aggregation/filtering over the contracts table
                    (counts, sums, rankings, group-bys, date filters).
    'semantic'    → requires reading clause/scope language inside documents
                    (what does X say, summarize the termination terms, etc.).

    Single call_llm(task="router") call. Returns one of the two literal strings;
    defaults to 'semantic' if the response is unparseable (RAG degrades more
    gracefully than a bad SQL attempt).
    """
```

The prompt gives the model the table's column names + the two definitions above and asks for a
one-word answer. Keep it cheap (`router` → Haiku, Section 7).

### 5.2 Structured Path — Text-to-SQL (guarded)

```python
def answer_structured(query: str) -> dict:
    """
    Generate and execute constrained SQL, return result + chart spec.

    Returns:
        {
          "sql": str,                 # the (validated) SQL actually executed
          "dataframe": pd.DataFrame,
          "chart_spec": dict,         # {chart_type, x, y, series, title} — see Section 6
          "error": str | None,       # human-readable; set when generation/exec fails
        }
    """
```

The `call_llm(task="sql")` prompt returns a single JSON object:
`{"sql": ..., "chart_type": ..., "x": ..., "y": ..., "series": ..., "title": ..., "rationale": ...}`.
The prompt **injects the full DDL** from `task_2_5_sqlite_db_setup.md` §3 plus the controlled
vocabularies (the `doc_type`, `service_category`, `procurement_vehicle`, `price_escalator_terms`
enum values) so filters use valid literals. `chart_type` is constrained to the Section 6 menu.

**SQL guardrails — applied before execution, in order:**

1. **Read-only connection** (`mode=ro`, Section 3) — the hard backstop; no write can occur regardless.
2. **Single statement** — strip; reject if more than one statement (more than one non-trailing `;`).
3. **SELECT-only** — must begin with `SELECT` or `WITH` (case-insensitive after trimming).
4. **Keyword denylist** — reject if it contains any of `INSERT UPDATE DELETE DROP ALTER CREATE
   REPLACE ATTACH PRAGMA` (word-boundary match).
5. **LIMIT enforcement** — if no `LIMIT` is present, wrap or append `LIMIT 1000` to bound result size.
6. **Single optional retry** — on an execution error (e.g. a hallucinated column), feed the error
   message back to `call_llm(task="sql")` once for a corrected query. If it still fails, return a
   friendly `error` ("I couldn't turn that into a valid query against the contract data") and the
   attempted SQL.

**Transparency.** Always render the executed SQL in a collapsed `st.expander("Show query")`. For a
skeptical, non-technical PE audience this is the trust mechanism — every number traces to an
inspectable query.

### 5.3 Semantic Path — RAG + Grounded Synthesis

```python
def answer_semantic(query: str, n_results: int = 5) -> dict:
    """
    Retrieve chunks and synthesize a grounded, cited answer.

    Returns:
        {
          "answer": str,             # grounded answer, or the low-confidence message
          "sources": list[dict],     # [{source_filename, contract_number, vendor_name}, ...]
          "low_confidence": bool,    # True when the distance gate tripped (Section 5.4)
          "error": str | None,
        }
    """
```

Calls `query_index(query, n_results=5)` (Task 2.6). The synthesis `call_llm(task="synthesis")`
prompt instructs: answer **only** from the supplied chunks; cite each claim's source by
`source_filename` / `contract_number`; if the chunks do not contain the answer, say so explicitly
rather than guessing. Render the answer plus a compact sources list beneath it.

### 5.4 Low-Confidence / No-Context Gate (and the 2.8 boundary)

Per Task 2.6 §10–11, `query_index()` has **no** similarity threshold — it always returns up to
`n_results` chunks even when nothing relevant exists. So "no relevant context" cannot be detected by
an empty list. This task adds a **simple placeholder gate**: inspect the returned `distance` values;
if the best (minimum) distance exceeds a configurable cutoff, set `low_confidence=True` and return a
message ("I couldn't find a confident match in the contracts for that — here's the closest I found")
instead of a confident synthesized answer.

- The cutoff is a single configurable constant; **tune it empirically** with a few known-irrelevant
  queries during testing and document the chosen value. It is acknowledged as a blunt placeholder.
- **Do not modify `query_index`'s contract** — Task 2.6's decision to keep retrieval threshold-free
  and push relevance judgment downstream stands. The gate lives in `chat_router`.
- **Task 2.8 hardens this.** The robust relevance check (distance calibration or an LLM relevance
  judgment over the retrieved chunks) is 2.8's, because the same logic feeds the eval harness's
  retrieval-precision metric (Task 3.x). This task ships the honest-but-simple version so the chat
  doesn't answer confidently from weak chunks in the gap before 2.8 lands. Claude Code may refine
  exactly where each guard lives against the full codebase; the requirement is only that the chat
  never crashes and never presents a confident answer over clearly weak retrieval.

Also handled here (app cannot run without them): an **empty collection** (`query_index` returns `[]`)
→ "the search index isn't available"; any exception from `query_index`/`call_llm` → caught, friendly
message, no stack trace.

### 5.5 Conversation State

**Stateless per query.** The visible transcript is kept in `st.session_state` and re-rendered each
run (user sees the running Q&A and charts), but **routing, SQL generation, and synthesis use only
the current query** — prior turns are not fed back in. A follow-up like "now just the ones over $1M"
will be treated as a fresh, underspecified query and may answer poorly.

This is a deliberate PoC simplification for demo reliability and clean defensibility (every answer
traces to one inspectable query; no stale-context failures). It must be called out prominently:

- **README** — listed as a known limitation **and the top roadmap item**.
- **In-app** — seed the chat with strong, self-contained example questions (chips/buttons) so users
  naturally ask answerable questions; e.g. *"Which contracts have auto-renewal clauses?"*,
  *"Top 10 vendors by total contract value"*, *"What are the termination terms for contract 23159?"*,
  *"Which contracts expire in the next 180 days?"*

The bounded multi-turn upgrade (pass only the last turn's question + the filters/columns it used) is
the roadmap design and should be named as such in the README.

---

## 6. Visualization Module (`viz.py`)

A **fixed menu** of renderers and a **deterministic guard**. The LLM (chat path) or the dashboard
(hardcoded) only ever names a `chart_type` from this menu; `viz.py` owns every actual plotting call.

| `chart_type` | Renderer | Expected shape |
|--------------|----------|----------------|
| `bar` | vertical bar | 1 categorical + 1 numeric |
| `grouped_bar` | grouped/colored bar | 1 categorical + 1 numeric + 1 series |
| `line` | line | 1 ordered/date axis + 1 numeric |
| `scatter` | scatter | 2 numeric (+ optional categorical color) |
| `metric` | single big number | 1 row, 1 value (`st.metric`) |
| `table` | rendered dataframe | anything (`st.dataframe`) |

```python
def render(df: pd.DataFrame, chart_spec: dict) -> None:
    """
    Render df according to chart_spec = {chart_type, x, y, series, title}.
    Applies the guard (below), then calls the matching renderer.
    """
```

**Deterministic guard — reconcile the requested spec against the realized DataFrame before rendering:**

- `df` is empty → render an explicit "no results" message, not a chart.
- `df` has exactly one row/value → force `metric` (or `table`), regardless of requested type.
- requested `x`/`y`/`series` names a column **not** in `df` → fall back to `table`.
- `bar`/`grouped_bar` with more than ~25 categories → fall back to `table` (unreadable as a chart).
- `scatter` whose `x` or `y` is non-numeric → fall back to `bar` if a sensible categorical+numeric
  pair exists, else `table`.

This is what makes "let the LLM pick the best chart" safe: the model supplies intent, the realized
data plus the guard decide what actually renders, and `table` is always a valid terminal fallback.

**User override.** Every chat result exposes a small chart-type selector; changing it re-renders the
same DataFrame through `viz.render` with the user's chosen type. Natural-language overrides
("show that as a line chart") are handled by the chat as a normal new turn.

Use **Plotly** for the dashboard's colored/annotated charts (colored bars, category-colored scatter,
donut); Streamlit-native charts are insufficient for those. Add `plotly` to requirements.

---

## 7. Model Configuration Additions

Extend the existing `MODEL_CONFIG` in `llm_client.py` (Task 2.3 §2) — do not create a parallel
config. Add the three task names this module uses:

```python
MODEL_CONFIG = {
    # ... existing: classification, extraction, judge ...
    "router":    "claude-haiku-4-5",   # cheap one-word intent classification
    "sql":       "claude-sonnet-4-5",  # text-to-SQL; correctness matters
    "synthesis": "claude-sonnet-4-5",  # grounded RAG answer with citations
}
```

All chat LLM calls go through `call_llm(prompt, task=...)`; model identity stays out of
`chat_router.py` entirely, consistent with the Task 2.3 pattern.

---

## 8. Deployment

### 8.1 Parse Cache — the key efficiency decision

To rebuild the vector store at startup **without** shipping the ~100 source PDFs (and without
re-parsing them at deploy time), commit a lightweight parsed-text cache.

- `scripts/build_parse_cache.py` runs `parse_directory()` once (locally) and serializes its output
  to `data/parse_cache.json`: a list of dicts with the fields `build_index()` needs —
  `filename`, `text`, `is_scanned`, `page_count`. (`filepath` is dropped; it is local-only and unused
  downstream.)
- `data/parse_cache.json` is **committed** to the repo alongside `data/contracts.db`.
- At startup, `_ensure_index()` (Section 3) loads the cache and calls
  `build_index(parse_results, db_path="data/contracts.db", chroma_path="data/chroma")`. Metadata is
  joined from the committed DB exactly as Task 2.6 specifies. Only embedding runs at cold start.
- Size note: scanned docs contribute empty `text`; text docs are a few KB–tens of KB each, so the
  cache is on the order of a few MB — fine to commit. If it grows uncomfortably large, gzip it and
  decompress on load.

This keeps the raw documents out of the repo (lower overhead, and the pattern generalizes to a portco
that won't want its contracts in a Git repo), while still honoring Task 2.6's "rebuilt at startup,
no separate hosted vector DB" decision.

### 8.2 Streamlit Community Cloud

- **Repo layout (relevant paths):**
  ```
  src/app.py, src/chat_router.py, src/analyses.py, src/viz.py
  src/llm_client.py, src/build_vector_store.py, src/db_writer.py   (existing)
  data/contracts.db          (committed — Task 2.5)
  data/parse_cache.json      (committed — Section 8.1)
  requirements.txt
  .streamlit/secrets.toml    (LOCAL ONLY — gitignored)
  ```
- **`requirements.txt`** (minimum): `streamlit`, `anthropic`, `chromadb`, `pandas`, `plotly`.
  (Chroma's default embedding function pulls its model at runtime; no extra embedding package needed.)
- **Secrets:** set `ANTHROPIC_API_KEY` in the Community Cloud app's Secrets settings (TOML). Locally,
  the same key goes in `.streamlit/secrets.toml`, which must be gitignored. `app.py` bridges
  `st.secrets` → `os.environ` before importing the router (Section 3, step 1).
- **Entry point:** the app's main file is `src/app.py`.
- **Ephemeral filesystem:** `data/chroma/` is written fresh into the container's scratch space on each
  cold start by `_ensure_index()` and discarded when the container recycles — exactly the intended
  behavior. Nothing in `data/chroma/` is committed.

### 8.3 Local Fallback

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # or .streamlit/secrets.toml
pip install -r requirements.txt
streamlit run src/app.py
```

Locally, `data/chroma/` persists between runs (Task 2.6 `PersistentClient`), so the index build is
paid once, not every launch. The committed `contracts.db` and `parse_cache.json` mean a fresh clone
runs with no pipeline execution required. Document both the hosted URL and these local steps in the
README (the assignment requires "hosted or clear local-run instructions").

### 8.4 Production Notes (for README / walkthrough)

- **Persist the vector store separately.** In production the index would live in a managed/persistent
  vector DB (e.g. a hosted Chroma, pgvector, or a managed service), built by the pipeline and queried
  by the app — not rebuilt on every cold start. The startup-rebuild pattern is a PoC convenience that
  trades cold-start latency for zero standing infra.
- **Hosted embeddings** for quality/latency at scale (Task 2.6 §12).
- **Auth + per-user data scoping** — none in the PoC; production needs SSO and row-level access so a
  user sees only the contracts they're entitled to (ties to the confidentiality requirement, Task 5.3).
- **Secrets management** beyond Streamlit secrets (a vault/KMS) for real credentials.

---

## 9. Error Handling & Graceful Failures (this task's scope)

The app must never present a stack trace to a non-technical user. Handle, at minimum:

| Condition | Behavior |
|-----------|----------|
| `ANTHROPIC_API_KEY` missing | Clear setup message at startup; stop before the import-time crash. |
| Vector index empty / not built | "Search isn't available right now"; dashboard (SQL-only) still works. |
| `query_index` / `call_llm` raises | Caught; friendly message; transcript and app stay alive. |
| SQL generation invalid / non-SELECT | One retry, then friendly "couldn't build a valid query" + show attempt. |
| SQL executes but returns no rows | "No contracts matched that" — not an empty chart. |
| Retrieval weak (distance over cutoff) | Low-confidence message instead of a confident answer (Section 5.4). |

The **deeper** relevance detection and malformed-document handling are Task 2.8's; this task ships the
app-critical subset above plus the simple low-confidence gate. (See Section 5.4 on the 2.7/2.8 seam.)

---

## 10. MS Teams Integration — Scoped, Not Implemented

**Decision: deprioritized; design-only.** Teams is a "strong plus, not required" in the assignment.
It is not being built for the PoC — no Teams tenant is available to develop against, and the core
pipeline + UI take priority under the time budget. This section documents what it would be and the
level of effort, so the decision is a deliberate, costed trade-off rather than an omission.

**Why it's cheap *relative to a rewrite*:** Teams cannot call a Streamlit app — it needs an HTTP
backend. Because all chat logic already lives in `chat_router.py` (not in `app.py`), the only new
core piece is a **thin FastAPI wrapper** exposing `classify_intent` / `answer_structured` /
`answer_semantic` as endpoints. The Teams bot becomes another front-end over the same backend; the
Streamlit app and the Teams bot share one brain. This decoupling is the payoff of the module split.

**Target architecture:**
- **FastAPI service** wrapping `chat_router` (the shared backend; also future-proofs other channels).
- **Bot Framework / Teams app** — a message extension (search/command) that forwards a user's NL
  question to the FastAPI backend and returns the answer as an adaptive card (text + a rendered
  chart image or a link back to the Streamlit deep-link).
- **Incoming webhook** for proactive alerts — e.g. a scheduled job posts the renewal-cliff "act now"
  list to a procurement channel weekly. This is arguably the highest-value Teams feature: push, not
  pull.
- **Teams SSO → backend** for per-user identity and data scoping in production.

**Level of effort (rough, design-stage estimate):**

| Workstream | Effort |
|-----------|--------|
| Azure Bot registration + Teams app manifest + dev tunnel setup | ~0.5 day |
| FastAPI wrapper around `chat_router` + deploy | ~0.5–1 day |
| Bot Framework message extension wired to the backend (adaptive cards) | ~1–1.5 days |
| Incoming-webhook proactive alert (renewal cliff) + scheduler | ~0.5 day |
| SSO, per-user scoping, testing in a real tenant | ~1–1.5 days |

- **Internal pilot:** ~1 week of focused dev (message extension + FastAPI wrapper + a proactive
  alert), assuming a tenant and bot-registration access.
- **Productionized:** ~2–3 weeks including Teams SSO, per-user data access control, adaptive-card
  polish, and compliance review for sensitive-document handling.

These are planning estimates to be refined against the actual tenant/admin constraints, not commitments.

---

## 11. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Surfaces | Dashboard **and** chat | Dashboard guarantees the "visualize basic data cuts" requirement renders reliably and answers the "last-mile / right interface" prompt; chat shows the GenAI capability. The two together cover the most surface area for non-technical deal-team use. |
| Query routing | LLM intent classifier → text-to-SQL OR RAG; two-way | Sufficient for a PoC; hybrid queries deferred. Defaults to semantic on ambiguity (RAG degrades more gracefully than bad SQL). |
| Text-to-SQL safety | Read-only connection + SELECT-only validation + LIMIT + one retry + SQL shown | Read-only `mode=ro` is the hard backstop; showing the SQL is the trust mechanism for a skeptical audience; the validator catches the common failure modes without a full SQL parser. |
| Chart selection (chat) | LLM proposes `chart_type` + column roles from a **fixed menu**; deterministic guard reconciles against the realized DataFrame; `table` always available; user can override | "LLM picks the best chart" without betting render reliability on model-emitted plotting code. |
| Chart selection (dashboard) | Hardcoded per analysis | We own the Task 1.4 analyses; their charts are known at design time. Deterministic = demo-safe. |
| Vector store at startup | Rebuild from committed `parse_cache.json` + committed `contracts.db`, via `@st.cache_resource`, once per container | Honors Task 2.6's "rebuilt at startup, no hosted vector DB" while keeping raw PDFs out of the repo and skipping re-parse — only embedding runs cold. |
| DB access | Per-query read-only connection; `db_writer` stays write-only | Streamlit's rerun model makes a shared connection fragile; `mode=ro` enforces read-only at the driver level; no read API bolted onto the writer. |
| Conversation state | Stateless per query; visible transcript only | Demo reliability + clean defensibility; flagged as the top roadmap limitation; mitigated with self-contained example questions. |
| Relevance gate | Simple distance cutoff in `chat_router`; `query_index` unchanged | Keeps Task 2.6 settled; ships an honest no-confident-match behavior now; Task 2.8 hardens it (shared with eval retrieval-precision). |
| Model config | Extend `MODEL_CONFIG` with `router`/`sql`/`synthesis` | One config for all model identity; reuses the Task 2.3 client; switching models is one line. |
| MS Teams | Design-only; FastAPI wrapper + Bot Framework + webhook; LOE documented | "Strong plus, not required"; no tenant available; the module split makes it a wrapper, not a rewrite — the trade-off is deliberate and costed. |

---

## 12. PoC Scope Boundaries (Not In Scope)

Document these in the README as production-hardening / roadmap items.

- **Multi-turn / stateful chat** — *top roadmap item.* Bounded design: pass the last turn's question +
  its filters/columns into the router.
- **Hybrid queries** (SQL aggregate + clause retrieval in one answer) — two-way routing only for PoC.
- **Hardened relevance detection** (Task 2.8) — the simple distance gate here is a placeholder.
- **Auth and per-user data scoping** — no access control in the PoC (Task 5.3 covers the production view).
- **Persistent/managed vector store** — startup-rebuild only; production would host it.
- **MS Teams** — design and LOE only (Section 10).
- **Streaming chat responses** — answers render after completion.
- **Caching of chat answers / query result memoization beyond the dashboard** — only dashboard
  DataFrames are cached.

---

## 13. Definition of Done

- `src/app.py`, `src/chat_router.py`, `src/analyses.py`, `src/viz.py`, and
  `scripts/build_parse_cache.py` exist; `app.py` contains no SQL, prompt strings, or retrieval calls.
- `python scripts/build_parse_cache.py` writes `data/parse_cache.json`; the file is committed.
- `streamlit run src/app.py` starts locally with only `data/contracts.db` and `data/parse_cache.json`
  present (no pipeline run required), builds the index once, and serves both surfaces.
- **Dashboard:** at least analyses 1, 3, 5, 6 render with their prescribed charts and their Task 1.4
  honesty captions; all 8 implemented if time allows.
- **Chat — structured:** a query like *"Top 10 vendors by total contract value"* routes to SQL,
  executes on a read-only connection, renders a bar chart, and shows the generated SQL in an expander.
- **Chat — semantic:** a query like *"What are the termination terms for contract 23159?"* routes to
  RAG, returns a grounded answer with a sources list (filename + contract number).
- **Guardrails:** an attempt to make the model emit non-SELECT SQL is blocked (validation and/or the
  read-only connection); a query naming a non-existent column is caught and retried/handled, not crashed.
- **Chart guard:** a single-row result renders as a metric/table, not a degenerate chart; a >25-category
  result falls back to a table; a chart spec naming a missing column falls back to a table.
- **Graceful failures:** missing API key, empty/unbuilt index, no-result queries, and weak-retrieval
  queries each produce a friendly message — no stack trace reaches the user.
- **`MODEL_CONFIG`** contains `router`, `sql`, `synthesis`; all chat LLM calls go through `call_llm`.
- **README inputs ready:** stateless chat is listed as the top roadmap limitation; the MS Teams
  decision + architecture + LOE are documented; production notes (persist vector store, auth) captured.
- `chat_router` is runnable from the CLI (`--query "..."`) without Streamlit, mirroring the
  `build_vector_store --query` / `extractor --test` pattern.