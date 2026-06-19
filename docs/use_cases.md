# Task 1.4 — Downstream Analyses & High-ROI Use Cases

> **Purpose:** Defines the concrete, named analyses that the extraction schema enables. Each analysis maps to a specific business question, a value rationale relevant to a portco operator team, and the fields required to power it. This document is the reference for slide narrative, walkthrough defense prep, and any future dashboard or reporting layer built on top of the SQLite output.
>
> **Framing:** The corpus (Lake County, IL municipal procurement PDFs) is treated as a stand-in for a typical portco's vendor contract archive. The insights below are framed for the portco's internal operator team — procurement managers, CFOs, COOs — not for external competitive intelligence purposes.

---

## Tier 1 — Act Now
*Operationally urgent. Real deadlines, real liability. Lead with these in any C-suite presentation.*

---

### Use Case 1: Renewal Cliff Dashboard

**Business question:** Which vendor contracts expire in the next 90 / 180 / 365 days, and what dollar value is at risk?

**Why it matters:** Most portcos have zero visibility into upcoming expirations until a vendor flags it. A ranked list of "contracts expiring this quarter = $X" creates an immediate procurement calendar and prevents costly last-minute renegotiations where the incumbent holds all the leverage. This is the single most immediately actionable output of the pipeline — it drives a real decision next week, not next quarter.

**Key fields required:**
- `contract_end_date`
- `total_contract_value`
- `vendor_name`
- `service_category`
- `renewal_options`

**How it works:** Filter rows where `contract_end_date` falls within the target window. Join to the most recent `total_contract_value` for that `contract_number`. Sort by value descending. Group by `service_category` for a category-level rollup.

**Output:** Ranked list of expiring contracts with vendor name, end date, total value, category, and remaining renewal options. Visualization: bar chart sorted by days-to-expiry, colored by service category.

---

### Use Case 2: Auto-Renewal Liability Scan

**Business question:** Which contracts renew automatically without affirmative action, and when is the last date to cancel?

**Why it matters:** A single missed cancellation window on a multi-year software or services contract can lock in six figures of unwanted spend. This converts a passive compliance risk into an active, dated to-do list. Most procurement teams managing hundreds of vendor relationships will routinely miss these windows without dedicated tooling.

**Key fields required:**
- `auto_renewal_flag`
- `contract_end_date`
- `termination_notice_days`
- `total_contract_value`
- `vendor_name`
- `contract_number`

**How it works:** Filter `auto_renewal_flag = TRUE`. Calculate the cancellation deadline: `contract_end_date` minus `termination_notice_days`. Flag contracts where the cancellation deadline is within 30 days. Sort by cancellation deadline ascending.

**Output:** Alert list of contracts with imminent cancellation deadlines — vendor name, contract value, "act by" date, and contract end date. Visualization: timeline view with cancellation deadlines highlighted.

**Note:** `auto_renewal_flag` is an inferred field (LLM interprets termination and renewal clause language). Present as a triage and review list, not a definitive audit — surfaces candidates for human confirmation, not a claim of certainty.

---

## Tier 2 — Understand Exposure
*Financial risk quantification. Answers the "how much is at stake?" question for the CFO.*

---

### Use Case 3: Spend Concentration Map

**Business question:** Which vendors represent an outsized share of total spend, and where does single-vendor dependency risk exist?

**Why it matters:** Most mid-market portcos have never seen their total vendor spend in one consolidated view. Surfacing that a single staffing firm represents 35% of total spend — or that three vendors are doing overlapping facilities work — creates immediate consolidation leverage and flags concentration risk before it becomes a crisis.

**Key fields required:**
- `vendor_name`
- `total_contract_value`
- `service_category`
- `contract_number`
- `doc_type`

**How it works:** Aggregate `total_contract_value` by `vendor_name` across fully executed agreements (filter `doc_type = 'fully_executed_agreement'` to avoid double-counting with renewals). Calculate each vendor's share of total portfolio spend. Repeat at the `service_category` level for category-level concentration.

**Output:** Vendor-level spend ranking (top 10–20 vendors by total value, with percentage of total). Category-level heat map. Visualization: horizontal bar chart with concentration percentage annotations.

**Note:** `total_contract_value` has partial coverage on fully executed agreements where Exhibit A is blank or redacted. Numbers are directional, not a complete ledger — disclose this in the walkthrough.

---

### Use Case 4: True Total Commitment by Contract Family

**Business question:** What is the real total spend commitment per vendor, including all amendments above the original award?

**Why it matters:** Original award values significantly understate true vendor spend when amendments are not tracked against the base contract. A contract awarded at $100K with three amendments totaling $180K has a true commitment of $280K — invisible without this aggregation. Amendment creep is a classic value-destruction pattern in portcos without tight procurement controls, and a frequent finding in PE due diligence.

**Key fields required:**
- `contract_number`
- `parent_contract_number`
- `total_contract_value`
- `modification_financial_delta`
- `doc_type`
- `vendor_name`

**How it works:** For each `contract_number`, sum the original `total_contract_value` (from the `fully_executed_agreement` row) plus all `modification_financial_delta` values (from `modification_amendment` rows sharing the same `contract_number`). Flag families where amendment value exceeds original award by more than 25%.

**Output:** Contract family summary table — original award value, total amendment value, true total commitment. Flagged list of contracts where amendments materially exceeded the original scope.

---

### Use Case 5: Price Escalation Exposure

