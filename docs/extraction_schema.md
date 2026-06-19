# Contract Intelligence Pipeline — Schema Requirements Document

> **Purpose:** This document defines the extraction schema for the GenAI contract intelligence pipeline. It specifies every field to be extracted, its definition, data type, nullability, and expected coverage by document type. It also defines the downstream analyses the schema is designed to enable. This document is the authoritative source for pipeline implementation — all extraction prompts, database schema, and evaluation test cases should trace back to it.

---

## 1. Project Framing

### What We Are Building
A generalizable contract intelligence tool that gives any portfolio company's internal operator team — procurement managers, CFOs, COOs — structured visibility into their vendor relationships. The tool is demonstrated on a real-world corpus of ~387 municipal procurement PDFs from Lake County, IL, which serves as a realistic stand-in for the kind of vendor contract archive any mid-market business accumulates.

### What We Are Not Building
This is not a competitive intelligence tool. The corpus is not being used to reverse-engineer a market or analyze competitors. The Lake County documents are the demo dataset; the value proposition is the pipeline capability, which is portable to any portco's own contract archive.

### The Business Owner
The end user is a portco's internal operator team. The "business owner" proving ROI is whoever manages vendor relationships at that portco — typically a procurement lead, CFO, or COO. The tool must produce analyses they can act on without writing a single line of code.

---

## 2. Corpus Overview

| Attribute | Detail |
|-----------|--------|
| Total files | ~387 PDFs |
| Source | Lake County, IL — Purchasing Division |
| Date range | Approximately 2015–2026; heaviest concentration 2022–2026 |
| Linking key | Contract number — appears in document headers, letterheads, and body text |
| Estimated contract families | 60–100+ distinct contracts; each family contains 2–8+ documents |
| Sampling target | ~100 documents (~20–25 complete contract families) |
| Sampling unit | Complete contract families (not individual documents) |

---

## 3. Document Taxonomy

Six document types exist in the corpus. The classifier must assign one of these types to every document **before** extraction runs. Doc type drives which fields the extractor is instructed to populate.

| Doc Type | Enum Value | Est. Share | Page Count | Business Signal |
|----------|-----------|------------|------------|-----------------|
| Fully Executed Agreement | `fully_executed_agreement` | ~22% | 10–20+ pages | Highest |
| Renewal Letter | `renewal_letter` | ~27% | Always 2 pages | Medium |
| Modification / Amendment | `modification_amendment` | ~13% | 2–5 pages | Medium-high |
| Award / Intent-to-Award Letter | `award_letter` | ~11% | 1–3 pages | Low to high |
| Vendor Disclosure Statement | `vendor_disclosure_statement` | ~12% | Always 1 page | Very low |
| Other | `other` | ~15% | Varies | Varies widely |

### Critical Classification Notes

- **Renewal letters:** Page 2 of every renewal letter PDF is a blank VDS form. The classifier must key off page 1 content only. The blank page 2 VDS must not be extracted as a separate document or confuse the classifier.
- **Award letters have two subtypes:** Simple award letter (1 page, no pricing) and intent-to-award with bid tab (2–3 pages, full unit price schedule). Both map to `award_letter`; the extractor handles both subtypes.
- **Modifications are delta documents:** They reference but do not repeat full original agreement terms. Extraction captures what changed, not the full contract state.
- **VDS forms:** Near-zero analytical value. Extract minimally — only the universal spine fields plus `parent_contract_number` if present.
- **Other:** Catch-all for price increase letters, task orders, SOW documents, 60-day extensions, bid documents, cooperative procurement references, and redacted files. Extract what is present; expect high null rates.

---

## 4. Schema Design Principles

1. **One row per document, not per contract.** `contract_number` is the grouping key. Contract-family-level analysis is performed by joining or aggregating on `contract_number` in queries, not by collapsing rows at ingestion time.
2. **ROI-linkage required.** Every field must connect to at least one named downstream analysis. Fields that are informative but not actionable are excluded.
3. **Universal spine + doc-type-specific fields.** Five fields populate on every row regardless of doc type. Remaining fields are populated only where the document type carries that information — nulls in those cells are by design, not pipeline failures.
4. **Structured values over free text.** Where a field's value drives SQL-level filtering or aggregation, it must be normalized (enum, boolean, integer, float, date). Raw clause text belongs in the vector store, not the structured table.
5. **Inferred vs. extracted fields are explicitly distinguished.** Fields where the LLM infers rather than directly extracts (e.g. `service_category`, `auto_renewal_flag`) are flagged as such in this document and must be evaluated separately in the eval harness.

