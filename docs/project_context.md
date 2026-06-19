# Berkshire Partners — VP Data Science Take-Home Project

> **Purpose of this project:** Self-contained workspace for the Berkshire Partners take-home assignment and the follow-on live walkthrough. This document is the canonical context. The runnable application (pipeline, database, web UI, evaluation) is built separately in Claude Code / VS Code; this project is for thinking, narrative, strategy, and walkthrough prep.

---

## 1. Candidate Profile

**Name:** Joe Loftus
**Current role:** Data Scientist III, Chewy (Seattle, WA)

### Career background

**Bain & Company — Boston, MA (Sep 2018 – Aug 2021)**
- *Senior Data Scientist* (Sep 2018 – Apr 2020), then *Data Science Manager* (Apr 2020 – Aug 2021)
- Spent ~3 years on the data science team across industries; substantial work supporting the **Private Equity practice** on due diligence and post-acquisition operational work
- Led data science workstreams: project scoping, oversight of junior team members, translating complex analytical findings into recommendations for non-technical client leadership
- Led geospatial analyses across multiple PE engagements (retail site performance, competitor proximity, customer density, growth-opportunity quantification)
- Built a custom network-analysis optimization algorithm for an autonomous-vehicle client to evaluate US deployment areas — included in the client's investor materials and led to repeat business
- Built an ML demand-forecasting system for a manufacturing client (equipment utilization 80% → 90%); automated a customer-segmentation pipeline reducing time-to-insight from days to hours

**Ubiety Technologies — Director of Data Science (2021 – 2024)**
- "AI startup in home security." First data science hire; built the function from scratch — data warehousing through full ML deployment
- Grew the team to four; promoted to the executive team
- Represented data and AI strategy to the **board, investors, and enterprise partners**; conference appearances and live partner sales situations

**Chewy — Data Scientist III (2025 – present)**
- Hands-on **GenAI work at scale**; building and deploying enterprise AI solutions with modern tooling

### Education
- **MS, Analytics** — NC State University, Institute for Advanced Analytics
- **BS, Applied Mathematics** — University of New Hampshire

### Differentiators relevant to this role
- **PE-native experience:** Bain work was largely PE due diligence and value creation — the exact intersection this role lives at.
- **External / stakeholder-facing depth:** Bain was almost entirely client-embedded; Ubiety involved board presentations, investor communications, conference talks, and live partner sales. Strong fit for a role requiring stakeholder influence and C-suite communication.
- **Full-lifecycle builder:** Built a data/ML function from zero at Ubiety, then moved to hands-on GenAI at scale at Chewy — combines the consulting instinct, the builder mentality, and current AI craft.

---

## 2. The Role & Firm

**Firm:** Berkshire Partners — private equity firm, 200 Clarendon Street, Boston, MA
**Role:** VP-level Data Science position within the **Portfolio Support Group (PSG)** — the operating-support arm where the data science / AI function sits. The mandate centers on building AI/ML proofs of concept and point solutions for portfolio companies and proving value to business owners.

### Firm culture / structure (useful for framing)
- Berkshire deliberately has **no CEO**; the firm is led collectively by Managing Directors via three committees (Governance & Policy, Private Equity, Stockbridge Executive). Decisions are made collaboratively.
- Cultural implication: this rewards people who build consensus and communicate clearly across stakeholders — lean into the collaborative, business-outcome-oriented framing.
- The DS/AI function is a relatively recent, deliberate build-out within PSG.

---

## 3. Interview Progress

### Completed rounds (all went well)
1. **HR / recruiter screen**
2. **Head of DS/AI**
3. **Data Scientist on the team**

### Current stage
**Take-home project** (spec below), evaluated together with a **live walkthrough** as a single exercise.

### Broader people / org context — from prior prep research; verify, do not treat as confirmed interviewers
- **Marni Payne** — Managing Director; leads the Portfolio Support Group; long-tenured (since 2000, MD since 2015); ex-McKinsey; Dartmouth AB / Harvard MBA. Consumer/retail board background. Likely a senior sponsor of the DS/AI build-out and a plausible later-round interviewer; tends to probe collaboration, real operating wins over frameworks, and consumer/retail use cases.
- **DS/AI build-out leadership** — recent senior AI hire and an internal promotion drove the function's expansion within PSG (names referenced in prep: Richard Lichtenstein, Limor).
- **Potential peers / reports** — Justin Kaashoek (Data Scientist); Adi Gupta (Operating Partner, Portfolio Support).
- **Other leadership names** — Chris Hadley (Co-Managing Partner, healthcare-focused); Terry Thompson (MD & COO, has spoken publicly about AI).

---

