"""
Streamlit app entrypoint.

Run with: streamlit run src/app/main.py

Provides a chat interface that routes natural language queries to either:
  - Structured SQL queries against the SQLite contracts table, or
  - Semantic RAG queries against the ChromaDB vector store.
Results are rendered as charts or tables, not raw text.
"""

import streamlit as st

# TODO: import query_router, components


def main():
    st.set_page_config(page_title="Contract Intelligence", layout="wide")
    st.title("Contract Intelligence")

    # TODO: initialize DB connection and ChromaDB client (cached with st.cache_resource)
    # TODO: render chat history from st.session_state
    # TODO: handle user input → query_router → render result via components


if __name__ == "__main__":
    main()