---

## 5. Extraction Schema — Full Field Definitions

### Group A — Universal Spine
*These 5 fields populate on every row regardless of document type. All are non-nullable. Pipeline failures on any Group A field should be flagged and the document queued for retry or manual review.*

---

#### `contract_number`
- **Type:** `STRING`
- **Nullable:** No — if not extractable, flag the document and do not write the row
- **Definition:** The unique identifier assigned to this contract by the issuing organization. The primary key for joining documents within a contract family. Appears in document headers, letterheads, subject lines, and body text. In multi-vendor contracts (where one contract number covers multiple vendors), each document row carries the shared contract number plus its own `vendor_name`.
- **Extraction note:** Extract exactly as it appears. Do not normalize or reformat. If multiple candidate numbers appear, prefer the one in the document header or letterhead.
- **Example values:** `23159`, `22847-A`, `C-2024-0312`

---

#### `doc_type`
- **Type:** `ENUM`
- **Nullable:** No — set by classifier before extraction; defaults to `other` if classifier cannot determine
- **Definition:** The document type assigned by the classifier. Drives which fields the extraction prompt is instructed to populate.
- **Allowed values:** `fully_executed_agreement` | `renewal_letter` | `modification_amendment` | `award_letter` | `vendor_disclosure_statement` | `other`

---

#### `vendor_name`
- **Type:** `STRING`
- **Nullable:** No
- **Definition:** The name of the external party contracting with the issuing organization (i.e., not Lake County / not the portco itself). Normalize to the legal entity name where determinable. In multi-vendor families, each document carries its own vendor name — do not attempt to concatenate.
- **Example values:** `Johnson Controls Inc.`, `Actalent Services LLC`, `Axon Enterprise Inc.`

---

#### `doc_date`
- **Type:** `DATE` (ISO 8601: YYYY-MM-DD)
- **Nullable:** No — use best available date signal from the document
- **Definition:** The document's own date. Semantics vary by doc type:
  - Fully Executed Agreement → execution/signing date
  - Renewal Letter → letter date (date the letter was issued)
  - Modification/Amendment → effective date of the modification
  - Award Letter → award date
  - VDS → signature date
- **Extraction note:** Do not use file metadata or pipeline run timestamps. Extract the date as it appears in the document text.

---

#### `county_department`
- **Type:** `STRING`
- **Nullable:** Yes — not always explicit in the document
- **Definition:** The Lake County department or division this contract serves. Maps to "business unit" in a portco context and enables department-level spend analysis. May appear in scope descriptions, letterheads, or routing language.
- **Example values:** `Sheriff's Office`, `Lake County Health Department`, `Division of Transportation`, `Facilities and Construction Services`
- **Extraction note:** Extract verbatim if present. Do not infer from vendor name or subject matter alone. Null if not determinable.

---

### Group B — Financial Exposure
*Drives spend concentration analysis and price escalation risk quantification. Highest downstream ROI.*

---

#### `total_contract_value`
- **Type:** `FLOAT` (USD)
- **Nullable:** Yes
- **Definition:** The total dollar value of the contract as stated in the document. For agreements with rate structures (hourly, per-unit), extract the total not-to-exceed value if stated. For award letters with bid tabs, extract the total awarded value. For modifications, this field is null — use `modification_financial_delta` instead.
- **Extractable from:** Fully Executed Agreement (unreliable when Exhibit A is blank/redacted), Award Letter with Bid Tab (reliable), Other (price sheets, task orders — where present)
- **Extraction note:** Extract numeric value only, strip currency symbols and commas. Null if not stated. Flag documents where Exhibit A is present but blank.
- **Example values:** `125000.00`, `48500.00`, `2340000.00`

---

