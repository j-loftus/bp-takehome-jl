"""
Query router.

Classifies an incoming natural language query and dispatches it to either:
  - structured_query(): SQL against SQLite for aggregations and filters
  - rag_query(): semantic search against ChromaDB for document-level questions
"""


def route(query: str, db_conn, chroma_dir: str) -> dict:
    """
    Route a query and return a response dict.

    Returns:
        {
            "type": "structured" | "rag",
            "answer": str,
            "data": pd.DataFrame | list[dict] | None,  # for visualization
        }
    """
    # TODO: use LLM to classify query intent, then dispatch
    raise NotImplementedError
