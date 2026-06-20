"""
Chat router — the brain of the chat interface.

Intent classification → structured (text-to-SQL) or semantic (RAG) → answer.
All LLM calls go through call_llm(); model identity lives in MODEL_CONFIG.

CLI usage:
    python src/chat_router.py --query "Top 10 vendors by total contract value"
    python src/chat_router.py --query "What are the termination terms for contract 23159?" --mode semantic
"""

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

# Resolve src/ on sys.path so this works both when run directly and when
# Streamlit adds src/ via its own path handling.
_SRC_DIR = Path(__file__).parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from build_vector_store import query_index
from llm_client import LLMCallError, call_llm, extract_json

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "data/contracts.db")
CHROMA_PATH = os.environ.get("VECTOR_STORE_DIR", "data/chroma")

# L2 distance cutoffs for the low-confidence gate.
#
# UNVALIDATED PLACEHOLDERS — not statistically calibrated. Set from a handful of
# manual spot checks (a known-irrelevant query and a known-relevant one), not a
# systematic pass over known-good/known-bad queries. Per spec §5.4 this gate is
# explicitly "the honest-but-simple version" pending Task 2.8, which owns proper
# calibration (e.g. an LLM relevance judgment over retrieved chunks). Treat these
# numbers as directionally reasonable, not load-bearing precision.
LOW_CONFIDENCE_CUTOFF = 1.0

# Looser cutoff used when retrieval was scoped to a specific vendor/contract via
# _resolve_entity_filter() (Chroma `where`). That scoping is an independent, exact
# signal from the SQL DB join — not just embedding similarity — so a chunk's distance
# can legitimately run higher than the generic cutoff while still being on-topic.
# Also an unvalidated placeholder (see above) — set just above the 1.08-1.25 range
# observed in one entity-scoped spot check, not a calibrated value.
ENTITY_SCOPED_LOW_CONFIDENCE_CUTOFF = 1.4

# ---------------------------------------------------------------------------
# DDL + controlled vocabularies injected into SQL prompt
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).parent / "db" / "schema.sql"
try:
    _DDL = _SCHEMA_PATH.read_text()
except FileNotFoundError:
    _DDL = "(schema.sql not found)"

_ENUMS = """
Controlled vocabulary values (use exactly these strings in WHERE clauses):

doc_type:
  'fully_executed_agreement', 'renewal_letter', 'modification_amendment',
  'award_letter', 'vendor_disclosure_statement', 'other'

price_escalator_terms:
  'fixed', 'cpi_capped', 'fixed_percentage', 'negotiated_at_renewal', 'not_specified'

service_category:
  'professional_services', 'technology_software', 'facilities_maintenance',
  'public_safety', 'infrastructure', 'staffing', 'supplies_goods',
  'behavioral_health', 'other'

procurement_vehicle:
  'direct_rfp', 'cooperative_piggyback', 'sole_source', 'other'
"""

_CHART_MENU = "bar, grouped_bar, line, scatter, metric, table"

_COLUMNS = """
Columns available in the contracts table:
  id, source_filename, pipeline_run_timestamp,
  contract_number, doc_type, vendor_name, doc_date, county_department,
  total_contract_value, price_escalator_terms, modification_financial_delta,
  contract_start_date, contract_end_date, renewal_options,
  auto_renewal_flag (0/1/NULL), termination_notice_days,
  service_category, procurement_vehicle, insurance_requirements_flag (0/1/NULL),
  parent_contract_number,
  extraction_confidence, extraction_notes, extraction_method
"""

# ---------------------------------------------------------------------------
# SQL guardrail helpers
# ---------------------------------------------------------------------------

_DENYLIST = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|PRAGMA)\b",
    re.IGNORECASE,
)