#### `price_escalator_terms`
- **Type:** `ENUM`
- **Nullable:** Yes
- **Definition:** How the contract price may change over its term. The LLM extracts the relevant clause language and normalizes it to one of the allowed values.
- **Allowed values:**
  - `fixed` — price does not change over the contract term
  - `cpi_capped` — increases allowed but capped at CPI or a named index
  - `fixed_percentage` — increases at a specified fixed annual percentage (e.g. 3% per year)
  - `negotiated_at_renewal` — pricing renegotiated at each renewal; no escalator in base term
  - `not_specified` — contract does not address price escalation
- **Extractable from:** Fully Executed Agreement (primary), Modification/Amendment (when amendment addresses pricing)
- **Extraction note:** This is a normalized inference from clause language, not a direct extraction. If the clause language is ambiguous, prefer `not_specified` over guessing. The raw clause text is available in the vector store.

---

#### `modification_financial_delta`
- **Type:** `FLOAT` (USD)
- **Nullable:** Yes — null on all doc types except `modification_amendment`
- **Definition:** The net dollar change to contract value introduced by this specific amendment. Positive values increase contract value; negative values decrease it. Null if the modification does not involve a financial change (e.g. scope-only or term-extension amendments).
- **Extractable from:** Modification/Amendment only
- **Extraction note:** Extract from the amendment's pricing exhibit or recitals. If the amendment adds a new rate schedule without a total, null this field and capture the change in the vector store. Summing this field across all modifications for a given `contract_number` gives total amendment value above the original award.

---

### Group C — Term and Renewal Exposure
*Drives renewal cliff dashboard and auto-renewal liability scan. Most immediately actionable group.*

---

#### `contract_start_date`
- **Type:** `DATE` (ISO 8601: YYYY-MM-DD)
- **Nullable:** Yes
- **Definition:** The date on which the current contract period begins. Semantics vary by doc type:
  - Fully Executed Agreement → effective date of the agreement
  - Renewal Letter → start date of the renewal period being granted
  - Award Letter → start date of the awarded contract period
  - Modification → effective date of the modification (not the original contract start)
- **Extraction note:** Do not conflate with `doc_date`. A renewal letter issued in November may grant a period beginning January 1.

---

#### `contract_end_date`
- **Type:** `DATE` (ISO 8601: YYYY-MM-DD)
- **Nullable:** Yes
- **Definition:** The date on which the current contract period ends. For renewal letters, this is the end of the renewal period being granted — the most forward-looking date in the document. Critical field for the renewal cliff dashboard; null rate here directly limits the dashboard's coverage.
- **Extractable from:** Fully Executed Agreement, Renewal Letter, Award Letter (reliable); Modification (only when amendment changes the term end date); Other (where applicable)

---

#### `renewal_options`
- **Type:** `STRING`
- **Nullable:** Yes — null on doc types where renewal terms are not stated
- **Definition:** The renewal option structure as stated in the original agreement or award letter. Normalized to a human-readable string describing the number and duration of renewal options available.
- **Extractable from:** Fully Executed Agreement, Award Letter
- **Example values:** `3 × 1-year options`, `2 × 2-year options`, `1 × 1-year option`, `no renewal options stated`
- **Extraction note:** Extract and normalize — do not reproduce the full clause. If only one party has the right to renew (e.g. county-only option), capture that in the string: `2 × 1-year options (county discretion)`.

---

#### `auto_renewal_flag`
- **Type:** `BOOLEAN`
- **Nullable:** Yes — null when doc type does not contain renewal terms
- **Definition:** Whether the contract renews automatically without affirmative action by either party. `TRUE` indicates the contract will roll over unless actively cancelled within the notice window. This is an inferred field — the LLM interprets termination and renewal clause language to determine the value.
- **Extractable from:** Fully Executed Agreement only (primary source of renewal/termination terms)
- **Extraction note:** Flag as inferred. Confidence should be noted in the evaluation. When clause language is ambiguous, prefer `NULL` over a forced boolean. The raw clause is available in the vector store for human review.

---

#### `termination_notice_days`
- **Type:** `INTEGER`
- **Nullable:** Yes — null when doc type does not contain termination terms
- **Definition:** The number of days advance written notice required to terminate the contract for convenience (i.e., without cause). This field captures the operational lead time required to exit a vendor relationship.
- **Extractable from:** Fully Executed Agreement only
- **Example values:** `30`, `60`, `90`, `180`
- **Extraction note:** Extract the for-convenience termination notice period specifically. Do not conflate with termination-for-cause notice periods, which are typically shorter. If only a for-cause period is stated, null this field.

