"""
Streamlit app — Contract Intelligence Platform.

Two surfaces:
  1. Insights Dashboard — deterministic, hardcoded analyses from Task 1.4.
  2. Natural-Language Chat — stateless per query; routes to SQL or RAG.

This file is UI only. No SQL, no prompt strings, no retrieval calls.
All logic delegates to: chat_router, analyses, viz.

Run:
    streamlit run src/app.py
"""

import json
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# STEP 1: Resolve ANTHROPIC_API_KEY into os.environ BEFORE importing any
# module that imports llm_client (llm_client raises EnvironmentError at
# import time if ANTHROPIC_API_KEY is unset). Local dev reads .env; hosted
# Community Cloud reads st.secrets.
# ---------------------------------------------------------------------------
load_dotenv()  # local fallback — no-op if .env doesn't exist

try:
    if "ANTHROPIC_API_KEY" in st.secrets:
        os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    pass  # st.secrets not configured — rely on .env / env var already set

if "ANTHROPIC_API_KEY" not in os.environ:
    st.error(
        "**ANTHROPIC_API_KEY is not set.**\n\n"
        "Add it to `.streamlit/secrets.toml`:\n"
        "```toml\nANTHROPIC_API_KEY = \"sk-ant-...\"\n```\n"
        "or export it in your shell before running."
    )
    st.stop()

# Ensure src/ is on the path so sibling imports resolve regardless of cwd.
_SRC = Path(__file__).parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Now safe to import modules that pull in llm_client.
import plotly.express as px

from analyses import (
    DB_PATH,
    analysis_1,
    analysis_2,
    analysis_3,
    analysis_4,
    analysis_5,
    analysis_6,
    analysis_7,
    analysis_8,
)
from build_vector_store import build_index
from chat_router import CHROMA_PATH, answer_semantic, answer_structured, classify_intent
from viz import CHART_TYPES, humanize_columns, render

# ---------------------------------------------------------------------------
# Chat rate limiting — process-global, not per-session, so it can't be bypassed
# by opening a new browser tab/incognito window. Protects API spend on a public
# deployment; resets on every app restart/redeploy. Only the chat surface calls
# the LLM, so only chat submissions are gated — the dashboard is pure SQL/pandas.
# Deliberately not in chat_router.py so CLI testing (--query) stays unrestricted.
# ---------------------------------------------------------------------------

_RATE_LIMIT_MAX_REQUESTS = 10
_RATE_LIMIT_WINDOW_SECONDS = 3600
_rate_limit_lock = threading.Lock()
_rate_limit_timestamps: deque = deque()


def _check_rate_limit() -> tuple[bool, int]:
    """
    Returns (allowed, seconds_until_next_slot_frees_up).

    Thread-safe sliding-window counter shared across all sessions hitting this
    process. If allowed, the call is recorded immediately (callers should only
    call this once per submission, right before doing the actual LLM work).
    """
    now = time.time()
    with _rate_limit_lock:
        while _rate_limit_timestamps and now - _rate_limit_timestamps[0] > _RATE_LIMIT_WINDOW_SECONDS:
            _rate_limit_timestamps.popleft()

        if len(_rate_limit_timestamps) < _RATE_LIMIT_MAX_REQUESTS:
            _rate_limit_timestamps.append(now)
            return True, 0

        retry_after = int(_RATE_LIMIT_WINDOW_SECONDS - (now - _rate_limit_timestamps[0]))
        return False, max(retry_after, 1)


# ---------------------------------------------------------------------------
# STEP 2: Build vector index once per container (cold start only).
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Building the search index… (first load only)")
def _ensure_index():
    cache_path = "data/parse_cache.json"
    if not Path(cache_path).exists():
        return {"error": "data/parse_cache.json not found. Run scripts/build_parse_cache.py first."}
    try:
        with open(cache_path) as f:
            parse_results = json.load(f)
        summary = build_index(parse_results, db_path=DB_PATH, chroma_path=CHROMA_PATH)
        return summary
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# STEP 3: Cache dashboard DataFrames — recomputed once per process.
# ---------------------------------------------------------------------------

