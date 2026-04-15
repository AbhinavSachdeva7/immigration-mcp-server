# Second Discussion Context: Architecture Deep Dive (2026-03-14)

## 1. Scope Decisions (Finalized)

### What We're Building
An **MCP Server** that provides two core capabilities:
1. **SOC Code Discovery** — Helps users find the correct Standard Occupational Classification code for their job, along with prevailing wage levels from OFLC data.
2. **Legal Document Navigation** — Allows users (F-1 students, H-1B workers, etc.) to navigate and understand the Federal Register and USCIS Policy Manual through deterministic, cited answers.

### What We're NOT Building
- No probability calculator for H-1B lottery odds (too many variables).
- No real-time Federal Register updates — **snapshot approach** (index today's documents once).

---

## 2. Architecture Decisions (Finalized)

### Database
- **PostgreSQL + pgvector** (single database for everything)
- pgvector used only as a potential future optimization ("semantic jump"), **NOT used in v1**

### LLM for Index Construction
- **Gemini 2.5 Flash-Lite** (free tier: 15 RPM, 1,000 RPD, 250K TPM)
- Or any free flash-tier model available at build time
- Budget: **< $10 total** for testing; index construction should be free

### MCP SDK
- **Official Anthropic MCP Python SDK** (`mcp` package) with `FastMCP`
- Backend: **FastAPI + Python**

### Deployment
- **Docker Compose** — localized, user brings their own API key

---

## 3. Core Pattern: LLM-Guided Tree Traversal (PageIndex)

Both SOC codes and legal documents use the same fundamental pattern:

### The Pattern
1. Data is stored as a **tree** in PostgreSQL (parent_id / child_id relationships)
2. Each node has: `title`, `summary`, `full_text` (leaves only), `citation` (legal), `metadata`
3. At **query time**, the LLM traverses **top-down**: reads children summaries, selects which branch(es) to explore
4. **Citations are deterministic** — stored as SQL metadata on each node, never LLM-generated
5. **Human-in-the-Loop (HITL)** — at every level, the LLM presents plausible options to the user; the user chooses

### Build Strategy
- **Legal documents (Federal Register, USCIS Manual):** Built **bottom-up** with LLM summarization. Clauses → summarized → subsections → summarized → sections → summarized → root. Map-reduce with `asyncio`.
- **SOC codes (O*NET + OFLC):** Built **top-down** from existing data. O*NET already provides titles, descriptions, tasks, tools. No LLM summarization needed — just load the tab-delimited files into Postgres.

---

## 4. SOC Code Discovery — Always-HITL Tree Traversal

### Critical Design Decision: HITL at Every Level, Not Just at Leaf

The user may be vague ("I work with computers") or ambiguous ("I build machine learning systems" — could be software OR hardware). Instead of the LLM guessing, **every level** presents options to the user.

### The Flow

```
Step 1: User provides description
  "I build machine learning systems"

Step 2: MAJOR GROUP level (~23 groups)
  LLM reads all 23 major group summaries + user description
  LLM selects PLAUSIBLE matches (recall-first, not precision-first)
  
  PRESENTS TO USER:
  "Based on your description, these groups could apply:
   [A] 15-0000: Computer and Mathematical Occupations
   [B] 17-0000: Architecture and Engineering Occupations
   Which is closest to your work?"
  
  User picks: A

Step 3: MINOR GROUP level (~4-6 groups)
  LLM reads minor group summaries + O*NET task descriptions
  LLM selects plausible matches
  
  PRESENTS TO USER with distinguishing details:
  "Under Computer Occupations:
   [A] 15-1200: Computer Occupations — writing software, managing systems
   [B] 15-2000: Mathematical Science — statistical models, data analysis
   Which aligns more with your daily work?"
  
  User picks: B

Step 4: DETAILED OCCUPATION level (~2-4 occupations)
  LLM enriches each candidate with O*NET data (tasks, tools, education)
  
  PRESENTS TO USER with full context:
  "Here are the specific occupations:
   [A] 15-2051 Data Scientists
       Tasks: Apply ML algorithms, build predictive models
       Tools: Python, TensorFlow, SQL
   [B] 15-2041 Statisticians
       Tasks: Apply statistical theory, design surveys
       Tools: R, SAS, SPSS
   Which matches your role?"
  
  User picks: A → CONFIRMED: 15-2051

Step 5: WAGE LOOKUP (deterministic SQL)
  Fetch OFLC wage levels for 15-2051 × user's MSA
```

### How the LLM Selects Which Options to Present

This is the critical capability question. The mechanism:

1. **Node summaries are pre-built and stored in SQL.** Each node has a summary (from O*NET descriptions or LLM-generated for legal text).
2. **The LLM receives ALL children at the current level** along with the user's description in a single prompt.
3. **The prompt asks for RECALL, not PRECISION:** "Which of these categories COULD POSSIBLY apply to someone who describes their work as '{description}'? Include any that are even remotely relevant. Err on the side of inclusion."
4. **The LLM returns a ranked list** of plausible matches with short explanations of why each was included.
5. **These are presented to the user** for selection.

### Can the LLM Handle Nuanced Distinctions?

**At coarse levels (Major/Minor Groups):** Yes, easily. The categories are distinct enough that even a free-tier flash model can filter 23 → 2-3 reliably.

**At fine-grained levels (Detailed Occupations):** This is where it gets hard. The solution is **enrichment**:
- Don't just show the LLM the SOC title ("Software Developers" vs. "Software QA Analysts")
- Show the LLM the **O*NET task statements** for each candidate
- Task statements are highly specific and differentiating
- The LLM doesn't need to make the final call — it just needs to include all plausible options. The USER makes the final decision.

**Failure mode and mitigation:** If the LLM excludes a correct option at any level, the user will reach the wrong leaf. Mitigation:
- Always include an "None of these / Other" option
- If user selects "Other," backtrack one level and expand the search (present ALL children, not just LLM-filtered ones)
- At the detailed level, always present ALL options (usually only 2-4), don't filter

---

## 5. Legal Document Navigation — LLM Tree Traversal with Citations

### The Flow

```
Step 1: User asks a question
  "Are there fee exemptions for nonprofit H-1B petitions?"

Step 2: ROOT level
  LLM reads section summaries, picks relevant branch(es)
  Presents to user: "I found relevant content in:
   [A] 8 CFR §214.2(h) — H-1B temporary workers
   [B] 8 CFR §214.2(h)(19) — Fee requirements
   Would you like me to explore these?"

Step 3: Descent to LEAF
  LLM navigates down, presenting options at each level
  User can follow the suggested path or explore alternatives

Step 4: LEAF reached
  Return full legal text + deterministic citation:
  "8 CFR §214.2(h)(19)(ii)(B)"
  Citation assembled from SQL node metadata, NOT generated by LLM
```

---

## 6. Data Sources

| Source | Format | Use |
|--------|--------|-----|
| O*NET 30.2 Database | Tab-delimited text (40 files) | SOC tree: occupations, tasks, tools, descriptions |
| OFLC Wage Data (Jul 2025–Jun 2026) | CSV/Excel | Wage levels by SOC × MSA |
| Federal Register (Feb 2026 Final Rule) | XML | H-1B wage-weighted selection rule |
| USCIS Policy Manual | Web/PDF (all 12 volumes) | Comprehensive immigration law reference |

---

## 7. Database Schema (Proposed)

### Tree Structure (shared by legal docs and SOC codes)
```sql
CREATE TABLE nodes (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,         -- 'federal_register', 'uscis_manual', 'soc'
    parent_id INTEGER REFERENCES nodes(id),
    level INTEGER NOT NULL,      -- depth in tree (0 = root)
    title TEXT NOT NULL,
    summary TEXT,                -- LLM-generated (legal) or O*NET native (SOC)
    full_text TEXT,              -- leaf nodes only
    citation TEXT,               -- legal citation path
    metadata JSONB,              -- flexible: page numbers, SOC code, etc.
    embedding vector(768)        -- pgvector, for future semantic jump (v2)
);
```

### O*NET Data (loaded directly from bulk download)
```sql
CREATE TABLE onet_occupations (
    soc_code VARCHAR(10) PRIMARY KEY,
    title TEXT,
    description TEXT
);

CREATE TABLE onet_task_statements (
    id SERIAL PRIMARY KEY,
    soc_code VARCHAR(10) REFERENCES onet_occupations(soc_code),
    task TEXT,
    task_type VARCHAR(20)  -- 'Core' or 'Supplemental'
);

CREATE TABLE onet_tools_technology (
    id SERIAL PRIMARY KEY,
    soc_code VARCHAR(10) REFERENCES onet_occupations(soc_code),
    t2_type VARCHAR(20),   -- 'Tool' or 'Technology'
    t2_example TEXT,
    hot_technology BOOLEAN
);
```

### OFLC Wage Data
```sql
CREATE TABLE oflc_wages (
    id SERIAL PRIMARY KEY,
    soc_code VARCHAR(10),
    soc_title TEXT,
    msa_area TEXT,
    wage_level INTEGER,     -- 1, 2, 3, or 4
    hourly_wage DECIMAL,
    yearly_wage DECIMAL
);
```

---

## 8. MCP Tools (Proposed)

```python
@mcp.tool() search_soc(job_description: str) → candidates with O*NET enrichment
@mcp.tool() get_soc_details(soc_code: str) → full O*NET details + wage data
@mcp.tool() get_wage_level(soc_code: str, msa_area: str) → wage levels 1-4
@mcp.tool() search_legal(query: str) → tree traversal into legal docs
@mcp.tool() browse_legal_section(node_id: str) → explore a specific section
```

---

## 9. Open Questions for Next Phase

1. **USCIS Policy Manual ingestion** — How to parse all 12 volumes? Web scraping vs. PDF download? What format is available?
2. **Federal Register XML parsing** — What XML schema does the FR use? How do we map XML elements to tree nodes?
3. **Backtracking UX** — When user selects "None of these" at a SOC level, exact mechanism for widening the search.
4. **MCP tool granularity** — Should tree traversal be one tool (server-side loop with HITL via prompts) or multiple tools (client LLM drives the loop)?
5. **Testing strategy** — How to test tree traversal without spending LLM tokens? Mock LLM responses?

---

## 10. Constraints & Safety (Unchanged)

- **Human-in-the-Loop (HITL):** Required at every SOC tree level to avoid misclassification.
- **UPL Compliance:** "Not Legal Advice" disclaimer. All legal answers must include deterministic citations to government sources.
- **Budget:** < $10 total for testing. Index construction uses free-tier LLM.
