"""
ChromaDB indexer — thin re-export from src.build_vector_store.

All logic lives in src/build_vector_store.py; this module exists so that any
future caller using the src.vector_store.indexer import path keeps working.
"""

from src.build_vector_store import build_index  # noqa: F401

__all__ = ["build_index"]