@st.cache_data
def _load_analysis_1():
    return analysis_1()

@st.cache_data
def _load_analysis_2():
    return analysis_2()

@st.cache_data
def _load_analysis_3():
    return analysis_3()

@st.cache_data
def _load_analysis_4():
    return analysis_4()

@st.cache_data
def _load_analysis_5():
    return analysis_5()

@st.cache_data
def _load_analysis_6():
    return analysis_6()

@st.cache_data
def _load_analysis_7():
    return analysis_7()

@st.cache_data
def _load_analysis_8():
    return analysis_8()


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Procurement Management Tool",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("Procurement Management Tool")
    st.caption("Port Co Procurement Analysis")
    st.divider()
    surface = st.radio(
        "Navigate",
        ["Structured Insights", "Ask a Question"],
        label_visibility="collapsed",
    )

# Trigger index build (happens once; subsequent calls are cached no-ops).
index_status = _ensure_index()
if isinstance(index_status, dict) and index_status.get("error"):
    st.sidebar.warning(f"Search index: {index_status['error']}")
    _index_available = False
else:
    _index_available = True
    with st.sidebar.expander("Search index status"):
        st.caption(f"cwd: `{os.getcwd()}`")
        st.caption(f"chroma path: `{os.path.abspath(CHROMA_PATH)}`")
        st.caption(f"db path: `{os.path.abspath(DB_PATH)}`")
        if isinstance(index_status, dict):
            st.json(index_status)

# ---------------------------------------------------------------------------
# SURFACE A: Insights Dashboard
# ---------------------------------------------------------------------------

