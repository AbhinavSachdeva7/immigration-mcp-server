# Task Status: Immigration MCP Server

**Last Updated:** 2026-03-16
**Current Phase:** SOC Pipeline Fixes (Phase 1 complete) — Legal Pipeline (Phase 2) not started

---

## Project Summary

An MCP (Model Context Protocol) server providing two capabilities:
1. **SOC Code Discovery** — HITL tree traversal through the BLS/O*NET occupation hierarchy to find a user's correct SOC code and prevailing wages
2. **Legal Document Navigation** — Tree traversal through Federal Register and USCIS Policy Manual with deterministic citations

The server is **stateless** — the client LLM (Claude, GPT, etc.) drives all traversal and HITL by calling MCP tools with parameters derived from previous tool responses.

**Distribution model:** GitHub repo + Google Drive links for data files (no cloud hosting).

---

## Architecture at a Glance

```
Client LLM (Claude Desktop / any MCP client)
    │  JSON-RPC over stdio (local) or SSE (Docker)
    ▼
FastMCP Server (src/server.py) — 9 tools, stateless
    │  SQLAlchemy ORM
    ▼
PostgreSQL — 8 tables (nodes, soc_hierarchy, onet_tasks, oflc_wages, etc.)
```

Key design decisions:
- **No vector search in v1** — pure relational tree crawling (PageIndex pattern)
- **Citations are SQL metadata**, never LLM-generated — prevents hallucination
- **UPL disclaimer hardcoded** into every legal tool response string
- **HITL at every SOC level** — LLM presents options, user picks, server returns next level
- **LLM summarization for legal nodes** — bottom-up map-reduce with any free-tier model (not yet implemented)

---

## What Has Been Built (Before Phase 1 Fixes)

| Component | File(s) | Status |
|-----------|---------|--------|
| MCP server with 9 tools | `src/server.py` | Built, had transport bug |
| SQLAlchemy models (8 tables) | `src/db/models.py` | Built, had missing indexes |
| Database connection | `src/db/database.py` | Built, working |
| O*NET ingestion script | `scripts/ingest_onet.py` | Built, had hierarchy + FK bugs |
| OFLC wage ingestion | `scripts/ingest_oflc.py` | Built, untested with real data |
| MSA mapping ingestion | `scripts/ingest_msa.py` | Built, untested with real data |
| Federal Register XML ingestion | `scripts/ingest_legal.py` | Skeleton only — non-functional |
| Docker setup | `Dockerfile`, `docker-compose.yml` | Built, had transport mismatch |
| Python packaging | `pyproject.toml` | Built, had unused deps |

---

## Phase 1: SOC Pipeline Fixes — COMPLETED (2026-03-16)

7 MVP-critical issues were identified during a code review and all fixed.

### Issue 1: MCP Transport Misconfiguration
**Problem:** `mcp.run()` defaulted to stdio. `docker-compose.yml` exposed port 8000 (implying HTTP). Server would hang in Docker because nothing listens on that port — stdio reads from stdin which Docker never provides.
**Fix:** Added `MCP_TRANSPORT` env var. Defaults to `stdio` for local/Claude Desktop. Docker sets `MCP_TRANSPORT=sse`.
**Files changed:** `src/server.py` (lines 262-267), `docker-compose.yml`, `.env.example`

### Issue 2: O*NET Hierarchy Empty at Level 0
**Problem:** `get_soc_major_groups()` queries `SOCHierarchy.level == 0`. O*NET's "Occupation Data.txt" only contains detailed occupations (e.g., `15-1252.00`), NOT Major Group rows like `15-0000`. So the query returned empty — the entire SOC discovery flow was dead.
**Fix:** Rewrote `scripts/ingest_onet.py` to first ingest the BLS SOC structure file (`soc_structure.csv`) which contains all Major/Minor/Broad/Detailed groups (levels 0-3), THEN load O*NET extensions (level 4) on top.
**New function:** `ingest_soc_structure()` — reads BLS CSV, sorts by level, inserts level-by-level (0→1→2→3) so parents always exist before children.
**Data dependency:** User must download BLS 2018 SOC structure CSV into `./data/onet/soc_structure.csv`. See `DATA_SOURCES.md`.
**Files changed:** `scripts/ingest_onet.py` (complete rewrite)

