"""
SQLite database client.

Wraps connection management, table initialization, and query helpers.
The DB path is read from the DB_PATH environment variable (or .env).
"""

import sqlite3
from pathlib import Path


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Return a SQLite connection with row_factory set to sqlite3.Row."""
    # TODO: open connection, set row_factory, run PRAGMA foreign_keys = ON
    raise NotImplementedError


def init_db(db_path: str | Path) -> None:
    """Create tables from schema.sql if they don't exist."""
    # TODO: read src/db/schema.sql and execute against connection
    raise NotImplementedError


def insert_document(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one extracted document row into the documents table."""
    # TODO: parameterized INSERT OR REPLACE
    raise NotImplementedError


def query_documents(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a SELECT and return results as a list of dicts."""
    # TODO: execute, fetchall, return as list of dicts
    raise NotImplementedError
