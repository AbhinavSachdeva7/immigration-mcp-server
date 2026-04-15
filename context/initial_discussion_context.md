# Project Context: Agentic Immigration & Wage Navigator (2026)

## 1. Project Overview

A specialized AI agent system designed to navigate **US Immigration Law**, **Federal Register** updates, and **Department of Labor (OES/O*NET)** wage data. The goal is to provide deterministic, grounded answers regarding the **FY 2027 H-1B Wage-Weighted Lottery** and job classification.

## 2. Technical Architecture

* **Protocol:** **Model Context Protocol (MCP)**. The system acts as a "Universal Adapter" allowing any LLM (Claude, GPT, etc.) to use the custom tools.
* **Indexing Strategy:** **PageIndex (Tree-based Indexing)**.
* Moved away from flat Vector RAG to preserve legal hierarchy.
* **Bottom-Up Build:** Document $\rightarrow$ Paragraph Summaries $\rightarrow$ Subsection Summaries $\rightarrow$ Root Node.
* **Mechanism:** The LLM navigates the tree logically (Top-Down) rather than semantically searching "fuzzy" chunks.


* **Database:** **Hybrid SQL + Vector**.
* **Postgres/SQLite:** Stores the hierarchical tree (Parent/Child keys for SOC codes and Law chapters).
* **TurboPuffer (or similar):** Used only for the initial "semantic jump" to find the starting node in the tree.


* **Deployment:** **Docker-Compose**. Localized hosting to allow users to "Bring Your Own API Key."

## 3. Data Sources (2026 Status)

* **SOC/O*NET:** O*NET 30.2 Database (Released Feb 2026).
* **Wage Data:** OFLC/DOL 2026 Public Disclosure files (CSV/Bulk).
* **Legal Text:** Federal Register (XML/API) and USCIS Policy Manual.
* **Key Regulation:** The **Feb 27, 2026 Final Rule** on Wage-Weighted Selection (Level 1 = 1 entry, Level 4 = 4 entries).

## 4. Key Workflows

1. **SOC Discovery:** User provides tasks $\rightarrow$ Agent uses a semantic search to find the "Major Group" $\rightarrow$ Agent uses the PageIndex/Tree to interview the human and distinguish between similar computer-related SOC codes.
2. **Wage/Lottery Calculation:** Once a SOC code is confirmed, the agent fetches specific 2026 OES wage levels for the user’s MSA (Metropolitan Statistical Area) and calculates lottery entry weights.
3. **Legal Navigation:** Uses the PageIndex to browse the Federal Register XML nodes to find specific clauses (e.g., the new $100k fee exemptions).

## 5. Current Implementation Stage

* **Architecture:** Finalized.
* **Data Strategy:** Transitioned from scraping to **Bulk Download + Async Processing**.
* **Build Method:** Map-Reduce style parallelization (using `asyncio`) to generate hierarchical node summaries.

## 6. Constraints & Safety

* **Human-in-the-Loop (HITL):** Required for final SOC selection to avoid misclassification.
* **UPL Compliance:** The system must include a "Not Legal Advice" disclaimer and strictly cite government sources.