if surface == "Structured Insights":
    st.header("Structured Insights")
    st.text(
        "Eight ready-made views into your contract portfolio — renewal risk, vendor spend, "
        "and negotiation leverage, calculated directly from the underlying data. "
        "Browse the tabs below to see where the portfolio needs attention first."
    )

    tabs = st.tabs([
        "1 · Renewal Cliff",
        "2 · Auto-Renewal",
        "3 · Spend Concentration",
        "4 · True Commitment",
        "5 · Price Escalation",
        "6 · Procurement Mix",
        "7 · Consolidation",
        "8 · Incumbents",
    ])

    # --- Analysis 1: Renewal Cliff ---
    with tabs[0]:
        st.subheader("Renewal Cliff Dashboard")
        st.caption("Contracts expiring in the next 365 days, sorted by urgency.")
        df1 = _load_analysis_1()
        render(df1, {
            "chart_type": "bar",
            "x": "days_to_expiry",
            "y": "total_contract_value",
            "series": "service_category",
            "title": "Contracts Expiring Within 365 Days (by Days to Expiry)",
        })
        if not df1.empty:
            st.dataframe(
                humanize_columns(df1[["vendor_name", "contract_number", "contract_end_date",
                      "days_to_expiry", "total_contract_value", "renewal_options"]]),
                use_container_width=True,
            )

    # --- Analysis 2: Auto-Renewal Liability ---
    with tabs[1]:
        st.subheader("Auto-Renewal Liability Scan")
        st.caption(
            ":warning: `auto_renewal_flag` is inferred by the LLM, not a direct text extraction. "
            "This list is a triage aid — verify before acting."
        )
        df2 = _load_analysis_2()
        if df2.empty:
            st.info("No contracts with auto-renewal detected.")
        else:
            urgent = df2[df2["urgent"] == True]
            if not urgent.empty:
                st.error(f"**{len(urgent)} contract(s) require action within 30 days.**")
                st.dataframe(humanize_columns(urgent), use_container_width=True)
            st.dataframe(humanize_columns(df2), use_container_width=True)

    # --- Analysis 3: Spend Concentration ---
    with tabs[2]:
        st.subheader("Spend Concentration Map")
        st.caption(
            ":information_source: `total_contract_value` is directional where Exhibit A was blank "
            "or redacted. Numbers are best-available, not audited totals."
        )
        df3 = _load_analysis_3()
        render(df3, {
            "chart_type": "bar",
            "x": "vendor_name",
            "y": "total_contract_value",
            "series": None,
            "title": "Top Vendors by Total Contract Value (Fully Executed Agreements)",
        })
        if not df3.empty:
            st.dataframe(humanize_columns(df3), use_container_width=True)

    # --- Analysis 4: True Total Commitment ---
    with tabs[3]:
        st.subheader("True Total Commitment by Contract Family")
        st.caption("Original award + all amendment deltas. Rows flagged where amendments exceed 25% of original.")
        df4 = _load_analysis_4()
        render(df4, {"chart_type": "table", "x": None, "y": None, "series": None,
                     "title": "Contract Families — Original vs. Amendment vs. True Total"})

    # --- Analysis 5: Price Escalation ---
    with tabs[4]:
        st.subheader("Price Escalation Exposure")
        st.caption(
            ":information_source: `total_contract_value` is directional where Exhibit A was blank "
            "or redacted. Escalator type is LLM-inferred from clause language."
        )
        df5 = _load_analysis_5()
        render(df5, {
            "chart_type": "scatter",
            "x": "days_to_renewal",
            "y": "total_contract_value",
            "series": "price_escalator_terms",
            "title": "Price Escalation Risk — Value vs. Days to Renewal",
        })
        if not df5.empty:
            st.dataframe(humanize_columns(df5), use_container_width=True)

    # --- Analysis 6: Procurement Channel Mix ---
    with tabs[5]:
        st.subheader("Procurement Channel Mix")
        channel_df, detail_df = _load_analysis_6()
        if not channel_df.empty:
            col_chart, col_table = st.columns([1, 1])
            with col_chart:
                fig = px.pie(
                    channel_df,
                    names="procurement_vehicle",
                    values="total_contract_value",
                    hole=0.4,
                    title="Spend by Procurement Vehicle",
                    labels={"procurement_vehicle": "Procurement Vehicle",
                            "total_contract_value": "Total Contract Value ($)"},
                )
                fig.update_traces(
                    textinfo="label+percent",
                    hovertemplate="%{label}: $%{value:,.0f} (%{percent})<extra></extra>",
                )
                st.plotly_chart(fig, use_container_width=True)
            with col_table:
                st.caption("Channel summary")
                st.dataframe(humanize_columns(channel_df), use_container_width=True)
        else:
            st.info("No procurement data available.")
        if not detail_df.empty:
            st.caption("Sole-source and cooperative contracts — highest-priority re-bid candidates")
            st.dataframe(humanize_columns(detail_df), use_container_width=True)

    # --- Analysis 7: Vendor Consolidation ---
    with tabs[6]:
        st.subheader("Vendor Consolidation Opportunity Map")
        df7 = _load_analysis_7()
        render(df7, {"chart_type": "table", "x": None, "y": None, "series": None,
                     "title": "Fragmentation by Service Category"})

    # --- Analysis 8: Incumbent Dependency ---
    with tabs[7]:
        st.subheader("Incumbent Dependency Flag")
        df8 = _load_analysis_8()
        render(df8, {"chart_type": "table", "x": None, "y": None, "series": None,
                     "title": "Vendors Ranked by Renewal Count (3+ = stale incumbent)"})


# ---------------------------------------------------------------------------
# SURFACE B: Natural-Language Chat
# ---------------------------------------------------------------------------