**Business question:** Which contracts carry CPI-linked or uncapped price escalators that represent budget risk at renewal?

**Why it matters:** In an inflationary environment, CPI-linked escalators on large, multi-year contracts can represent material budget surprises at renewal. Identifying these contracts before renewals are negotiated gives the CFO real quantitative context and negotiating leverage — rather than discovering the increase after the contract auto-renews.

**Key fields required:**
- `price_escalator_terms`
- `contract_end_date`
- `total_contract_value`
- `vendor_name`
- `contract_number`
- `service_category`

**How it works:** Filter to contracts where `price_escalator_terms IN ('cpi_capped', 'fixed_percentage', 'negotiated_at_renewal')` and `contract_end_date` is within the next 12 months. Rank by `total_contract_value`. For `cpi_capped` contracts, overlay current CPI to estimate maximum price increase at renewal.

**Output:** List of at-risk contracts with escalator type, current value, estimated renewal cost range, and days to expiry. Visualization: scatter plot with contract value on Y-axis and days-to-renewal on X-axis, colored by escalator type.

---

## Tier 3 — Improve Position
*Strategic levers. Highest value at renewal time and during annual procurement planning cycles.*

---

### Use Case 6: Procurement Channel Mix

**Business question:** What share of total spend went through competitive RFP vs. cooperative piggyback vs. sole source — and where is the portco buying without competitive pressure?

**Why it matters:** Cooperative and sole-source awards mean zero competitive pressure at the point of award. A portco CFO seeing that 35% of vendor spend was procured through non-competitive channels has an immediate strategic lever: re-bid those categories when contracts expire. Cooperative piggyback contracts (Sourcewell, Gordian, GSA) are often sold to buyers as "already competitively priced" but the competition happened elsewhere and may be years stale.

**Key fields required:**
- `procurement_vehicle`
- `total_contract_value`
- `service_category`
- `vendor_name`

**How it works:** Aggregate `total_contract_value` by `procurement_vehicle` across fully executed agreements and award letters. Calculate percentage of total spend by channel. Cross-tab against `service_category` to identify which categories are most reliant on non-competitive sourcing.

**Output:** Donut chart of spend by procurement vehicle. Table of sole-source and cooperative contracts ranked by value — the highest-priority re-bid candidates.

---

### Use Case 7: Vendor Consolidation Opportunity Map

**Business question:** Which service categories have high vendor fragmentation that could be consolidated for better pricing leverage and lower administrative overhead?

**Why it matters:** Fragmented vendor relationships within a single category mean lost pricing leverage, duplicated onboarding and compliance overhead, and no ability to negotiate volume discounts. A portco with eight staffing vendors and no master agreement is leaving money on the table — and creating unnecessary administrative burden for the procurement team.

**Key fields required:**
- `service_category`
- `vendor_name`
- `total_contract_value`
- `contract_number`

**How it works:** For each `service_category`, count distinct `vendor_name` values and sum `total_contract_value`. Calculate average contract value per vendor in the category. Categories with high vendor count and low average contract value are consolidation candidates.

**Output:** Category-level matrix — number of vendors, total category spend, average contract value, and a fragmentation signal. Highlight categories where consolidation to 1–2 primary vendors could reduce overhead and improve pricing leverage.

---

### Use Case 8: Incumbent Dependency Flag

**Business question:** Which vendor relationships have been continuously renewed for 3+ years without a competitive re-bid — and where is the portco most exposed to incumbent pricing drift?

**Why it matters:** Long-running incumbents tend to carry pricing that has drifted above market over time — the "convenience premium" of never having been competed. Flagging stale relationships by renewal count and relationship age surfaces the highest-value re-bid candidates at contract expiration, and gives the procurement team a concrete priority list rather than a gut-feel assessment.

**Key fields required:**
- `contract_number`
- `doc_type`
- `contract_start_date`
- `contract_end_date`
- `renewal_options`
- `vendor_name`
- `total_contract_value`

**How it works:** For each `contract_number`, count the number of `renewal_letter` rows. Each renewal letter represents one year of renewal without re-bidding. Contracts with 3+ renewal letters and an original award date more than 3 years ago are flagged as "stale incumbents." Rank by current `total_contract_value` to prioritize re-bid sequencing.

**Output:** Table of vendors ranked by relationship age (years since original award), with renewal count, current contract value, and last-competed date. Produces a concrete, ranked re-bid priority list.

**Note:** This analysis requires joining across document types — counting `renewal_letter` rows per `contract_number` — rather than reading fields from a single document. It is an example of the contract family structure enabling cross-document inference that no single document could support alone.

---

## Summary Table

| # | Use case | Tier | Primary output |
|---|----------|------|----------------|
| 1 | Renewal cliff dashboard | Act now | Ranked expiring contracts by value and days-to-expiry |
| 2 | Auto-renewal liability scan | Act now | Dated "act by" alert list for imminent auto-renewals |
| 3 | Spend concentration map | Understand exposure | Vendor and category-level share of total spend |
| 4 | True total commitment | Understand exposure | Per-family aggregation of base + amendment value |
| 5 | Price escalation exposure | Understand exposure | At-risk contracts by escalator type and renewal date |
| 6 | Procurement channel mix | Improve position | Spend share by competitive vs. non-competitive channel |
| 7 | Vendor consolidation map | Improve position | Category-level fragmentation score and consolidation targets |
| 8 | Incumbent dependency flag | Improve position | Ranked stale-incumbent list by relationship age and value |