def _validate_sql(sql: str) -> tuple[str, str | None]:
    """
    Apply guardrails; return (cleaned_sql, error_message_or_None).

    Guardrails (in order):
    1. Strip whitespace; reject multiple statements.
    2. Must begin with SELECT or WITH.
    3. Denylist check.
    4. Enforce LIMIT 1000 if absent.
    """
    sql = sql.strip().rstrip(";")

    # Guard 2: multiple statements
    if ";" in sql:
        return sql, "Multiple SQL statements are not allowed."

    # Guard 3: SELECT-only
    upper = sql.upper().lstrip()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return sql, "Only SELECT queries are permitted."

    # Guard 4: denylist
    if _DENYLIST.search(sql):
        return sql, "Query contains disallowed keywords."

    # Guard 5: enforce LIMIT
    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql = sql + "\nLIMIT 1000"

    return sql, None


_CONTRACT_NUMBER_PATTERN = re.compile(r"\b\d{5}(?:-\d+)?\b")


def _resolve_entity_filter(query: str) -> dict | None:
    """
    If the query names a specific contract_number or vendor_name that exists in the DB,
    build a Chroma `where` filter scoping retrieval to that document family's full closure
    of vendor_name and contract_number values.

    A closure, not a single value, because a document family can carry inconsistent values
    on both axes across its lifecycle: the same contract_number can appear under multiple
    vendor_name strings (e.g. a vendor rebrand — "Global Tel*Link Corporation" renamed to
    "ViaPath Technologies" partway through contract 21019's life), and the same vendor_name
    can span multiple contract_number variants (e.g. an exhibit with its own internal
    reference number distinct from the parent contract_number). Resolving to only one
    matched value and filtering on it alone silently excludes the other half of the family —
    which is worse than no filter, since it actively excludes the right document rather than
    just failing to prioritize it.

    Returns None if nothing resolves, so callers can fall back to unfiltered retrieval.
    """
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            contract_numbers: set[str] = set()
            vendor_names: set[str] = set()

            for candidate in _CONTRACT_NUMBER_PATTERN.findall(query):
                rows = con.execute(
                    "SELECT DISTINCT vendor_name FROM contracts WHERE contract_number = ?", (candidate,)
                ).fetchall()
                if rows:
                    contract_numbers.add(candidate)
                    vendor_names.update(r[0] for r in rows)

            if not vendor_names:
                all_vendors = [r[0] for r in con.execute("SELECT DISTINCT vendor_name FROM contracts").fetchall()]
                query_lower = query.lower()
                matches = [v for v in all_vendors if v and v.lower() in query_lower]
                if matches:
                    vendor_names.add(max(matches, key=len))  # prefer the most specific (longest) match

            if not vendor_names:
                return None

            # Expand the closure both ways for two passes — enough to catch a rebrand
            # (vendor -> contract -> other vendor name) or a split reference number
            # (contract -> vendor -> other contract number) without unbounded recursion.
            for _ in range(2):
                if vendor_names:
                    placeholders = ",".join("?" * len(vendor_names))
                    rows = con.execute(
                        f"SELECT DISTINCT contract_number FROM contracts WHERE vendor_name IN ({placeholders})",
                        list(vendor_names),
                    ).fetchall()
                    contract_numbers.update(r[0] for r in rows)
                if contract_numbers:
                    placeholders = ",".join("?" * len(contract_numbers))
                    rows = con.execute(
                        f"SELECT DISTINCT vendor_name FROM contracts WHERE contract_number IN ({placeholders})",
                        list(contract_numbers),
                    ).fetchall()
                    vendor_names.update(r[0] for r in rows)
        finally:
            con.close()
    except Exception:
        return None

    clauses = []
    if vendor_names:
        clauses.append({"vendor_name": {"$in": sorted(vendor_names)}})
    if contract_numbers:
        clauses.append({"contract_number": {"$in": sorted(contract_numbers)}})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$or": clauses}