---

### Group D — Vendor and Compliance Risk
*Drives vendor consolidation opportunity mapping and procurement mix analysis.*

---

#### `service_category`
- **Type:** `ENUM`
- **Nullable:** Yes — null for VDS forms; partial for Other
- **Definition:** The category of goods or services covered by this contract. This is an **inferred field** — the LLM assigns a category based on scope description, vendor name, and document context. It is not directly extracted from a labeled field in the document. Expected noise rate is higher than for extracted fields and must be evaluated explicitly.
- **Allowed values:**
  - `professional_services` — engineering, consulting, project management, legal, lobbying
  - `technology_software` — software licenses, SaaS, IT systems, body cameras, AI services
  - `facilities_maintenance` — HVAC, elevators, building systems, pump repair, janitorial
  - `public_safety` — ammunition, law enforcement equipment, security, corrections
  - `infrastructure` — roads, water/wastewater, ADA, construction
  - `staffing` — temporary and permanent staffing services
  - `supplies_goods` — uniforms, chemicals, OEM parts, consumables
  - `behavioral_health` — counseling, mental health, substance abuse, juvenile services
  - `other` — does not fit any above category
- **Extraction note:** Assign the single best-fit category. If the contract covers multiple categories, assign the primary one by spend or scope emphasis. Document this as an inferred field in the README and eval harness.

---

#### `procurement_vehicle`
- **Type:** `ENUM`
- **Nullable:** Yes — null on Renewal Letters, Modifications, VDS forms
- **Definition:** The mechanism by which this contract was procured. Relevant for benchmarking how much spend flows through competitive vs. non-competitive channels.
- **Allowed values:**
  - `direct_rfp` — Lake County issued a direct RFP, RFQ, or SOI and selected this vendor competitively
  - `cooperative_piggyback` — contract is issued under a cooperative purchasing vehicle (e.g. Sourcewell, Gordian, GSA)
  - `sole_source` — awarded without competition; single-source justification on file
  - `other` — procurement mechanism present but does not fit above categories
- **Extractable from:** Fully Executed Agreement, Award Letter
- **Extraction note:** Cooperative piggyback contracts typically reference the cooperative vehicle by name in the recitals (e.g. "pursuant to Sourcewell Contract #..."). Direct RFP contracts reference an RFP or SOI number.

---

#### `insurance_requirements_flag`
- **Type:** `BOOLEAN`
- **Nullable:** Yes — null when doc type does not contain insurance provisions
- **Definition:** Whether the document specifies explicit insurance requirements with named coverage types and dollar limits. `TRUE` signals a higher-complexity, higher-risk vendor relationship. Used as a lightweight risk-tier proxy across the vendor portfolio.
- **Extractable from:** Fully Executed Agreement (primary — typically Section 10 or similar); Award Letter (occasionally)
- **Extraction note:** Set `TRUE` if the document names specific coverage types (CGL, umbrella, professional liability, workers comp, auto) with minimum dollar amounts. Set `FALSE` if insurance is mentioned generically without specifics. Null if not present.

---

### Group E — Document Linkage
*Enables contract family reconstruction and true total commitment calculation.*

---

#### `parent_contract_number`
- **Type:** `STRING`
- **Nullable:** Yes — null on Fully Executed Agreements and Award Letters, which are the originating documents
- **Definition:** The contract number of the originating fully executed agreement that this document modifies, renews, or references. Populated on Renewal Letters and Modifications only. Enables joining child documents back to their parent agreement for family-level aggregation.
- **Extractable from:** Renewal Letter (always — the letter explicitly identifies the contract being renewed), Modification/Amendment (always — the amendment references its parent agreement), VDS (sometimes — the reference number may identify a contract), Other (sometimes — task orders and price increase letters typically reference a parent contract)
- **Extraction note:** In most cases `parent_contract_number` will equal `contract_number` — they share the same number. The field exists to make the parent-child relationship explicit in the schema and to handle edge cases where numbering diverges across amendments.

