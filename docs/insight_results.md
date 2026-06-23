# Structured Insights — Results Writeup

> Snapshot of the eight Task 1.4 analyses as rendered on the **Structured Insights** dashboard
> page, against the current sampled dataset (99 documents, 20 `fully_executed_agreement` rows).
> Every number here is pure SQL/pandas over `data/contracts.db` — no LLM involved. Results will
> shift as more contracts are sampled and extracted; this is a point-in-time read, not a permanent
> reference.

---

## 1. Renewal Cliff Dashboard

**What it is.** Every contract whose `contract_end_date` falls within the next 365 days, ranked by
urgency (days until expiry), rendered as a bar chart colored by `service_category`.

**Business purpose.** Most portcos have zero centralized visibility into upcoming contract
expirations. This is the single highest-urgency view in the platform — it converts a buried date
field into a ranked action list, so renewals get negotiated proactively instead of under deadline
pressure (which is exactly when the incumbent vendor holds maximum leverage).

**Result on the dashboard.** Two contracts currently fall inside the 365-day window:

| Vendor | Contract # | End Date | Days Left | Value | Category |
|---|---|---|---|---|---|
| Accent Landscape Design, Inc. | 24262-2 | 2026-09-10 | 80 | $14,315 | Facilities Maintenance |
| Ciorba Group | 22128-1 | 2026-10-18 | 118 | — (not specified) | Professional Services |

Small list, low dollar exposure — reflects the sample's limited `fully_executed_agreement`
coverage (only 20 of ~99 sampled documents are that doc type) more than it reflects the portfolio's
actual renewal risk. A full extraction pass would surface a longer, higher-stakes list.

---

## 2. Auto-Renewal Liability Scan

**What it is.** Contracts flagged `auto_renewal_flag = 1`, with a calculated "act by" cancellation
deadline (`contract_end_date` minus `termination_notice_days`), flagged urgent if that deadline
falls within 30 days.

**Business purpose.** A single missed cancellation window on a multi-year auto-renewing contract
can lock in six figures of unwanted spend with no recourse. This turns a passive, easy-to-miss risk
into a dated, explicit to-do list — the kind of thing that should never depend on someone
remembering a clause buried on page 14.

**Result on the dashboard.** **Empty** — no contract in the current sample has
`auto_renewal_flag = 1`. This is a sparsity artifact of the sample and/or the extraction's
inference confidence on this field, not a claim that no auto-renewing contracts exist in the full
corpus. `auto_renewal_flag` is itself an LLM-inferred field (not a direct text extraction, per
`extraction_schema.md`), so this view is explicitly framed in-app as a triage aid, not an audit.

---

## 3. Spend Concentration Map

**What it is.** Total `total_contract_value` summed per vendor, restricted to
`fully_executed_agreement` rows only (to avoid double-counting the same award across its renewal/
amendment lifecycle documents), ranked with a percent-of-total annotation.

**Business purpose.** Most mid-market portcos have never seen total vendor spend in one place.
This surfaces both single-vendor concentration risk (too many eggs in one basket) and
consolidation leverage (multiple small vendors that could be renegotiated as one larger,
better-priced relationship).

**Result on the dashboard.** Ten vendors currently rank, with spend heavily concentrated at the
top:

| Vendor | Spend | % of Total |
|---|---|---|
| HOV Services Inc. | $2,645,271 | 37.6% |
| Burns & McDonnell Engineering Co. | $1,000,000 | 14.2% |
| Black & Veatch Corporation | $1,000,000 | 14.2% |
| Applied Technologies, Inc. | $996,110 | 14.1% |
| Clark Dietz Inc. | $500,000 | 7.1% |
| Ciorba Group Consulting Engineers | $500,000 | 7.1% |
| Appin Associates | $170,169 | 2.4% |
| Conference Technologies Inc. | $98,076 | 1.4% |
| CDM Smith, Inc. | $73,640 | 1.0% |
| Journal Technologies, Inc. | $60,000 | 0.9% |