def _exec_sql(sql: str) -> tuple[pd.DataFrame, str | None]:
    """Execute validated SQL on a read-only connection. Returns (df, error)."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            df = pd.read_sql_query(sql, con)
            return df, None
        finally:
            con.close()
    except Exception as e:
        return pd.DataFrame(), str(e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_intent(query: str) -> str:
    """
    Classify query as 'structured' or 'semantic'.

    'structured' → answerable by aggregation/filtering over the contracts table.
    'semantic'   → requires reading clause/scope language inside documents.

    Defaults to 'semantic' if the response is unparseable (RAG degrades more
    gracefully than a bad SQL attempt).
    """
    prompt = f"""You are a query router for a contract intelligence system.

A user has asked: "{query}"

Classify this query as either:
- "structured": answerable by counting, summing, ranking, filtering, or grouping rows
  in the contracts table (e.g. "top vendors", "contracts expiring soon", "total spend by category").
- "semantic": requires reading clause or scope language inside the actual contract documents
  (e.g. "what does X say", "summarize the termination terms", "what are the payment conditions").

{_COLUMNS}

Reply with exactly one word: structured or semantic"""

    try:
        response = call_llm(prompt, task="router", max_tokens=10)
        word = response.strip().lower().split()[0] if response.strip() else "semantic"
        return "structured" if word == "structured" else "semantic"
    except (LLMCallError, Exception):
        return "semantic"


def answer_structured(query: str) -> dict:
    """
    Generate and execute constrained SQL; return result + chart spec.

    Returns:
        {
          "sql": str,                 # the (validated) SQL actually executed
          "dataframe": pd.DataFrame,
          "chart_spec": dict,         # {chart_type, x, y, series, title}
          "error": str | None,
        }
    """
    prompt = f"""You are a SQL expert for a contract management database.

Generate a SQLite SELECT query to answer this question: "{query}"

{_DDL}

{_ENUMS}

After writing the SQL, also specify the best visualization for the result using one of these
chart types: {_CHART_MENU}

Respond with ONLY a JSON object (no markdown fences, no explanation):
{{
  "sql": "<your SELECT query>",
  "chart_type": "<one of: {_CHART_MENU}>",
  "x": "<column name for x-axis, or null>",
  "y": "<column name for y-axis, or null>",
  "series": "<column name for color/grouping, or null>",
  "title": "<short descriptive title>",
  "rationale": "<one sentence why this chart type>"
}}

Rules:
- Write only a single SELECT statement (no semicolons except at the very end).
- Use only columns that exist in the contracts table.
- Do not use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE, ATTACH, or PRAGMA.
- Dates are stored as TEXT in ISO 8601 format (YYYY-MM-DD); use julianday() for date math.
- auto_renewal_flag and insurance_requirements_flag are stored as INTEGER (0 or 1).
- For aggregations, always include a descriptive alias (e.g. SUM(...) AS total_value).
- IMPORTANT — avoiding double-counting: a single contract_number can have multiple rows
  (one per document in its lifecycle: award_letter, fully_executed_agreement, renewal_letter,
  modification_amendment, vendor_disclosure_statement, other). Rows from different doc_types for
  the same contract_number often repeat the SAME total_contract_value. This filter applies ONLY
  when the query aggregates total_contract_value with SUM/AVG across multiple vendors, contracts,
  or categories (e.g. "total spend by vendor", "top vendors by value") — in that case, add
  WHERE doc_type = 'fully_executed_agreement' so each contract's value is counted once.
  Do NOT add a doc_type filter for queries that merely filter or list contracts by other
  attributes (e.g. expiration date, renewal flags, service category) without summing
  total_contract_value — those should query across all doc_types unless the user names a
  specific doc_type. Use whatever row has the relevant non-null field regardless of doc_type.