### Issue 3: Foreign Key Violations During O*NET Ingestion
**Problem:** Original script inserted rows in file order. If child appeared before parent, FK constraint on `parent_soc_code` would crash.
**Fix:** Two-part solution: (1) BLS structure loaded first guarantees parents for levels 0-3 exist. (2) O*NET level-4 insertions check if parent exists; if not, insert with `parent_soc_code=None` and log a warning instead of crashing.
**Files changed:** `scripts/ingest_onet.py` (same rewrite as Issue 2)

### Issue 4: Missing Composite Database Index
**Problem:** `get_wage_data` filters on `(soc_code, msa_area)` simultaneously. Individual indexes on each column don't help — Postgres picks one index and scans the rest. With ~2M rows, this is ~500ms per lookup instead of ~5ms.
**Fix:** Added composite index `ix_oflc_wages_soc_msa` on `(soc_code, msa_area)`. Also added index on `Node.parent_id` for tree traversal queries.
**Files changed:** `src/db/models.py` (lines 93-97, line 15)

### Issue 5: UPL Disclaimer Missing from `get_legal_citations`
**Problem:** `read_legal_node` and `get_legal_leaf` wrapped responses with UPL disclaimer. `get_legal_citations` did not — compliance gap.
**Fix:** Added `_wrap_legal_response()` to `get_legal_citations` return.
**Files changed:** `src/server.py` (line 242)

### Issue 6: Unused Dependencies
**Problem:** `fastapi` and `uvicorn` listed in `pyproject.toml` but never imported. FastMCP handles its own server.
**Fix:** Removed both from dependencies.
**Files changed:** `pyproject.toml`

### Issue 7: No Data Download Documentation
**Problem:** Ingestion scripts expect `./data/onet/`, `./data/oflc/`, etc. but no documentation on where to get the files.
**Fix:** Created `DATA_SOURCES.md` with download URLs, expected directory structure, column names, and ingestion commands.
**Files created:** `DATA_SOURCES.md`

---

## Phase 3: Reverse Wage Lookup — PLANNED (not started)

Detailed plan: `plans/phase3_reverse_wage_lookup.md`

**Feature:** Given a wage + SOC code, tell the user which MSA locations their wage qualifies them at (and at what level). Targeted at remote workers evaluating where to file.

**New tools (both in `src/server.py`):**

| Tool | Purpose |
|------|---------|
| `get_wage_qualification_summary(soc_code, annual_wage)` | Returns per-level counts of qualifying MSAs — entry point for HITL |
| `find_qualifying_locations(soc_code, annual_wage, target_level?, state_filter?, offset, limit)` | Paginated list of qualifying MSAs with surplus amount |

**Schema change:** Add index `ix_oflc_wages_soc_wage` on `(soc_code, yearly_wage)` in `src/db/models.py` — needed so the reverse query doesn't do a full table scan per SOC code.

**Files to change:** `src/server.py` (2 new tools), `src/db/models.py` (1 new index)

---

## Phase 2: Legal Pipeline — 2a/2b/2c/2d COMPLETE, 2e remaining

### Completed

#### 2a. Rewrite `scripts/ingest_legal.py` — Federal Register XML Parser — COMPLETED (2026-03-16)
**Problem:** Skeleton parser produced garbage citations (`FR §SECTION §P`) and non-functional cross-references.
**Fix:** Complete rewrite with three section-specific parsers:
- **Preamble parser** — Extracts Summary, Dates, Addresses, Contact Info as leaf nodes
- **SUPLINF parser** — Stack-based heading hierarchy (`HD SOURCE="HD1/HD2/HD3"`) → tree with proper depth. Handles mixed content (text + children) by splitting into Introduction child nodes.
- **REGTEXT parser** — Extracts CFR title/part from XML attributes, parses `<SECTION>` with `<SECTNO>`, builds subsection tree from paragraph markers `(h)(19)(ii)(B)` with full CFR citations like `8 CFR §214.2(h)(19)(ii)(B)`
- Cross-references extracted via regex patterns and stored with `target_node_id=None`
- Leaf/intermediate separation enforced: `full_text` only on leaves
**Files changed:** `scripts/ingest_legal.py` (complete rewrite)

