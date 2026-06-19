# Task 1.3 — Extraction Prompt Strategy & Implementation Requirements

> **Purpose:** This document specifies the extraction prompt architecture for the contract intelligence pipeline. It is the authoritative implementation guide for the LLM extraction step (Pipeline Task 2.3). All prompt code, orchestration logic, and output validation should trace back to this document.
>
> **Dependencies:** Extraction schema (`extraction_schema.md`) for field definitions and coverage matrix. Document classifier output (Task 2.2) as input to prompt routing.

---

## 1. Architecture Decision

### Approach: Single Templated Prompt with Doc-Type Field Injection

The extraction prompt is a single reusable template consisting of:

1. **Shared header** — role, goal, and context instructions
2. **Universal spine fields** — 5 fields extracted from every document regardless of type
3. **Injected field block** — doc-type-specific fields inserted dynamically at runtime based on classifier output
4. **Shared footer** — output format rules, null handling, confidence scoring instructions, and example JSON

This approach was chosen over:
- A single universal prompt covering all fields on every document — wasteful, error-prone, higher hallucination risk on thin documents
- Separate independent prompts per doc type — higher maintenance overhead, harder to update shared instructions

**Key property:** The doc-type classifier (Task 2.2) must run before extraction. Its output drives which field block is injected. A misclassification produces a wrong field block with no obvious error signal — classifier accuracy is therefore a first-class dependency of extraction accuracy.

---

## 2. Prompt Template

```python
EXTRACTION_PROMPT_TEMPLATE = """
You are a contract data extraction specialist. Your task is to extract structured
information from a procurement contract document and return it as a JSON object.

## Context

This document is from a municipal procurement archive and belongs to one of six
document types: fully executed agreement, renewal letter, modification/amendment,
award letter, vendor disclosure statement, or other. The document type has already
been classified and is provided to you below. Extract only the fields listed for
this document type — do not attempt to populate fields not listed.

Document type: {doc_type}

---

## Universal Fields (extract from every document)

Extract the following fields from every document regardless of type. These fields
are non-nullable — if you cannot extract a value, set "extraction_confidence" to
"low" and explain the failure in "extraction_notes". Do not omit these keys from
the output JSON.

### contract_number
- Type: string
- Nullable: NO — if not extractable, return null and flag in extraction_notes
- Definition: The unique identifier assigned to this contract. This is the primary
  linking key across all documents in a contract family.
- Extraction rule: Extract exactly as it appears in the document. Do not normalize,
  reformat, or add prefixes. If multiple candidate numbers appear in the document,
  prefer the one in the document header or letterhead.
- Examples: "23159", "22847-A", "C-2024-0312"

### vendor_name
- Type: string
- Nullable: NO
- Definition: The name of the external vendor or contractor — the party that is NOT
  the issuing organization (i.e., not Lake County, not the portco).
- Extraction rule: Normalize to the legal entity name where determinable (e.g.,
  "Johnson Controls Inc." not "Johnson Controls"). In multi-vendor documents, extract
  the vendor named in this specific document — do not concatenate multiple vendors.
- Examples: "Johnson Controls Inc.", "Actalent Services LLC", "Axon Enterprise Inc."

### doc_date
- Type: date (ISO 8601 format: YYYY-MM-DD)
- Nullable: NO — use the best available date signal in the document
- Definition: The document's own date. Semantics vary by document type:
    - Fully executed agreement → execution/signing date
    - Renewal letter → letter date (the date the letter was issued)
    - Modification/amendment → effective date of the modification
    - Award letter → award date
    - Vendor disclosure statement → signature date
    - Other → most prominent date in the document
- Extraction rule: Extract from document text only. Do not use file metadata or
  infer from filenames. Return in YYYY-MM-DD format.

### county_department
- Type: string
- Nullable: YES
- Definition: The issuing organization's department or division that this contract
  serves. In a portco context this maps to "business unit." May appear in scope
  descriptions, letterheads, routing language, or the body of the document.
- Extraction rule: Extract verbatim if present. Do not infer from vendor name,
  service type, or subject matter alone. Return null if not determinable.
- Examples: "Sheriff's Office", "Lake County Health Department",
  "Division of Transportation", "Facilities and Construction Services"

### doc_type
- Type: string (enum)
- Nullable: NO
- Definition: The document type as classified before this extraction step. Copy
  the value provided above exactly — do not reclassify.
- Allowed values: "fully_executed_agreement" | "renewal_letter" |
  "modification_amendment" | "award_letter" | "vendor_disclosure_statement" | "other"

---

## Document-Type-Specific Fields

Based on the document type above, also extract the following fields. Return null
for any field whose value is not present or not determinable in this document.

{injected_fields}

---

## Output Format

Return a single valid JSON object. Requirements:
- Include every field listed above (both universal and type-specific) as a key.
- Use null (not empty string, not "N/A", not "unknown") for any field that is not
  present or not extractable.
- For boolean fields, use true or false (not 1/0, not "yes"/"no").
- For date fields, use YYYY-MM-DD string format or null.
- For float fields, return a numeric value with no currency symbols or commas
  (e.g., 125000.00 not "$125,000").
- For enum fields, return only one of the allowed values listed — never free text.
- Do not include any text outside the JSON object. No preamble, no explanation,
  no markdown code fences.
- Include these two metadata fields in every response:
    - "extraction_confidence": one of "high" / "medium" / "low"
        - high: all non-nullable fields extracted cleanly; most type-specific fields
          populated
        - medium: non-nullable fields extracted; some type-specific fields null due
          to document quality or structure
        - low: one or more non-nullable fields could not be extracted; or document
          appears malformed, scanned, or heavily redacted
    - "extraction_notes": string or null — use to flag any issues encountered
      (e.g., "Exhibit A present but blank — total_contract_value set to null",
      "contract_number ambiguous — two candidates found, preferred header value",
      "document appears to be scanned image, text quality degraded")

## Example output structure (field values are illustrative):

{
  "contract_number": "23159",
  "vendor_name": "Johnson Controls Inc.",
  "doc_date": "2023-04-15",
  "county_department": "Facilities and Construction Services",
  "doc_type": "fully_executed_agreement",
  "total_contract_value": 248500.00,
  "price_escalator_terms": "cpi_capped",
  "contract_start_date": "2023-05-01",
  "contract_end_date": "2024-04-30",
  "renewal_options": "3 × 1-year options",
  "auto_renewal_flag": false,
  "termination_notice_days": 30,
  "service_category": "facilities_maintenance",
  "procurement_vehicle": "direct_rfp",
  "insurance_requirements_flag": true,
  "parent_contract_number": null,
  "extraction_confidence": "high",
  "extraction_notes": null
}

---

## Document text

{document_text}
"""
```