"""

    def _attempt(extra_context: str = "") -> tuple[str, dict]:
        full_prompt = prompt + extra_context
        raw = call_llm(full_prompt, task="sql", max_tokens=1000)
        parsed = extract_json(raw)
        sql_raw = parsed.get("sql", "")
        chart_spec = {
            "chart_type": parsed.get("chart_type", "table"),
            "x": parsed.get("x"),
            "y": parsed.get("y"),
            "series": parsed.get("series"),
            "title": parsed.get("title", query),
        }
        return sql_raw, chart_spec

    # First attempt
    try:
        sql_raw, chart_spec = _attempt()
    except (LLMCallError, Exception) as e:
        return {
            "sql": "",
            "dataframe": pd.DataFrame(),
            "chart_spec": {"chart_type": "table", "x": None, "y": None, "series": None, "title": query},
            "error": f"I couldn't generate a query for that question. ({e})",
        }

    sql_clean, validation_error = _validate_sql(sql_raw)

    if validation_error:
        # Single retry with validation error context
        try:
            sql_raw2, chart_spec = _attempt(
                f"\n\nPrevious attempt failed validation: {validation_error}\n"
                f"Previous SQL: {sql_raw}\nPlease fix it."
            )
            sql_clean, validation_error2 = _validate_sql(sql_raw2)
            if validation_error2:
                return {
                    "sql": sql_clean,
                    "dataframe": pd.DataFrame(),
                    "chart_spec": chart_spec,
                    "error": f"I couldn't turn that into a valid query against the contract data. ({validation_error2})",
                }
        except (LLMCallError, Exception) as e:
            return {
                "sql": sql_clean,
                "dataframe": pd.DataFrame(),
                "chart_spec": chart_spec,
                "error": f"I couldn't turn that into a valid query against the contract data. ({e})",
            }

    df, exec_error = _exec_sql(sql_clean)

    if exec_error:
        # Single retry with execution error context
        try:
            sql_raw2, chart_spec = _attempt(
                f"\n\nPrevious query caused an execution error: {exec_error}\n"
                f"Previous SQL:\n{sql_clean}\n"
                f"Please correct the SQL to fix this error."
            )
            sql_clean2, validation_error2 = _validate_sql(sql_raw2)
            if validation_error2:
                return {
                    "sql": sql_clean,
                    "dataframe": pd.DataFrame(),
                    "chart_spec": chart_spec,
                    "error": f"I couldn't turn that into a valid query against the contract data. ({validation_error2})",
                }
            df2, exec_error2 = _exec_sql(sql_clean2)
            if exec_error2:
                return {
                    "sql": sql_clean,
                    "dataframe": pd.DataFrame(),
                    "chart_spec": chart_spec,
                    "error": f"I couldn't turn that into a valid query against the contract data. ({exec_error2})",
                }
            return {"sql": sql_clean2, "dataframe": df2, "chart_spec": chart_spec, "error": None}
        except (LLMCallError, Exception) as e:
            return {
                "sql": sql_clean,
                "dataframe": pd.DataFrame(),
                "chart_spec": chart_spec,
                "error": f"I couldn't turn that into a valid query against the contract data. ({e})",
            }

    return {"sql": sql_clean, "dataframe": df, "chart_spec": chart_spec, "error": None}


def answer_semantic(query: str, n_results: int = 5) -> dict:
    """
    Retrieve chunks and synthesize a grounded, cited answer.

    Returns:
        {
          "answer": str,
          "sources": list[dict],     # [{source_filename, contract_number, vendor_name}, ...]
          "low_confidence": bool,
          "error": str | None,
        }
    """
    entity_scoped = False
    try:
        entity_filter = _resolve_entity_filter(query)
        chunks = []
        if entity_filter:
            chunks = query_index(query, n_results=n_results, where=entity_filter, chroma_path=CHROMA_PATH)
            entity_scoped = bool(chunks)
        if not chunks:
            # No named entity resolved, or the resolved entity has no indexed chunks
            # (e.g. all its documents are scanned) — fall back to unfiltered retrieval.
            chunks = query_index(query, n_results=n_results, chroma_path=CHROMA_PATH)
    except Exception as e:
        return {
            "answer": "The search index isn't available right now. Please try again.",
            "sources": [],
            "low_confidence": False,
            "error": str(e),
        }

    if not chunks:
        return {
            "answer": "The search index isn't available right now — the collection appears to be empty.",
            "sources": [],
            "low_confidence": False,
            "error": None,
        }

    # Low-confidence gate: check minimum distance. Use a looser cutoff when retrieval
    # was scoped to a vendor/contract resolved from the SQL DB (see ENTITY_SCOPED_LOW_CONFIDENCE_CUTOFF).
    cutoff = ENTITY_SCOPED_LOW_CONFIDENCE_CUTOFF if entity_scoped else LOW_CONFIDENCE_CUTOFF
    min_distance = min(c.get("distance", 0) for c in chunks)
    if min_distance > cutoff:
        closest = chunks[0].get("chunk_text", "")[:300]
        return {
            "answer": (
                "I couldn't find a confident match in the contracts for that question — "
                f"here's the closest I found:\n\n> {closest}"
            ),
            "sources": _extract_sources(chunks[:1]),
            "low_confidence": True,
            "error": None,
        }

    # Build synthesis prompt
    chunk_texts = "\n\n---\n\n".join(
        f"[Source: {c.get('source_filename', 'unknown')} | "
        f"Contract: {c.get('contract_number', 'unknown')} | "
        f"Vendor: {c.get('vendor_name', 'unknown')}]\n{c.get('chunk_text', '')}"
        for c in chunks
    )

    synthesis_prompt = f"""You are a contract analyst. Answer the user's question using ONLY the contract excerpts provided below.