---

## 6. Field-to-Document-Type Coverage Matrix

Coverage codes:
- **E** — Expected and reliable: field is consistently present and extractable in this doc type
- **P** — Partial: field is present in some instances of this doc type but not reliably
- **N** — Null by design: this doc type does not contain this field; null is correct, not a failure

| Field | Fully Executed | Renewal Letter | Modification | Award Letter | VDS | Other |
|-------|:--------------:|:--------------:|:------------:|:------------:|:---:|:-----:|
| `contract_number` | E | E | E | E | E | E |
| `doc_type` | E | E | E | E | E | E |
| `vendor_name` | E | E | E | E | E | E |
| `doc_date` | E | E | E | E | E | E |
| `county_department` | P | P | P | P | N | P |
| `total_contract_value` | P | N | N | P | N | P |
| `price_escalator_terms` | E | N | P | N | N | N |
| `modification_financial_delta` | N | N | P | N | N | N |
| `contract_start_date` | E | E | P | E | N | P |
| `contract_end_date` | E | E | P | E | N | P |
| `renewal_options` | E | N | N | E | N | N |
| `auto_renewal_flag` | P | N | N | N | N | N |
| `termination_notice_days` | E | N | N | N | N | N |
| `service_category` | E | E | E | E | N | P |
| `procurement_vehicle` | E | N | N | E | N | N |
| `insurance_requirements_flag` | E | N | N | P | N | N |
| `parent_contract_number` | N | E | E | N | P | P |

### Expected Null Patterns by Document Type

**Fully Executed Agreement** — the richest document type. All Group C and D fields should be populated. Group B fields are reliable except `total_contract_value` when Exhibit A is blank and `modification_financial_delta` (always null here by design).

**Renewal Letter** — thin document. Only spine fields, date fields, and `parent_contract_number` expected. Everything else null by design. High confidence extractions; consistent template makes them reliable.

**Modification/Amendment** — delta document. Spine fields plus `modification_financial_delta` (when financial), `contract_start_date` / `contract_end_date` (when term changes). Most Group C and D fields null by design.

**Award Letter** — variable. Simple award letters yield only spine fields, dates, and `renewal_options`. Bid-tab award letters also yield `total_contract_value`. Both yield `procurement_vehicle`.

**Vendor Disclosure Statement** — minimal extraction target. Spine fields only. All other fields null by design. Do not attempt Group B–D extraction on VDS forms.

**Other** — highly variable. Attempt all fields; expect high null rates. Price increase letters and task orders are the richest subtype within Other.

---

## 7. SQLite Database Schema (DDL)

```sql
CREATE TABLE contracts (
    -- Row identity
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_filename             TEXT NOT NULL,
    pipeline_run_timestamp      TEXT NOT NULL,          -- ISO 8601 datetime

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
    extraction_notes            TEXT                    -- free text for flagged issues
);

-- Indexes for common query patterns
CREATE INDEX idx_contract_number ON contracts(contract_number);
CREATE INDEX idx_doc_type ON contracts(doc_type);
CREATE INDEX idx_vendor_name ON contracts(vendor_name);
CREATE INDEX idx_contract_end_date ON contracts(contract_end_date);
CREATE INDEX idx_service_category ON contracts(service_category);
```

---

## 8. Downstream Analyses the Schema Enables

These are the concrete, named analyses that justify the field selection. Each analysis is described with the fields it requires, the business question it answers, and the expected output format.

---

### Analysis 1 — Renewal Cliff Dashboard

**Business question:** Which vendor contracts are expiring in the next 90, 180, or 365 days, and what is the dollar value at risk?

**Fields required:** `contract_end_date`, `total_contract_value`, `vendor_name`, `contract_number`, `service_category`, `renewal_options`

**How it works:** Filter rows where `contract_end_date` falls within the target window. Join to the most recent `total_contract_value` for that `contract_number` (from the fully executed agreement or most recent amendment). Sort by value descending. Group by `service_category` for a category-level view.

**Expected output:** Ranked list of expiring contracts with vendor name, end date, total value, category, and remaining renewal options. Visualization: bar chart sorted by days-to-expiry, colored by service category.