---

## 3. Injected Field Blocks

The following Python dict maps each doc type enum value to its field block string. At runtime, the pipeline looks up `INJECTED_FIELDS[doc_type]` and substitutes it into `{injected_fields}` in the template above.

```python
INJECTED_FIELDS = {

"fully_executed_agreement": """
### total_contract_value
- Type: float (USD)
- Nullable: YES
- Definition: The total dollar value of the contract. For rate-based agreements,
  use the total not-to-exceed value if stated.
- Extraction rule: Numeric value only — strip currency symbols and commas.
  Null if not stated. If Exhibit A is present but blank or redacted, set to null
  and note in extraction_notes: "Exhibit A blank or redacted."

### price_escalator_terms
- Type: enum — INFER from clause language
- Nullable: YES
- Definition: How contract pricing may change over its term.
- Allowed values:
    - "fixed" — price does not change over the contract term
    - "cpi_capped" — increases allowed but capped at CPI or a named index
    - "fixed_percentage" — increases at a specified fixed annual percentage
    - "negotiated_at_renewal" — pricing renegotiated at each renewal
    - "not_specified" — contract does not address price escalation
- Extraction rule: INFER by reading the pricing and escalation clauses. If the
  language is ambiguous, prefer "not_specified" over guessing. Do not reproduce
  clause text — return only the enum value.

### contract_start_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The effective date of this agreement — the date the contract period
  begins.
- Extraction rule: Do not conflate with doc_date (the signing date). These may
  differ.

### contract_end_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The date on which the initial contract period ends.

### renewal_options
- Type: string
- Nullable: YES
- Definition: The renewal option structure available under this agreement.
- Extraction rule: Normalize to a compact human-readable string. Do not reproduce
  the full clause. If only one party holds the renewal right, note it in the string.
- Examples: "3 × 1-year options", "2 × 1-year options (county discretion)",
  "no renewal options stated"

### auto_renewal_flag
- Type: boolean — INFER from clause language
- Nullable: YES
- Definition: Whether the contract renews automatically without affirmative action
  by either party. True means the contract rolls over unless actively cancelled
  within the notice window.
- Extraction rule: INFER by reading termination and renewal clauses. If the
  language is ambiguous, return null — do not force a boolean. This field will
  be spot-checked in evaluation.

### termination_notice_days
- Type: integer
- Nullable: YES
- Definition: The number of days advance written notice required to terminate
  this contract for convenience (i.e., without cause).
- Extraction rule: Extract the for-convenience notice period specifically. Do not
  use termination-for-cause notice periods, which are typically shorter. If only
  a for-cause period is stated, return null.
- Examples: 30, 60, 90

### service_category
- Type: enum — INFER from scope, vendor name, and document context
- Nullable: YES
- Definition: The category of goods or services covered by this contract.
- Allowed values:
    - "professional_services" — engineering, consulting, project management,
      legal, lobbying
    - "technology_software" — software licenses, SaaS, IT systems, body cameras,
      AI services
    - "facilities_maintenance" — HVAC, elevators, building systems, pump repair,
      janitorial
    - "public_safety" — ammunition, law enforcement equipment, security, corrections
    - "infrastructure" — roads, water/wastewater, ADA, construction
    - "staffing" — temporary and permanent staffing services
    - "supplies_goods" — uniforms, chemicals, OEM parts, consumables
    - "behavioral_health" — counseling, mental health, substance abuse,
      juvenile services
    - "other" — does not fit any category above
- Extraction rule: INFER from the scope of work, vendor name, and any exhibit
  content. Assign the single best-fit category. If multiple categories apply,
  choose the primary one by spend or scope emphasis.

### procurement_vehicle
- Type: enum
- Nullable: YES
- Definition: The mechanism by which this contract was procured.
- Allowed values:
    - "direct_rfp" — issued via a direct RFP, RFQ, or SOI competitive process
    - "cooperative_piggyback" — issued under a cooperative purchasing vehicle
      (e.g., Sourcewell, Gordian, GSA)
    - "sole_source" — awarded without competition
    - "other" — procurement mechanism present but does not fit above categories
- Extraction rule: Cooperative piggyback contracts reference the cooperative
  vehicle by name in the recitals. Direct RFP contracts reference an RFP or
  SOI number. If neither signal is present, return null.

### insurance_requirements_flag
- Type: boolean
- Nullable: YES
- Definition: Whether this document specifies explicit insurance requirements
  with named coverage types and minimum dollar limits.
- Extraction rule: Return true if the document names specific coverage types
  (e.g., CGL, umbrella, professional liability, workers comp, auto) with dollar
  amounts. Return false if insurance is mentioned generically without specifics.
  Null if insurance is not addressed.
""",


"renewal_letter": """
### contract_start_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The start date of the renewal period being granted by this letter.
  This is NOT the original contract start date.
- Extraction rule: A renewal letter issued in November may grant a period
  beginning January 1 of the following year. Extract the renewal period start,
  not the letter date.

### contract_end_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The end date of the renewal period being granted by this letter —
  the most forward-looking date in the document. This is the critical field for
  renewal exposure tracking.

### parent_contract_number
- Type: string
- Nullable: YES
- Definition: The contract number of the originating agreement being renewed.
  In most cases this will match contract_number — the field exists to make the
  parent-child relationship explicit.
- Extraction rule: The renewal letter will explicitly identify the contract
  being renewed. Extract that number exactly as it appears.

### service_category
- Type: enum — INFER from contract description and vendor name
- Nullable: YES
- Definition: The category of goods or services covered by this contract.
- Allowed values: "professional_services" | "technology_software" |
  "facilities_maintenance" | "public_safety" | "infrastructure" | "staffing" |
  "supplies_goods" | "behavioral_health" | "other"
- Extraction rule: INFER from the contract description in the letter header and
  the vendor name. Renewal letters are thin — the description in the letterhead
  block is the primary signal.
""",


"modification_amendment": """
### modification_financial_delta
- Type: float (USD)
- Nullable: YES
- Definition: The net dollar change to contract value introduced by this amendment.
  Positive = increase, negative = decrease. Null if this modification does not
  involve a financial change (e.g., scope-only or term-extension amendments).
- Extraction rule: Extract from the amendment's pricing exhibit or recitals.
  If the amendment adds a new rate schedule without stating a total, return null
  and note in extraction_notes.

### contract_start_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The effective date of this modification — not the original
  contract start date.

### contract_end_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The new contract end date if this amendment changes the term.
  Null if the modification does not affect the contract term.

### parent_contract_number
- Type: string
- Nullable: YES
- Definition: The contract number of the originating agreement being modified.
- Extraction rule: The amendment will reference the parent agreement explicitly.
  Extract that contract number exactly as it appears.

### service_category
- Type: enum — INFER from scope language and vendor name
- Nullable: YES
- Definition: The category of goods or services covered by the parent contract
  being modified.
- Allowed values: "professional_services" | "technology_software" |
  "facilities_maintenance" | "public_safety" | "infrastructure" | "staffing" |
  "supplies_goods" | "behavioral_health" | "other"
- Extraction rule: INFER from the scope language describing what is being
  amended and the vendor name. Amendments are delta documents — infer category
  from what is changed, not from a full scope description.

### price_escalator_terms
- Type: enum — INFER from clause language
- Nullable: YES
- Definition: If this amendment modifies pricing terms, normalize the new
  escalator structure.
- Allowed values: "fixed" | "cpi_capped" | "fixed_percentage" |
  "negotiated_at_renewal" | "not_specified"
- Extraction rule: Only populate if this amendment explicitly modifies pricing
  or escalation terms. Null if the amendment does not address pricing.
""",


"award_letter": """
### total_contract_value
- Type: float (USD)
- Nullable: YES
- Definition: The total awarded contract value. Present and reliable in
  intent-to-award letters with bid tabs. Often absent in simple award letters.
- Extraction rule: Numeric value only — strip currency symbols and commas.
  Null if not stated. Do not sum line items — extract the stated total only.

### contract_start_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The start date of the awarded contract period.

### contract_end_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The end date of the awarded contract period.

### renewal_options
- Type: string
- Nullable: YES
- Definition: The renewal option structure available under this award.
- Extraction rule: Normalize to a compact string.
- Examples: "3 × 1-year options", "1 × 1-year option", "no renewal options stated"

### procurement_vehicle
- Type: enum
- Nullable: YES
- Definition: The procurement mechanism for this award.
- Allowed values: "direct_rfp" | "cooperative_piggyback" | "sole_source" | "other"
- Extraction rule: Award letters referencing an RFP or SOI number → "direct_rfp".
  Letters referencing Sourcewell, Gordian, or GSA → "cooperative_piggyback".
  Null if not determinable.

### insurance_requirements_flag
- Type: boolean
- Nullable: YES
- Definition: Whether this award letter includes explicit insurance requirements
  with named coverage types and dollar limits.
- Extraction rule: Return true if named coverage types with dollar amounts are
  present. Return false if insurance is mentioned generically. Null if not addressed.

### service_category
- Type: enum — INFER from contract description and vendor name
- Nullable: YES
- Definition: The category of goods or services covered by this award.
- Allowed values: "professional_services" | "technology_software" |
  "facilities_maintenance" | "public_safety" | "infrastructure" | "staffing" |
  "supplies_goods" | "behavioral_health" | "other"
- Extraction rule: INFER from the contract description in the award letter and
  the vendor name.
""",


"vendor_disclosure_statement": """
### parent_contract_number
- Type: string
- Nullable: YES
- Definition: The contract or renewal reference number this VDS was submitted in
  connection with.
- Extraction rule: VDS forms include a reference number field. Extract it if
  present. In most cases this will match contract_number.
""",


"other": """
### total_contract_value
- Type: float (USD)
- Nullable: YES
- Definition: The total dollar value stated in this document, if present.
  Applies to task orders, price sheets, and price increase letters.
- Extraction rule: Numeric value only. Null if not stated.

### modification_financial_delta
- Type: float (USD)
- Nullable: YES
- Definition: The net dollar change stated in this document, if it amends a
  prior agreement. Applies to price increase letters and scope additions.
- Extraction rule: Null if this document does not explicitly state a change
  to contract value.

### contract_start_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The start date of the period or task covered by this document.

### contract_end_date
- Type: date (YYYY-MM-DD)
- Nullable: YES
- Definition: The end date of the period or task covered by this document.

### parent_contract_number
- Type: string
- Nullable: YES
- Definition: The parent contract number this document references, if applicable.
- Extraction rule: Task orders, price increase letters, and SOW documents
  typically reference a parent contract. Extract that number exactly as it appears.

### service_category
- Type: enum — INFER from document content and vendor name
- Nullable: YES
- Definition: The category of goods or services this document relates to.
- Allowed values: "professional_services" | "technology_software" |
  "facilities_maintenance" | "public_safety" | "infrastructure" | "staffing" |
  "supplies_goods" | "behavioral_health" | "other"
- Extraction rule: INFER from available context. High null rates expected for
  this document type.
"""

}
```

