"""
ChromaDB semantic retriever — thin re-export from src.build_vector_store.

All logic lives in src/build_vector_store.py; this module exists so that any
future caller using the src.vector_store.retriever import path keeps working.
"""

from src.build_vector_store import query_index as retrieve  # noqa: F401

__all__ = ["retrieve"]