#### 2b. Bottom-Up LLM Summarization Pipeline — COMPLETED (2026-03-17)
**Problem:** No LLM calls existed. Non-leaf nodes had hardcoded `summary="Section handling {title}"`.
**Fix:** Two new files implementing the PageIndex pattern:
- **`src/llm.py`** — Model-agnostic LLM client using OpenAI-compatible chat completions API. Token-bucket rate limiter. Configurable via env vars (`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_RPM`). Supports Gemini (free tier default), OpenAI, and custom providers. Exponential backoff on 429/5xx.
- **`scripts/summarize_tree.py`** — Bottom-up tree walker. Computes processing levels (leaves first, then parents). Leaf nodes: summarizes `full_text`. Intermediate nodes: summarizes children's summaries (map-reduce). Supports `--force` (re-summarize all), `--dry-run` (preview without LLM calls). Flushes to DB every 50 nodes.
**Files created:** `src/llm.py`, `scripts/summarize_tree.py`
**Files changed:** `pyproject.toml` (added `httpx`), `.env.example` (added LLM vars)

#### 2c. Cross-Reference Resolution — COMPLETED (2026-03-17)
**Problem:** `NodeCrossReference` rows all had `target_node_id=None`.
**Fix:** New script `scripts/resolve_crossrefs.py` with multi-strategy matching:
1. Direct normalized citation match (case-insensitive, § normalization)
2. CFR component extraction and match (`title`, `section.number`, `subsections`)
3. Partial/parent match — strips subsection markers from the right until a match is found
4. Section-number match — bare number against all citations
5. Paragraph reference match — `paragraph (h)(19)` against citation suffixes
6. Substring fallback — last resort for unusual reference formats
Self-references are detected and skipped. Unresolved references are logged.
**Files created:** `scripts/resolve_crossrefs.py`

#### 2d. USCIS Policy Manual Ingestion — COMPLETED (2026-03-17)
**Problem:** No script existed. `get_server_info()` listed USCIS Policy Manual as a source but no data was in the database.
**Research findings:** USCIS Policy Manual is HTML-only at uscis.gov (no API, no bulk download). Structure: Volume → Part → Chapter → Sections (from HTML headings).
**Fix:** New script `scripts/ingest_uscis.py` with two phases:
- **Fetcher (`--fetch`)**: Crawls uscis.gov/policy-manual with 2s rate limiting. Discovers volumes → parts → chapters via link extraction. Caches all HTML locally to `./data/uscis/` with a `manifest.json`. Polite user-agent string.
- **Parser (`--ingest`)**: Reads cached HTML, finds content container via multiple CSS selectors (Drupal body field, article content, etc.). Stack-based heading parser (h2/h3/h4/h5) builds section tree. Leaf/intermediate separation with Introduction child nodes for mixed content.
- **Citations**: `USCIS-PM Vol. {N}, Pt. {L}, Ch. {N}, {section_title}`
- **Cross-references**: Extracts Volume/Part/Chapter refs, CFR refs, INA refs
- **Selective ingestion**: Default volumes 2, 6, 7 (configurable via `--volumes`)
- **Two ingestion modes**: manifest-based (from `--fetch`) or directory-discovery (manual HTML placement)
- **Clears only USCIS nodes** on re-run (preserves Federal Register nodes)
**Files created:** `scripts/ingest_uscis.py`
**Files changed:** `pyproject.toml` (added `beautifulsoup4`), `DATA_SOURCES.md` (added USCIS section + updated pipeline steps)

### Remaining Work

#### 2e. Test Suite
**Current state:** pytest in dev dependencies, zero test files.
**Required:**
- Unit tests for all 9 MCP tool functions using a mock/test database
- Ingestion tests verifying tree integrity (parent-child relationships, citation format)
- Integration test: full SOC discovery flow (major → minor → broad → detailed → wage lookup)