---

## 4. Runtime Orchestration

The pipeline module that calls the LLM extractor should follow this logic:

```python
def build_extraction_prompt(doc_type: str, document_text: str) -> str:
    injected = INJECTED_FIELDS.get(doc_type, INJECTED_FIELDS["other"])
    return EXTRACTION_PROMPT_TEMPLATE.format(
        doc_type=doc_type,
        injected_fields=injected,
        document_text=document_text
    )
```

**Input:** Classified `doc_type` string (from Task 2.2 classifier) + extracted document text (from Task 2.1 PDF parser).

**Output:** Formatted prompt string ready to send to the LLM.

**Fallback:** If `doc_type` is not found in `INJECTED_FIELDS`, default to the `"other"` block. This should not happen in normal operation but handles any classifier edge cases gracefully.

---

## 5. Output Validation

After receiving the LLM response, the pipeline should validate the JSON before writing to SQLite:

- Parse as JSON — if unparseable, log as extraction failure and skip the row
- Assert all universal spine fields are present as keys
- Assert `contract_number` and `vendor_name` are non-null — if either is null, do not write the row; log as pipeline failure
- Assert `doc_type` value matches one of the six allowed enum values
- Assert enum fields (`price_escalator_terms`, `service_category`, `procurement_vehicle`) contain only allowed values or null
- Assert date fields match YYYY-MM-DD format or are null
- Assert `extraction_confidence` is one of `"high"` / `"medium"` / `"low"`