The top vendor (HOV Services) alone represents over a third of tracked spend — a real
concentration signal even at this sample size. `total_contract_value` is directional where
Exhibit A was blank or redacted (per Task 1.4's caveat), so these are best-available figures, not
audited totals.

---

## 4. True Total Commitment by Contract Family

**What it is.** Per `contract_number`, sums the original award value (`fully_executed_agreement`)
plus all amendment deltas (`modification_amendment` rows), flagging families where amendments
exceed 25% of the original award — "amendment creep."

**Business purpose.** Original award values alone systematically understate real spend once
amendments aren't tracked centrally. Amendment creep is a classic value-destruction pattern in PE
diligence — a contract that looks like a $170K commitment on paper can really be a $217K
commitment once every signed change order is added back in.

**Result on the dashboard.** 15 contract families currently resolve. One is flagged for creep:

- **Appin Associates (contract 22143):** original award $170,169 + amendments $47,082 = **true
  total $217,251** — amendments are **27.7% of the original award**, crossing the 25% creep
  threshold.

The largest true-total family is **HOV Services (contract 19028)**: $2,645,271 original +
$410,418 in amendments = **$3,055,689 true total** — a substantial absolute amendment dollar
figure, but proportionally under the 25% flag (15.5%).

---

## 5. Price Escalation Exposure

**What it is.** Contracts with a risky escalator type (`cpi_capped`, `fixed_percentage`, or
`negotiated_at_renewal`) expiring within the next 365 days, plotted as value vs. days-to-renewal,
colored by escalator type.

**Business purpose.** In an inflationary environment, CPI-linked or uncapped escalators on large,
multi-year contracts translate directly into budget surprises at renewal. This gives a CFO
quantitative footing — "here's what's coming, and here's roughly how much it could move" — before
walking into a renewal negotiation blind.

**Result on the dashboard.** **Empty.** No contract in the current sample both (a) carries one of
the three flagged escalator types and (b) falls within the 365-day renewal window. This reflects
the same small-sample effect as Analysis 1 (limited overlap between "has an extracted escalator
term" and "expires soon") rather than an actual absence of escalation risk in the broader
portfolio.

---

## 6. Procurement Channel Mix

**What it is.** Total spend by `procurement_vehicle` (direct RFP, cooperative/piggyback, sole
source, other), rendered as a donut, plus a ranked table of sole-source and cooperative contracts
specifically — the highest-priority re-bid candidates, since those channels carry the least
competitive pricing pressure.

**Business purpose.** Cooperative ("piggyback") and sole-source awards mean little to no
competitive pressure on price. Flagging exactly which contracts were procured that way — and how
much spend runs through each channel — gives procurement a concrete, prioritized re-bid list for
the next renewal cycle.

**Result on the dashboard.**

| Channel | Spend | % of Total |
|---|---|---|
| Direct RFP | $19,843,550 | 95.2% |
| Unknown (not specified) | $493,573 | 2.4% |
| Other | $427,415 | 2.1% |
| Sole Source | $73,640 | 0.4% |

The portfolio is overwhelmingly direct-RFP — a healthy competitive-procurement signal at this
sample size. One sole-source contract surfaces as a re-bid candidate: **CDM Smith, Inc.** (contract
25263, $73,640, Professional Services).

---

## 7. Vendor Consolidation Opportunity Map

**What it is.** Per `service_category`: distinct vendor count, total spend, and average contract
value — a fragmentation signal flags categories with many vendors and low average value per vendor
(the pattern that indicates lost volume-discount leverage).

**Business purpose.** A category with eight small vendors and no master agreement is leaving
money on the table — duplicated compliance overhead, no negotiating leverage, no volume pricing.
This view turns "we have a lot of vendors" into a specific, rankable consolidation target list.

**Result on the dashboard.** Only two categories currently have populated `fully_executed_agreement`
rows:

| Category | Vendors | Total Spend | Avg. Value | Fragmentation |
|---|---|---|---|---|
| Professional Services | 8 | $6,885,190 | $529,630 | Medium |
| Technology/Software | 2 | $158,076 | $79,038 | Medium |

Professional Services is the clear consolidation target by vendor count (8 distinct vendors) —
worth a closer look once the full corpus is extracted, since 8 vendors in one category is exactly
the fragmentation pattern this view is designed to catch.

---

## 8. Incumbent Dependency Flag

**What it is.** Per `contract_number`, counts `renewal_letter` rows (each renewal = one more year
without competitive re-bid), flags 3+ renewals as a "stale incumbent," ranked by renewal count then
current value. The originating document falls back to `award_letter` when no
`fully_executed_agreement` row exists, so contracts whose primary document was classified
differently aren't silently excluded.

**Business purpose.** A vendor relationship renewed year after year without ever being re-bid
tends to drift toward convenience pricing above market. This surfaces exactly which relationships
have gone the longest without competitive pressure — a concrete, ranked re-bid priority list for
procurement.

**Result on the dashboard.** Four contracts currently resolve, one flagged as a stale incumbent:

| Vendor | Contract # | Renewals | Current Value | Stale Incumbent |
|---|---|---|---|---|
| Imagesoft, Inc. | 18018 | **3** | — (not specified) | **Yes** |
| Evoqua Water Technologies LLC | 21003 | 2 | $231,120 | No |
| Core & Main | 23036 | 1* | $652,152 | No |
| Conference Technologies Inc. | 16069 | 1 | $98,076 | No |

**Imagesoft (18018)** is the clearest incumbent-dependency signal in the current sample — three
consecutive renewals with no re-bid, the exact pattern this analysis exists to catch.

\* Core & Main's renewal count is understated: the underlying documents include three renewal
letters (2024–25, 2025–26, 2026–27), but two were misclassified upstream by the extraction
pipeline as `vendor_disclosure_statement` rather than `renewal_letter` and so aren't counted here.
This is a classification-accuracy gap in the extraction step (Task 2.2), not a flaw in this
analysis's logic — worth a closer look at the classifier prompt for renewal letters specifically.

---

## Cross-Cutting Notes

- **Sample-size effects dominate several "empty" results** (Analyses 2 and 5). These are read
  honestly as "no qualifying contracts in the current 99-document sample," not as evidence the
  underlying risk doesn't exist in the full ~389-document corpus.
- **`fully_executed_agreement` coverage is the limiting factor** for Analyses 1, 3, 4, 5, 6, and 7,
  all of which restrict to (or heavily weight) that doc type for `total_contract_value` accuracy.
  Only 20 of 99 sampled documents carry that tag.
- **Two known upstream data-quality gaps surfaced while validating these results:** the Core & Main
  renewal-letter misclassification noted above (Analysis 8), and a related fix already applied to
  Analysis 8 itself — it previously excluded any contract whose originating document was tagged
  `award_letter` instead of `fully_executed_agreement`, which silently dropped real incumbents
  (Imagesoft included) from the chart entirely. Worth scanning the other seven analyses for the
  same class of doc-type-assumption gap as more of the corpus gets extracted.