User question: "{query}"

Contract excerpts:
{chunk_texts}

Instructions:
- Answer based solely on the information in the excerpts above.
- Cite each claim by mentioning the source filename or contract number in parentheses.
- If the excerpts do not contain enough information to answer, say "I don't have enough information in the provided excerpts to answer that question."
- Do not guess or infer information not present in the excerpts.
- Be concise and factual.
"""

    try:
        answer = call_llm(synthesis_prompt, task="synthesis", max_tokens=1500)
    except (LLMCallError, Exception) as e:
        return {
            "answer": "I encountered an error while synthesizing an answer. Please try again.",
            "sources": _extract_sources(chunks),
            "low_confidence": False,
            "error": str(e),
        }

    return {
        "answer": answer,
        "sources": _extract_sources(chunks),
        "low_confidence": False,
        "error": None,
    }


def _extract_sources(chunks: list[dict]) -> list[dict]:
    """Deduplicate and extract source metadata from retrieved chunks."""
    seen = set()
    sources = []
    for c in chunks:
        key = (c.get("source_filename"), c.get("contract_number"))
        if key not in seen:
            seen.add(key)
            sources.append({
                "source_filename": c.get("source_filename", "unknown"),
                "contract_number": c.get("contract_number", "unknown"),
                "vendor_name": c.get("vendor_name", "unknown"),
            })
    return sources


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chat router CLI — test without Streamlit")
    parser.add_argument("--query", required=True, help="Natural language query")
    parser.add_argument(
        "--mode",
        choices=["auto", "structured", "semantic"],
        default="auto",
        help="Force routing mode (default: auto-classify)",
    )
    args = parser.parse_args()

    if args.mode == "auto":
        intent = classify_intent(args.query)
        print(f"[router] classified as: {intent}")
    else:
        intent = args.mode
        print(f"[router] mode forced: {intent}")

    if intent == "structured":
        result = answer_structured(args.query)
        print(f"\n[SQL]\n{result['sql']}\n")
        if result["error"]:
            print(f"[error] {result['error']}")
        else:
            print(f"[chart_spec] {result['chart_spec']}")
            print(f"\n[result — {len(result['dataframe'])} rows]")
            print(result["dataframe"].to_string(index=False))
    else:
        result = answer_semantic(args.query)
        if result["error"]:
            print(f"[error] {result['error']}")
        if result["low_confidence"]:
            print("[low confidence]")
        print(f"\n[answer]\n{result['answer']}")
        if result["sources"]:
            print(f"\n[sources]")
            for s in result["sources"]:
                print(f"  - {s['source_filename']} (contract {s['contract_number']}, {s['vendor_name']})")