else:
    st.header("Ask a Question")
    st.markdown(
        "Ask anything about the contract portfolio in plain English, for example:\n"
        "- **Aggregates** — \"Top 10 vendors by spend\"\n"
        "- **Filters** — \"Which contracts expire in the next 90 days?\"\n"
        "- **Contract language** — \"What are the payment terms for contract 21019?\"\n\n"
        "**Each question is answered on its own**, with no memory of earlier questions in this "
        "session — so include everything the question needs (vendor name, contract number, time "
        "window, etc.) right in your question rather than referring back to a prior answer."
    )

    if not _index_available:
        st.warning(
            "The semantic search index is unavailable. "
            "Document questions will not work. SQL questions still will."
        )

    # Example query chips — 3 across the top row, 2 centered below
    st.markdown("**Example questions to try:**")
    examples = [
        "Top 10 vendors by total contract value",
        "Which contracts expire in the next 180 days?",
        "Which contracts have renewal options?",
        "What are the billing rates for contract 21019?",
        "What services does Journal Technologies provide under contract 24313?",
    ]

    def _example_button(col, example: str) -> None:
        with col:
            if st.button(example, use_container_width=True):
                st.session_state.setdefault("pending_query", example)

    for col, example in zip(st.columns(3), examples[:3]):
        _example_button(col, example)

    _, col_a, col_b, _ = st.columns([1, 3, 3, 1])
    for col, example in zip([col_a, col_b], examples[3:]):
        _example_button(col, example)

    st.divider()

    # Session transcript
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Render existing transcript (text only; charts are rendered inline on the live turn)
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Handle example chip click
    prefill = st.session_state.pop("pending_query", None)

    # Chat input
    user_input = st.chat_input("Ask about your contracts…") or prefill

    if user_input:
        # Show user message
        with st.chat_message("user"):
            st.markdown(user_input)
        st.session_state.messages.append({"role": "user", "content": user_input, "result": None})

        allowed, retry_after = _check_rate_limit()
        if not allowed:
            minutes = max(retry_after // 60, 1)
            with st.chat_message("assistant"):
                st.warning(
                    f"This demo allows {_RATE_LIMIT_MAX_REQUESTS} questions per hour to keep API costs "
                    f"in check. Please try again in about {minutes} minute(s)."
                )
            st.session_state.messages.append({
                "role": "assistant",
                "content": "Rate limit reached for this hour — please try again shortly.",
                "result": None,
            })
            st.stop()

        # Route and answer
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                try:
                    intent = classify_intent(user_input)
                except Exception:
                    intent = "semantic"

            if intent == "structured":
                with st.spinner("Running query…"):
                    result = answer_structured(user_input)

                if result["error"]:
                    st.warning(result["error"])
                    if result["sql"]:
                        with st.expander("Show attempted query"):
                            st.code(result["sql"], language="sql")
                    msg_text = result["error"]
                else:
                    df = result["dataframe"]
                    chart_spec = result["chart_spec"]

                    if df.empty:
                        st.info("No contracts matched that query.")
                    else:
                        # User chart-type override
                        selected_chart = st.selectbox(
                            "Chart type",
                            CHART_TYPES,
                            index=CHART_TYPES.index(chart_spec.get("chart_type", "table"))
                            if chart_spec.get("chart_type") in CHART_TYPES else 0,
                            key=f"chart_override_{len(st.session_state.messages)}",
                            label_visibility="collapsed",
                        )
                        override_spec = {**chart_spec, "chart_type": selected_chart}
                        render(df, override_spec)

                    with st.expander("Show query"):
                        st.code(result["sql"], language="sql")

                    row_word = "row" if len(df) == 1 else "rows"
                    msg_text = f"Found {len(df)} {row_word}. (SQL query above)"

                st.session_state.messages.append({
                    "role": "assistant",
                    "content": msg_text,
                    "result": None,
                })

            else:  # semantic
                if not _index_available:
                    st.warning("Semantic search is unavailable. Try rephrasing as a data question.")
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": "Semantic search is unavailable right now.",
                        "result": None,
                    })
                else:
                    with st.spinner("Searching documents…"):
                        result = answer_semantic(user_input)

                    if result["error"]:
                        st.warning(result["error"])
                    elif result["low_confidence"]:
                        st.warning("Low confidence — no strong match found in the contracts.")
                        st.markdown(result["answer"])
                    else:
                        st.markdown(result["answer"])

                    if result["sources"]:
                        with st.expander(f"Sources ({len(result['sources'])})"):
                            for s in result["sources"]:
                                st.caption(
                                    f"**{s['vendor_name']}** · Contract {s['contract_number']} · "
                                    f"`{s['source_filename']}`"
                                )

                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": result["answer"],
                        "result": None,
                    })
