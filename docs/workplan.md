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
| 2.7 | Chat interface + deployment | ✅ Done | Streamlit UI; NL query → structured table OR RAG depending on query type; **must visualize/render data cuts back to user (chart or rendered table), not just text — "perform and visualize basic data cuts" is an explicit spec requirement**; deployment plan: SQLite committed to repo + ChromaDB rebuilt at startup (avoids hosting a separate vector DB); deploy to Streamlit Community Cloud with local fallback; production note: would persist vector store separately; **MS Teams integration deliberately deprioritized vs. core (strong plus, not required) — document the decision + how I'd approach it** |
| 2.8 | Error handling and graceful failure modes | ✅ Done | Malformed doc, no relevant context, extraction failure, classification failure |
| 2.9 | README | ✅ Done | Architecture, decisions, known limitations, top 2-3 next improvements |

---

## Phase 3: Evaluation (Task 2)
| # | Task | Status | Notes |
|---|------|--------|-------|
| 3.1 | Design eval framework | ✅ Done | Two-layer: ground truth anchor + LLM-as-judge scale layer, bridged by a judge-vs-ground-truth calibration number; spec in `docs/evaluation.md` |
| 3.2 | Define metrics | ✅ Done | `eval/scoring.py` — six-bucket field scoring, field accuracy + hallucination rate (headline), classification accuracy + confusion matrix, inferred-field accuracy reported separately |
| 3.3 | Build LLM-as-judge | ✅ Done | `eval/judge.py` — extraction faithfulness (text + vision path for scanned docs) and chat faithfulness/relevance judges, `temperature=0`, batch-capable with `--sample N` |
| 3.4 | Build ground-truth test set | ✅ Done | `eval/ground_truth_extraction.json` (6 docs), `eval/ground_truth_classification.json` (24 docs), `eval/chat_cases.json` (8 cases) — fully labeled and validated against `eval/scoring.py`'s type rules |
| 3.5 | Run eval and record results | ✅ Done | `python -m eval.run_eval` — classification 58.3% (n=24), extraction field accuracy 93.3% / hallucination 6.1%, judge-ground-truth agreement 89.1% (n=55); results in `outputs/eval_report.md`. Judge surfaced two real findings beyond ground truth: a document-level contract-number inconsistency on `2023_10_05_Contract_22143_Modification_1_EXECUTED.pdf` and a defensible `service_category` disagreement on the same doc |
| 3.6 | Production monitoring approach | ✅ Done | `eval/monitoring.py` — `monitoring_snapshot()` + `compare_to_baseline()` reusing `scoring.py`/`judge.py`; logged drift alerts, no scheduler/storage (PoC scope) |

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