**Why it matters:** This is the single most immediately actionable output. Most portcos with unmanaged contract archives have no visibility into upcoming expirations until a vendor or account manager flags it. "You have $2.3M in contracts expiring in the next 90 days — here are the five largest" drives a real decision next week.

---

### Analysis 2 — Auto-Renewal Liability Scan

**Business question:** Which contracts will renew automatically without affirmative action, and when must we act to prevent unwanted renewals?

**Fields required:** `auto_renewal_flag`, `contract_end_date`, `termination_notice_days`, `vendor_name`, `contract_number`, `total_contract_value`

**How it works:** Filter `auto_renewal_flag = TRUE`. Calculate the cancellation deadline: `contract_end_date` minus `termination_notice_days`. Flag contracts where the cancellation deadline is within 30 days. Sort by cancellation deadline ascending.

**Expected output:** Alert list of contracts with imminent cancellation deadlines. For each: vendor name, contract value, cancellation deadline, and end date. Visualization: timeline view with "act by" dates highlighted.

**Why it matters:** Auto-renewing contracts represent a silent liability. A procurement team managing hundreds of vendor relationships will routinely miss these windows without tooling. A single missed cancellation on a multi-year software contract can lock in six figures of unwanted spend.

---

### Analysis 3 — Spend Concentration Map

**Business question:** Which vendors represent an outsized share of total spend, and where does single-vendor dependency risk exist?

**Fields required:** `vendor_name`, `total_contract_value`, `service_category`, `contract_number`, `doc_type`

**How it works:** Aggregate `total_contract_value` by `vendor_name` across all fully executed agreements (filter `doc_type = 'fully_executed_agreement'` to avoid double-counting with renewals). Calculate each vendor's share of total portfolio spend. Repeat the aggregation at the `service_category` level to identify category-level concentration.

**Expected output:** Vendor-level spend ranking (top 10–20 vendors by total value, with percentage of total). Category-level heat map showing spend distribution across service categories. Visualization: horizontal bar chart with concentration percentage annotations.

**Why it matters:** Most mid-market portcos have never seen their vendor spend in one view. Surfacing that a single staffing firm represents 35% of total vendor spend, or that three HVAC vendors are doing overlapping work, creates immediate consolidation opportunities.

---

### Analysis 4 — Price Escalation Exposure

**Business question:** Which contracts carry uncapped or inflation-linked price escalators that represent budget risk at renewal?

**Fields required:** `price_escalator_terms`, `contract_end_date`, `total_contract_value`, `vendor_name`, `contract_number`, `service_category`

**How it works:** Filter to contracts where `price_escalator_terms IN ('cpi_capped', 'fixed_percentage', 'negotiated_at_renewal')` and `contract_end_date` is within the next 12 months. Rank by `total_contract_value`. For `cpi_capped` contracts, overlay current CPI to estimate maximum price increase at renewal.

**Expected output:** List of at-risk contracts with escalator type, current value, estimated renewal cost range, and days to expiry. Visualization: scatter plot with contract value on Y-axis and days to renewal on X-axis, colored by escalator type.

**Why it matters:** In an inflationary environment, CPI-linked escalators on large, multi-year contracts can represent material budget surprises. Quantifying this exposure before renewals are executed gives the CFO real negotiating context.

---

### Analysis 5 — True Total Commitment by Contract Family

**Business question:** What is the actual total spend commitment for each vendor relationship, including all amendments above the original award?

**Fields required:** `contract_number`, `parent_contract_number`, `total_contract_value`, `modification_financial_delta`, `doc_type`, `vendor_name`

**How it works:** For each `contract_number`, sum the original `total_contract_value` (from the `fully_executed_agreement` row) plus all `modification_financial_delta` values (from `modification_amendment` rows sharing the same `contract_number`). Where `parent_contract_number` is populated, use it to ensure modifications link correctly to their parent.

**Expected output:** Contract family summary table with original award value, total amendment value, and true total commitment. Flag families where amendment value exceeds original award by more than 25%.

**Why it matters:** Original contract values significantly understate total vendor spend when amendments are not tracked. A contract awarded at $100K with three amendments totaling $180K has a true commitment of $280K — invisible without this aggregation.

---

### Analysis 6 — Vendor Consolidation Opportunity Map

