import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH: str = os.environ.get("DB_PATH", "data/contracts.db")

_SCHEMA_PATH = Path(__file__).parent / "db" / "schema.sql"

# Columns written by write_extraction_result, in INSERT order.
# pipeline_run_timestamp is injected here; id is autoincrement (excluded).
_INSERT_COLUMNS = [
    "source_filename",
    "pipeline_run_timestamp",
    "contract_number",
    "doc_type",
    "vendor_name",
    "doc_date",
    "county_department",
    "total_contract_value",
    "price_escalator_terms",
    "modification_financial_delta",
    "contract_start_date",
    "contract_end_date",
    "renewal_options",
    "auto_renewal_flag",
    "termination_notice_days",
    "service_category",
    "procurement_vehicle",
    "insurance_requirements_flag",
    "parent_contract_number",
    "extraction_confidence",
    "extraction_notes",
    "extraction_method",
]

_INSERT_SQL = (
    f"INSERT INTO contracts ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(['?'] * len(_INSERT_COLUMNS))})"
)


def initialize_db(db_path: str) -> None:
    ddl = _SCHEMA_PATH.read_text()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(ddl)
        conn.commit()
    logger.info("Database initialized at %s", db_path)


def write_extraction_result(result: dict, db_path: str = DEFAULT_DB_PATH) -> bool:
    if result.get("extraction_status") != "success":
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {**result, "pipeline_run_timestamp": timestamp}

    try:
        values = tuple(row.get(col) for col in _INSERT_COLUMNS)
        with sqlite3.connect(db_path) as conn:
            conn.execute(_INSERT_SQL, values)
            conn.commit()
        return True
    except Exception:
        logger.exception(
            "Failed to write extraction result for %s", result.get("source_filename")
        )
        return False
