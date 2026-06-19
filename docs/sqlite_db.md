# Task 2.5 — SQLite Database Setup: Implementation Requirements

> **Purpose:** Specifies the SQLite database setup module for the contract intelligence pipeline.
> This module is responsible for initializing the database schema and writing validated extraction
> results to the `contracts` table. It is the final step in the per-document pipeline execution path.
>
> **Dependencies:**
> - `extraction_schema.md` — authoritative source for the DDL (Section 7) and all field definitions
> - `task_2_3_extraction_pipeline.md` — defines `_assemble_result()` output dict; this module consumes that dict directly
> - `adr_scanned_document_vision_extraction.md` — adds `extraction_method` column to the DDL
>
> **Upstream producer:** `src/extractor.py` (Task 2.3) — assembled result dict
> **Downstream consumers:** `src/app.py` (Task 2.7 Streamlit UI), eval harness (Task 3.1)

---

## 1. Module to Produce

This task produces one Python module:

| Module | Responsibility |
|--------|---------------|
| `src/db_writer.py` | Initialize the database schema; write validated extraction results to `contracts` table |

---

## 2. Database File Location and Deployment

- **Default path:** `data/contracts.db`
- **Configurable via:** environment variable or top-level `config.py` — do not hardcode the path inside the module
- **Committed to the repo:** the pre-populated `data/contracts.db` ships with the app. Streamlit Community Cloud has an ephemeral filesystem, so the populated database must exist in the repo at deploy time. Do not rely on running the pipeline at deploy time to create it.
- **SQLite is a file, not a server.** No daemon, no connection string, no credentials. The Streamlit app connects with `sqlite3.connect("data/contracts.db")` — nothing else required. This was the explicit reason SQLite was chosen: lightweight, inspectable, zero infra cost, and the file can be opened directly in any SQLite browser during the walkthrough.

---

## 3. DDL — Final Schema

The DDL below is the authoritative implementation target. It incorporates the base schema from `extraction_schema.md` Section 7 plus the `extraction_method` column added by the vision extraction ADR.

Use `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` throughout — the init function must be idempotent and safe to call at pipeline startup on every run.

```sql
CREATE TABLE IF NOT EXISTS contracts (
    -- Row identity
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_filename             TEXT NOT NULL,
    pipeline_run_timestamp      TEXT NOT NULL,          -- ISO 8601 datetime, UTC, set at write time

    -- Group A: Universal Spine
    contract_number             TEXT NOT NULL,
    doc_type                    TEXT NOT NULL CHECK (doc_type IN (
                                    'fully_executed_agreement',
                                    'renewal_letter',
                                    'modification_amendment',
                                    'award_letter',
                                    'vendor_disclosure_statement',
                                    'other'
                                )),
    vendor_name                 TEXT NOT NULL,
    doc_date                    TEXT,                   -- ISO 8601 date YYYY-MM-DD
    county_department           TEXT,

    -- Group B: Financial Exposure
    total_contract_value        REAL,
    price_escalator_terms       TEXT CHECK (price_escalator_terms IN (
                                    'fixed',
                                    'cpi_capped',
                                    'fixed_percentage',
                                    'negotiated_at_renewal',
                                    'not_specified',
                                    NULL
                                )),
    modification_financial_delta REAL,

    -- Group C: Term and Renewal Exposure
    contract_start_date         TEXT,                   -- ISO 8601 date YYYY-MM-DD
    contract_end_date           TEXT,                   -- ISO 8601 date YYYY-MM-DD
    renewal_options             TEXT,
    auto_renewal_flag           INTEGER,                -- 0/1/NULL (SQLite boolean)
    termination_notice_days     INTEGER,

    -- Group D: Vendor and Compliance Risk
    service_category            TEXT CHECK (service_category IN (
                                    'professional_services',
                                    'technology_software',
                                    'facilities_maintenance',
                                    'public_safety',
                                    'infrastructure',
                                    'staffing',
                                    'supplies_goods',
                                    'behavioral_health',
                                    'other',
                                    NULL
                                )),
    procurement_vehicle         TEXT CHECK (procurement_vehicle IN (
                                    'direct_rfp',
                                    'cooperative_piggyback',
                                    'sole_source',
                                    'other',
                                    NULL
                                )),
    insurance_requirements_flag INTEGER,                -- 0/1/NULL (SQLite boolean)

    -- Group E: Document Linkage
    parent_contract_number      TEXT,

    -- Pipeline metadata
    extraction_confidence       TEXT,                   -- 'high' / 'medium' / 'low'
    extraction_notes            TEXT,                   -- free text for flagged issues
    extraction_method           TEXT                    -- 'text' | 'vision' (per vision extraction ADR)
);

CREATE INDEX IF NOT EXISTS idx_contract_number   ON contracts(contract_number);
CREATE INDEX IF NOT EXISTS idx_doc_type          ON contracts(doc_type);
CREATE INDEX IF NOT EXISTS idx_vendor_name       ON contracts(vendor_name);
CREATE INDEX IF NOT EXISTS idx_contract_end_date ON contracts(contract_end_date);
CREATE INDEX IF NOT EXISTS idx_service_category  ON contracts(service_category);
```