**Business question:** Which service categories have high vendor fragmentation that could benefit from consolidation?

**Fields required:** `service_category`, `vendor_name`, `total_contract_value`, `contract_number`

**How it works:** For each `service_category`, count distinct `vendor_name` values and sum `total_contract_value`. Calculate average contract value per vendor in the category. Categories with high vendor count and low average contract value are the consolidation candidates.

**Expected output:** Category-level matrix: number of vendors, total category spend, average contract value, and a fragmentation score. Highlight categories where consolidation to 1–2 primary vendors could reduce administrative overhead and improve pricing leverage.

**Why it matters:** Fragmented vendor relationships in a single category mean lost pricing leverage, duplicated onboarding overhead, and compliance complexity. A portco with 8 staffing vendors and no master agreement is leaving money on the table.

---

## 9. Fields Excluded and Why

The following field types were considered and deliberately excluded:

**Free-text scope description** — Scope text belongs in the RAG/vector layer. A text blob in a SQL column cannot be aggregated, grouped, or filtered in any meaningful way. `service_category` provides the structured handle; the vector store handles semantic queries against the actual clause language.

**Insurance dollar limits** — The specific dollar amounts for each coverage type (CGL, umbrella, professional liability, etc.) are extractable from fully executed agreements but create a wide, sparse set of numeric fields with low downstream value in the structured table. For portcos that need this detail, it is better served by a targeted RAG query ("what are the insurance requirements for contract 23159?") than by structured columns.

**Governing law / jurisdiction** — Present in all fully executed agreements but near-uniform (Illinois law) in this corpus. Not actionable at the analysis level for a portco use case.

**Vendor contact information** — Extractable from renewal letters and VDS forms but not analytically valuable in the structured table. Contact details belong in a CRM, not a contract intelligence DB.

**VDS disclosure flags** — The familial relationship and campaign contribution disclosure fields from VDS forms have very low analytical value (almost universally "None") and are excluded from the schema. VDS rows carry only the universal spine fields.

---

## 10. Inferred vs. Extracted Fields

The following fields require LLM inference rather than direct text extraction. These must be evaluated separately in the evaluation harness and disclosed honestly in the "proven vs. assumed" framing during the walkthrough.

| Field | Why Inferred | Expected Accuracy |
|-------|-------------|-------------------|
| `service_category` | No standard labeled field in documents; assigned from scope text and vendor name | Medium — expect ~15–20% noise; spot-check in eval |
| `auto_renewal_flag` | Requires interpretation of termination/renewal clause language | Medium-high — clause language is typically clear but occasionally ambiguous |
| `price_escalator_terms` | Requires normalization of varied clause language to a controlled vocabulary | High — escalator types are finite and clause patterns are consistent |

All other fields are direct extractions from document text.

---

## 11. Pipeline Implementation Notes

These notes are for the implementing agent and should be reflected in the extraction prompt design and pipeline architecture.

- **Classification before extraction:** The doc type classifier must run as Step 0 on every document before any extraction prompt is invoked. The extraction prompt uses `doc_type` to determine which fields to populate and which to skip.
- **Renewal letter page handling:** Classify renewal letters using page 1 content only. Page 2 (blank VDS form) must be excluded from extraction and must not trigger a separate document row.
- **Exhibit A handling:** When a fully executed agreement contains Exhibit A and it is parseable, extract `total_contract_value` from it. When Exhibit A is blank or redacted, null `total_contract_value` and record `extraction_notes = 'Exhibit A blank or redacted'`.
- **Confidence scoring:** Populate `extraction_confidence` as `high` / `medium` / `low` based on document quality, page count, and whether key fields were extractable. Use `extraction_notes` for any flagged issues (redacted content, scanned images, missing sections).
- **Multi-vendor contracts:** Where one contract number covers multiple vendors (e.g. contract 23159 with multiple award letters), each document produces its own row with its own `vendor_name`. The shared `contract_number` links them. A derived `n_vendors` attribute can be computed at query time by counting distinct `vendor_name` values per `contract_number`.
- **Non-nullable enforcement:** If `contract_number` or `vendor_name` cannot be extracted, do not write the row. Log the file as a pipeline failure and include it in the eval results.