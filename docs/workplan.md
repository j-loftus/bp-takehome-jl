# Berkshire Partners Take-Home — Running Workplan

> **Living document.** Updated as decisions are made across chats and Claude Code sessions.
> Status: 🔲 Not started | 🔄 In progress | ✅ Done | 🚧 Blocked

---

## Phase 0: Scoping & Setup
| # | Task | Status | Notes |
|---|------|--------|-------|
| 0.1 | Review assignment spec and project context | ✅ Done | |
| 0.2 | Document corpus recon — understand file types and structure | ✅ Done | ~350 docs; ~6 subtypes; contract # is the linking key |
| 0.3 | Define document taxonomy (doc type classification scheme) | ✅ Done | 6 types: Fully Executed Agreement, Renewal Letter, Modification/Amendment, Award/Intent-to-Award, Vendor Disclosure Statement, Other |
| 0.4 | Sampling strategy + sample validation | ✅ Done | Standalone task — own chat; covers approach design, pros/cons, and validation |

---

## Phase 1: Feature Selection & Schema Design
| # | Task | Status | Notes |
|---|------|--------|-------|
| 1.1 | Define 15-field extraction schema with PE-value rationale | ✅ Done | Core decision — drives everything downstream |
| 1.2 | Map fields to expected doc types (which fields appear in which doc types) | ✅ Done | Determines null patterns in the structured table |
| 1.3 | Define extraction prompt strategy (single prompt vs. typed prompts by doc type) | ✅ Done | Architecture decision for the pipeline |
| 1.4 | Define 2-3 concrete downstream high-ROI analyses the schema enables | ✅ Done | Assignment explicitly probes "how you'd use features for downstream analysis that drives high ROI" — distinct from feature rationale; e.g., renewal/expiry exposure, pricing concentration, auto-renewal liability, vendor consolidation; each analysis should trace back to the fields that power it |

---

## Phase 2: Pipeline Build (Claude Code)
| # | Task | Status | Notes |
|---|------|--------|-------|
| 2.1 | PDF parsing module | ✅ Done | Separate module; PyMuPDF or pdfplumber; flag scanned/image-only docs |
| 2.2 | Document classification module | ✅ Done | Separate module; Step 0 before extraction; detect doc type before applying schema |
| 2.3 | LLM extraction pipeline (structured output) | ✅ Done | One row per document; contract_number as linking key; uses prompt from 1.3 |
| 2.4 | Prompt iteration loop (manual, pre-DB) | ✅ Done | Run extraction on 5-10 docs manually; catch early failure modes; refine prompt before scaling |
| 2.5 | SQLite database setup | ✅ Done | Stand up DB only after prompt quality is acceptable; one table minimum; schema from Phase 1 |
| 2.6 | Vector store / RAG layer | ✅ Done | ChromaDB (local, no infra); chunk docs with overlap; handles semantic queries the structured table can't |
| 2.7 | Chat interface + deployment | 🔲 Not started | Streamlit UI; NL query → structured table OR RAG depending on query type; **must visualize/render data cuts back to user (chart or rendered table), not just text — "perform and visualize basic data cuts" is an explicit spec requirement**; deployment plan: SQLite committed to repo + ChromaDB rebuilt at startup (avoids hosting a separate vector DB); deploy to Streamlit Community Cloud with local fallback; production note: would persist vector store separately; **MS Teams integration deliberately deprioritized vs. core (strong plus, not required) — document the decision + how I'd approach it** |
| 2.8 | Error handling and graceful failure modes | 🔲 Not started | Malformed doc, no relevant context, extraction failure, classification failure |
| 2.9 | README | 🔲 Not started | Architecture, decisions, known limitations, top 2-3 next improvements |

---

## Phase 3: Evaluation (Task 2)
| # | Task | Status | Notes |
|---|------|--------|-------|
| 3.1 | Design eval framework | 🔲 Not started | Two-layer approach: (1) ground truth test set for precision anchoring; (2) LLM-as-judge for scalable coverage across full sample |
| 3.2 | Define metrics | 🔲 Not started | Extraction: field-level accuracy, null rate by doc type; Chat: retrieval precision, answer faithfulness, relevance; judge scores dimensions: faithfulness, completeness, relevance |
| 3.3 | Build LLM-as-judge | 🔲 Not started | Judge prompt takes (question, context, extracted answer) → scores + reasoning; run across full 100-doc extraction and broader query set; surfaces edge cases ground truth set won't catch |
| 3.4 | Build ground-truth test set (5-10 cases) | 🔲 Not started | Manually verified against source docs; anchors the eval; catches gross failures |
| 3.5 | Run eval and record results | 🔲 Not started | Share alongside eval code; report both ground truth accuracy and judge scores |
| 3.6 | Production monitoring approach | 🔲 Not started | Judge prompt as foundation for ongoing monitoring; run on sample of live outputs periodically; alert on score drift; also covers prompt drift and doc format changes |

---

## Phase 4: Presentation & Narrative (Claude Web)
| # | Task | Status | Notes |
|---|------|--------|-------|
| 4.1 | Task 1b — C-suite executive summary slide | 🔲 Not started | Single slide; key insights + management implications; **tie insights to the portco business context (logistics/CPG value chain from Part 2) so Task 1b and Part 2 share a narrative spine — contracts likely belong to this business** |
| 4.2 | Part 1 slides (≤7) — architecture, decisions, eval results | 🔲 Not started | Each slide needs a clear message/insight; **ensure eval results survive as a dedicated findings slide with a clear message (not just mentioned in passing) within the ≤7-slide budget** |
| 4.3 | Part 2 slides (≤2) — GenAI/ML use cases for logistics/CPG portco | 🔲 Not started | Prioritized list + project proposal approach; C-suite ready |

---

## Phase 5: Walkthrough Prep
| # | Task | Status | Notes |
|---|------|--------|-------|
| 5.1 | Defense prep — architectural decisions Q&A | 🔲 Not started | Why those features, why that stack, proven vs. assumed |
| 5.2 | Defense prep — hardening / production readiness | 🔲 Not started | Guardrails, human-in-the-loop checkpoints; **articulate non-negotiable features vs. roadmap items — explicit assignment prompt, distinct from README's "top 2-3 improvements"** |
| 5.3 | Defense prep — confidentiality & sensitive document handling | 🔲 Not started | Explicitly called out in assignment; covers data access controls, PII handling, legal doc sensitivity, audit trails |
| 5.4 | Defense prep — people/org context | 🔲 Not started | Berkshire stakeholders, likely questions by persona |

---

## Key Decisions Log
| Decision | Choice | Rationale |
|----------|--------|-----------|
| Database | SQLite | Lightweight, inspectable, no infra cost, meets "DB we can examine together" requirement |
| Schema unit | One row per document (not per contract) | PoC-appropriate; contract_number enables grouping; avoids complex stitching |
| Doc classification | Step 0 in pipeline before extraction | Prevents wrong schema applied to wrong doc type; enables typed prompts |
| Sample size | 100 docs, stratified | Assignment requirement; stratify for representativeness |
| UI | Streamlit | Speed; non-technical user accessible; meets spec |
| MS Teams integration | Deprioritized for PoC | Strong plus, not required; core pipeline + UI take priority under the time budget; roadmap item |

---

*Last updated: Session 2 — workplan gap review complete; added downstream-analysis task (1.4), visualization + Teams notes (2.7), non-negotiable/roadmap framing (5.2), business-context + eval-slide notes (Phase 4)*