---

## Verification Checklist (Phase 1)

These should be run after downloading data and running ingestion to confirm Phase 1 fixes work:

- [ ] `python -m src.server` starts on stdio (no crash, no port binding)
- [ ] `MCP_TRANSPORT=sse python -m src.server` binds to a port
- [ ] `SELECT count(*) FROM soc_hierarchy WHERE level = 0` returns ~23
- [ ] `SELECT count(*) FROM soc_hierarchy WHERE level = 4` returns O*NET codes
- [ ] `SELECT * FROM soc_hierarchy WHERE parent_soc_code IS NOT NULL AND parent_soc_code NOT IN (SELECT soc_code FROM soc_hierarchy)` returns 0 rows (FK integrity)
- [ ] `EXPLAIN ANALYZE SELECT * FROM oflc_wages WHERE soc_code = '15-2051' AND msa_area = 'San Jose...'` shows index scan
- [ ] `get_legal_citations([1, 2])` response starts with UPL disclaimer
- [ ] MCP client → `get_soc_major_groups()` → returns ~23 groups → `get_soc_children("15-0000")` returns children

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `src/server.py` | Main MCP server — all 9 tools defined here |
| `src/db/models.py` | SQLAlchemy ORM models for all 8 tables |
| `src/db/database.py` | Database engine and session factory |
| `scripts/ingest_onet.py` | BLS SOC structure + O*NET data ingestion |
| `scripts/ingest_oflc.py` | OFLC wage data + SOC crosswalk ingestion |
| `scripts/ingest_msa.py` | ZIP-to-MSA mapping ingestion |
| `scripts/ingest_legal.py` | Federal Register XML ingestion (GPO format parser) |
| `scripts/resolve_crossrefs.py` | Cross-reference resolution (Phase 2c) |
| `scripts/summarize_tree.py` | Bottom-up LLM summarization pipeline (Phase 2b) |
| `src/llm.py` | Model-agnostic LLM client with rate limiting |
| `scripts/ingest_uscis.py` | USCIS Policy Manual fetcher + ingestion (Phase 2d) |
| `context/initial_discussion_context.md` | Architecture overview and project goals |
| `context/second_discussion_context.md` | Deep dive on tree traversal, HITL design, schema, tool definitions |
| `plans/implementation_plan.md` | Original implementation plan with schema and tool specs |
| `DATA_SOURCES.md` | Where to download all required data files |

---

## Design Decisions Log

| Decision | Rationale | Date |
|----------|-----------|------|
| JSON-RPC (MCP) over gRPC | LLMs need runtime tool introspection; gRPC is compile-time. MCP ecosystem compatibility. Agent is slow by design (~4-16s per tool call due to LLM thinking) so JSON overhead is irrelevant. | 2026-03-16 |
| MCP over Markdown/Skills | Data is too large for context (~2M wage rows, 12 legal volumes). Exact lookups required (not LLM approximation). Deterministic citations require SQL, not LLM generation. | 2026-03-16 |
| No vector search in v1 | Preserves legal hierarchy. Deterministic citations. No embedding cost. PageIndex tree traversal is sufficient for navigation. | 2026-03-14 |
| Stateless server, stateful client | MCP tools are pure functions. HITL is emergent from client LLM following tool descriptions. Simplifies server, no session management. | 2026-03-14 |
| BLS SOC structure file for hierarchy | O*NET data doesn't contain Major/Minor/Broad group rows. BLS file provides complete hierarchy levels 0-3. | 2026-03-16 |
| Model-agnostic LLM summarization | Use any free-tier model for PageIndex construction. Budget < $10. Gemini Flash-Lite suggested but not locked in. | 2026-03-16 |
| GitHub + Google Drive for distribution | No cloud hosting budget. Data files too large for git. Drive links in README/DATA_SOURCES.md. | 2026-03-16 |
| Composite index on (soc_code, msa_area) | Wage lookups filter both columns. Individual indexes cause partial scan on ~2M rows. Composite gives direct lookup. | 2026-03-16 |