Documents that fail non-nullable field validation should be logged to a separate failures table or CSV for manual review.

---

## 6. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Single template vs. per-type prompts | Single template with injected blocks | Maintainable — shared instructions updated once; extensible — new doc type = new dict entry |
| Prompt verbosity | Uniform full definitions for all fields | Consistency over selective trimming; easier to justify and maintain for PoC; optimize post-eval if token cost or accuracy warrants |
| Confidence scoring | Document-level only (`extraction_confidence` + `extraction_notes`) | Sufficient for PoC; field-level confidence scoring is a production hardening item |
| Inferred fields labeling | `INFER` prefix in type line within field block | Makes inference vs. extraction distinction visible in the prompt itself; eval harness can use field name list to apply separate scoring |
| Null handling | Explicit `null` required; empty string and "N/A" prohibited | Consistent null representation simplifies SQLite writes and downstream queries |
| Output format | Raw JSON only; no markdown fences, no preamble | Simplifies parsing; reduces failure modes in output validation step |

---

## 7. Inferred Fields Reference

These fields require LLM inference rather than direct text extraction. They must be evaluated separately in the eval harness and disclosed in the "proven vs. assumed" walkthrough framing.

| Field | Doc Types | Inference Task |
|-------|-----------|----------------|
| `service_category` | All except VDS | Assign category from scope text and vendor name |
| `auto_renewal_flag` | Fully Executed Agreement | Interpret termination/renewal clause language |
| `price_escalator_terms` | Fully Executed Agreement, Modification | Normalize escalator clause to controlled vocabulary |

---

## 8. Notes for Prompt Iteration (Task 2.4)

Before running extraction at scale, run the prompt manually on 5–10 documents covering at least 3 doc types. Watch for:

- `contract_number` extraction failures — most common on scanned or poorly formatted docs
- `doc_date` vs. `contract_start_date` conflation — especially on renewal letters
- `auto_renewal_flag` forced booleans where clause language is ambiguous — should be null
- `service_category` mis-assignments — spot-check against known vendor names
- `total_contract_value` hallucination on docs where Exhibit A is blank — should be null, not a fabricated number
- Enum values returned as free text — tighten the allowed values instruction if observed

Prompt refinements made during iteration should be documented in `extraction_notes` patterns observed and addressed.