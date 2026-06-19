# Sampling Strategy — Berkshire Partners Take-Home
## Context & Goal

The contract corpus contains ~387 PDF documents across ~6 document types. The goal is to select a sample of ~100 documents (flexible) to run through the extraction pipeline. 

After reviewing sample documents and analyzing the filename structure, we determined that documents are organized into **contract families** — a set of related documents sharing a common contract number that represent the full lifecycle of a vendor relationship (e.g., award → fully executed agreement → modifications → renewals → vendor disclosures).

The unit of sampling is the **contract family**, not the individual document. This ensures lifecycle integrity — sampling individual documents risks pulling renewal letters without their parent agreements, which would introduce structural gaps that mislead downstream analysis.

---

## Document Taxonomy

Six document types observed in the corpus:

- **Fully Executed Agreement** — base contracts, 10–20 pages, richest fields (parties, term, value, scope, pricing, insurance, renewal options)
- **Renewal Letter** — thin 1-2 page templated letters; always include a blank VDS form as page 2; consistent fields (contract #, vendor, period, new period)
- **Modification / Amendment** — scope, price, or term changes to existing contracts; delta-only fields; vary in richness
- **Vendor Disclosure Statement (VDS)** — 1-page compliance forms; near-zero business signal; appear both as standalone signed files and as blank attachments inside renewal letters
- **Award / Intent-to-Award Letter** — pre-contract notices; two subtypes: (1) simple 1-page award letters, (2) intent-to-award letters with full bid tab line items (much richer)
- **Other** — price increase letters, SOWs, task orders, bid docs, extensions, cooperative procurement agreements

---

## Contract Family Structure

Documents are linked by **contract number**, which appears in filenames either as a leading number (`18018_Award_Letter.pdf`) or embedded mid-filename (`2024_01_30_Contract_23145_Modification_1_EXECUTED.pdf`).

Example of a complete family (Contract 18018):
- `18018_Award_Letter.pdf`
- `18018_Vendor_Disclosure_Form__signed.pdf`
- `18018_Vendor_Renewal_Letter_2020_2021.pdf`
- `18018_Vendor_Renewal_Letter_2021_2022.pdf`
- `18018_Vendor_Renewal_Letter_2022_2023.pdf`

Example of a multi-vendor family (Contract 23159 — 3 vendors, 9 docs):
- Award letters, VDS forms, and renewal letters for AGAE, Leopardo, and McDonagh all share contract number 23159

Family sizes vary: small (2-3 docs), medium (4-6 docs), large (7+). The minimum family size threshold for inclusion will be determined after running the distribution analysis.

---

## Sampling Approach

1. **Parse contract numbers** from all filenames using regex (two patterns needed: leading contract number, embedded `Contract_XXXXX`)
2. **Group files into families** by contract number
3. **Flag unlinked files** — filenames with no parseable contract number should be flagged separately, not silently dropped. For this PoC these will likely be skipped but should be noted.
4. **Analyze family size distribution** — characterize min/max/median, determine bucket boundaries (small/medium/large)
5. **Set minimum family size threshold** — based on distribution; exclude families below the threshold (e.g., singleton files or pairs with no substantive docs)
6. **Stratified random sample of families** — sample across size buckets to ensure variety in lifecycle completeness and document richness. Target ~20-25 families, accepting whatever total document count results (expected ~80-120 docs)
7. **Fixed random seed** — document the seed for reproducibility; include in README

---

## Key Decisions

| Decision | Choice |
|---|---|
| Unit of sampling | Contract family |
| Linking key | Contract number |
| Target families | ~20-25 |
| Target documents | ~100 (flexible — completeness over strict count) |
| Minimum family size | TBD after distribution analysis |
| Unlinked files | Flag and skip for PoC |
| Random selection | Reproducible with fixed seed |

---

## Schema Notes for Coding Agent

- `contract_number` — non-nullable linking key; extract from every document
- `doc_type` — required for lifecycle reconstruction and downstream analysis
- `vendor_name` — extract from every document; some contracts have multiple vendors under one contract number
- `n_vendors` — derived attribute; count of distinct vendors per contract family; useful for vendor consolidation analysis
- One row per document in the structured table; contract number enables grouping across rows

---

## Notes & Caveats

- Filename-based contract number parsing will not be perfect. Some files have ambiguous or missing contract numbers. Flag these rather than guessing.
- Multi-vendor contracts (like 23159) should be treated as one family. Vendor name and a vendor count field handle the multi-vendor dimension within the schema.
- Renewal letter PDFs always contain a blank VDS form as page 2. The extraction pipeline should key off page 1 content for classification, not the full document.
- Award / Intent-to-Award letters come in two subtypes — simple letters and letters with full bid tab pricing. The classifier should distinguish these if possible as they have very different extraction richness.
- The ~387 file count and distribution estimates are based on filename analysis with spot-check validation. Treat as approximate.