## 4. The Take-Home Assignment

**Submission:** via email, **24 hours ahead of the walkthrough.**

### Stated goal
Build and test a small generative-AI pipeline to structure and interact with content from contract documents, then reason through evaluation frameworks and other use cases for a sample business. It is an illustrative PoC of what the team might build for a portfolio company.

### What they're evaluating
- **Hustle quality & execution velocity** — get to a working PoC; scope and ship under real time constraints.
- **Ability to defend decisions** — walk through architecture, explain the evaluation approach, and honestly characterize *proven vs. assumed*.
- **Problem framing & time budgeting** — structure ambiguity, decide where to go deep vs. shallow, articulate trade-offs.
- **Communication** — with technical teams, non-technical stakeholders, and C-suite leaders.
- **Evaluation rigor** — prove value to business owners and build trust.

### Part 1

**Task 1 — Build a GenAI pipeline that can:**
- **Structure contract content into a table.** After reviewing the contracts, pick the **10–20 most important features** to extract for meaningful business decisions. A few hundred contracts are provided; **process ~100** and discuss the process in detail (no meaningful infra cost expected). Be ready to explain *why* those features, how they'd drive a high-ROI downstream analysis, key architectural decisions, and what would change in a hardened production deployment.
- **Let users chat with the contracts** via a web UI / chat interface for non-technical users. Deal-team members without coding skills should ask natural-language questions, retrieve relevant clauses, and perform/visualize basic data cuts (e.g., "Which contracts have auto-renewal clauses?"). Think about the **last-mile problem** — the right interface. MS Teams integration is a plus, not required.
- **Be runnable** — hosted or with clear local-run instructions — and rely on a **database we can examine together**. Include basic error handling and graceful failure modes (malformed document, query returns no relevant context).
- **Include a README** covering: architecture and key decisions; known limitations; top 2–3 improvements you'd prioritize next.
- Be ready to discuss: non-negotiable features vs. roadmap items; how you'd harden this into a durable tool a deal team could rely on; guardrails / human-in-the-loop checkpoints; and how you'd handle **confidentiality for sensitive legal documents**.

**Task 1b — A single C-suite-ready executive summary slide** highlighting a few key insights from the contracts and the implications for management.

**Task 2 — Build and run a lightweight evaluation of the pipeline.**
- Prepare **5–10 test cases with ground-truth answers**; share results alongside the evaluation code.
- Be ready to discuss: how you'd measure table-extraction accuracy and chat relevance/effectiveness; which metrics and why; how you'd monitor output quality in production and detect model degradation (prompt drift, changes in document format or clause language).

### Part 2
A **portfolio company value chain** is provided (a logistics / CPG-distribution business): Demand gen & early pipeline → Price / Contracting → Inbound logistics → Warehouse mgmt → Building / loading truck → Route design & delivery → Billing & reconciliation. Key functions: Marketing, Sales, Finance, Operations (warehouse, logistics, transport), Customer service.

Propose **other high-impact GenAI/ML use cases** for the team. Present a list of ideas, a prioritization, and a project-proposal approach — **C-suite ready.**

### Deliverables
- **Presentation:** a few slides per task; **≤7 slides for Part 1, ≤2 for Part 2**; each findings slide must carry a clear message/insight.
- **Code:** the Python used for analysis and the pipeline, with documentation.
- **Structured contract output:** a hosted file or database, **one row per contract**, columns of your choosing.

---

## 5. Working Setup & Approach

### Tooling split
- **Build (Claude Code in VS Code):** the runnable app — extraction pipeline, structured database, chat/web UI, evaluation harness, repo README. Contracts and code live locally on disk.
- **This project (Claude web):** the thinking — feature selection and rationale, executive-slide narratives, Part 2 use-case strategy, and walkthrough/defense prep.
- **Source of truth:** files in the repo (e.g., `CLAUDE.md`, README) keep the build and the thinking in sync, since Claude Code and this project are separate contexts.

### Constraints & priorities
- **Time budget: ~10–12 hours total.** This is unpaid work; a lean, credible PoC is the goal, not a gold-plated app.
- They explicitly reward **velocity** and **honest "proven vs. assumed"** framing. Sharp feature selection, real evaluation rigor, and a crisp C-suite story are the differentiators — likely more than app polish.
- Suggested stack for speed: local PDF parsing + a cheap LLM for extraction; SQLite for the structured table (a DB they can examine); a vector store for retrieval; Streamlit for the web UI.

### First step
A **~20-minute scoping and feature-selection pass** before writing any code — that decision drives the entire build. (Prerequisite: open the sample contracts to see what kind of documents they actually are; that changes which features are worth extracting.)