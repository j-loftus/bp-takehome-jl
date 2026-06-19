-- Contract intelligence pipeline — SQLite schema
-- Source of truth: docs/extraction_schema.md §7
-- One row per document; contract_number is the linking key across a contract family.

CREATE TABLE IF NOT EXISTS contracts (
    -- Row identity
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_filename              TEXT NOT NULL,
    pipeline_run_timestamp       TEXT NOT NULL,          -- ISO 8601 datetime

    -- Group A: Universal Spine
    contract_number              TEXT NOT NULL,
    doc_type                     TEXT NOT NULL CHECK (doc_type IN (
                                     'fully_executed_agreement',
                                     'renewal_letter',
                                     'modification_amendment',
                                     'award_letter',
                                     'vendor_disclosure_statement',
                                     'other'
                                 )),
    vendor_name                  TEXT NOT NULL,
    doc_date                     TEXT,                   -- ISO 8601 date YYYY-MM-DD
    county_department            TEXT,

    -- Group B: Financial Exposure
    total_contract_value         REAL,
    price_escalator_terms        TEXT CHECK (price_escalator_terms IN (
                                     'fixed',
                                     'cpi_capped',
                                     'fixed_percentage',
                                     'negotiated_at_renewal',
                                     'not_specified',
                                     NULL
                                 )),
    modification_financial_delta REAL,

    -- Group C: Term and Renewal Exposure
    contract_start_date          TEXT,                   -- ISO 8601 date YYYY-MM-DD
    contract_end_date            TEXT,                   -- ISO 8601 date YYYY-MM-DD
    renewal_options              TEXT,
    auto_renewal_flag            INTEGER,                -- 0/1/NULL (SQLite boolean)
    termination_notice_days      INTEGER,

    -- Group D: Vendor and Compliance Risk
    service_category             TEXT CHECK (service_category IN (
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
    procurement_vehicle          TEXT CHECK (procurement_vehicle IN (
                                     'direct_rfp',
                                     'cooperative_piggyback',
                                     'sole_source',
                                     'other',
                                     NULL
                                 )),
    insurance_requirements_flag  INTEGER,                -- 0/1/NULL (SQLite boolean)

    -- Group E: Document Linkage
    parent_contract_number       TEXT,

    -- Pipeline metadata
    extraction_confidence        TEXT CHECK (extraction_confidence IN (
                                     'high', 'medium', 'low', NULL
                                 )),
    extraction_notes             TEXT,                   -- free text for flagged issues
    extraction_method            TEXT CHECK (extraction_method IN ('text', 'vision', NULL))
);

-- Indexes for common query patterns (renewal cliff, spend concentration, etc.)
CREATE INDEX IF NOT EXISTS idx_contract_number   ON contracts(contract_number);
CREATE INDEX IF NOT EXISTS idx_doc_type          ON contracts(doc_type);
CREATE INDEX IF NOT EXISTS idx_vendor_name       ON contracts(vendor_name);
CREATE INDEX IF NOT EXISTS idx_contract_end_date ON contracts(contract_end_date);
CREATE INDEX IF NOT EXISTS idx_service_category  ON contracts(service_category);