---

## 4. Public Interface — Two Functions Only

### `initialize_db(db_path: str) -> None`

- Creates the database file if it does not exist
- Runs the full DDL above — idempotent on repeat calls
- Logs confirmation on success
- Call this at pipeline startup, before the batch loop begins

### `write_extraction_result(result: dict, db_path: str) -> bool`

- Receives the assembled dict produced by `_assemble_result()` in `extractor.py`
- The dict already has all 19 schema field names as keys (matching column names exactly); no key remapping is done here
- **Adds two fields the extractor does not own:**
  - `pipeline_run_timestamp` — UTC ISO 8601 datetime, set at write time inside this function
  - `id` — auto-assigned by SQLite; do not include in the INSERT statement
- **Skip condition:** if `result["extraction_status"] != "success"`, return `False` and write nothing. Failure and skipped records are not written to the `contracts` table; the pipeline orchestrator handles logging those separately.
- Uses a **parameterized INSERT** — no string formatting of values under any circumstances
- Returns `True` on successful commit
- Returns `False` on any exception — catches the exception, logs it with the filename, and does not re-raise. The calling batch loop checks the return value and accumulates the failure count.

---

## 5. What This Module Does NOT Do

- **No validation.** Input dict validation is the extractor's responsibility. The db_writer trusts the dict it receives.
- **No failure record storage.** The db_writer only writes success rows. Failure records are the pipeline orchestrator's concern.
- **No connection lifecycle management for the batch loop.** Two acceptable patterns for the PoC:
  - **Simple (preferred for PoC):** `write_extraction_result` opens and closes its own connection per call. SQLite handles this fine at this scale.
  - **Efficient:** the orchestrator opens a single connection before the batch loop and passes it in as a parameter. Either is acceptable — document whichever you implement.

---

## 6. Cross-Module Callout: `extraction_method` in `extractor.py`

The `extraction_method` column was added to the DDL by the vision extraction ADR **after** the extractor spec (Task 2.3) was written. Confirm that `_assemble_result()` in `extractor.py` includes `"extraction_method"` in its returned dict, set as follows:

```python
"extraction_method": "vision" if is_scanned else "text",
```

`is_scanned` must be passed into `_assemble_result()` as a parameter. If this field is missing from the extractor's output dict, the db_writer will write `NULL` for that column — which is technically acceptable but loses the eval signal. Fix it in `extractor.py` as a two-line change if not already present.

---

## 7. Definition of Done

- `src/db_writer.py` exists with `initialize_db()` and `write_extraction_result()`
- Running `initialize_db("data/contracts.db")` creates the file with the correct schema, verifiable via:
  ```bash
  sqlite3 data/contracts.db ".schema"
  ```
- Passing a valid mock extraction dict with `extraction_status = "success"` writes one row with all columns correctly typed
- Passing a dict with `extraction_status = "failed"` or `"skipped"` returns `False` and writes nothing
- `pipeline_run_timestamp` is populated at write time inside the db_writer (not by the extractor)
- All INSERTs use parameterized queries — no f-strings or `%` formatting used to construct SQL
- `initialize_db()` is safe to call multiple times on an existing database without error or data loss