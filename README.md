# Immigration Navigator MCP Server

An MCP (Model Context Protocol) server that gives AI assistants access to U.S. immigration law, regulations, and wage data — with real legal citations that attorneys can actually use.

---

## The Problem

When a lawyer asks an AI about H-1B requirements, it typically responds with something like:

> *"According to USCIS Policy Manual Vol. 2, Part B..."*

That's not how lawyers cite law. A brief cites **8 CFR § 214.2(h)(4)(ii)** or **INA § 214(i)(1)** — the actual statutory and regulatory text that can be verified, quoted, and argued in court. Generic AI guidance citations are useless in a legal context.

This project is an MVP demonstrating how a legal firm could connect an AI assistant to authoritative legal sources and get back citations that are actually usable.

---

## What It Does

The server exposes a set of tools that an AI assistant (like Claude) can call to navigate U.S. immigration law and look up labor market data. The tools cover three domains:

### Legal Navigation

The legal corpus is stored as a **tree of nodes** in a database. Each node has a title, a summary, and a citation. The AI navigates the tree level by level — like browsing a table of contents — until it reaches a leaf node containing verbatim statutory or regulatory text.

The citation chain looks like this:

```
8 CFR                                    ← Title 8, Code of Federal Regulations
└── 8 CFR Part 214                       ← Nonimmigrant Workers
    └── 8 CFR § 214.2                    ← Special requirements...
        └── 8 CFR § 214.2(h)            ← Temporary workers (H-1B)
            └── 8 CFR § 214.2(h)(4)     ← Specific requirements
                └── 8 CFR § 214.2(h)(4)(ii)  ← The actual rule text
```

The same structure applies to the INA (Immigration and Nationality Act) and the USCIS Policy Manual.

Every legal response is unconditionally prefixed with a UPL (Unauthorized Practice of Law) disclaimer — this is hardcoded at the server level, not left to the AI to decide.

### SOC Code Discovery

The Standard Occupational Classification (SOC) system is how the Department of Labor classifies jobs. It matters for immigration because visa petitions require specifying an exact SOC code, which then determines prevailing wage requirements.

The AI walks through the SOC hierarchy with the user:

1. Start at ~23 major groups ("Computer and Mathematical Occupations")
2. Narrow to minor/broad groups ("Software and Web Developers")
3. Confirm the exact 6-digit code ("15-1252 — Software Quality Assurance Analysts")

At each step, the user can say "none of these" and backtrack. The process is human-in-the-loop by design — the system guides but the attorney decides.

### Wage Lookup

Once an SOC code and location are confirmed, the server looks up the OFLC prevailing wage data — the Department of Labor's official wage levels used in H-1B and PERM applications. It returns Level 1 through Level 4 wages (entry to senior) for the specific job title and metropolitan area, after mapping zip codes or city names to the correct MSA (Metropolitan Statistical Area).

---

## Architecture

**The key architectural choice: no vector search.**

Most AI-powered legal tools use embeddings + semantic search to retrieve "relevant" chunks of documents. This produces plausible-sounding answers but makes it easy to miss critical nuances or pull from slightly wrong sections.

Instead, this server uses a **tree-crawl approach (PageIndex)**:

- All legal documents are pre-processed into a hierarchical node tree stored in PostgreSQL
- Each non-leaf node gets an LLM-generated summary to aid navigation
- The AI client traverses the tree level by level, choosing which branch to follow
- Leaf nodes contain the verbatim source text

This means the AI always ends up at the exact statutory or regulatory provision — not a semantically similar chunk that might be from a different context.

**The server is completely stateless.** The AI client maintains conversation state and passes parameters (like `node_id`) between tool calls. The server just looks things up and returns results.

### Tech Stack

| Component | Technology |
|-----------|------------|
| MCP framework | Python `mcp` SDK (`FastMCP`) |
| Database | PostgreSQL (no `pgvector`) |
| ORM | SQLAlchemy 2.0 |
| Data parsing | BeautifulSoup4, httpx |
| Containerization | Docker + Docker Compose |
| LLM summarization | Gemini 2.0 Flash Lite (free tier) |

---

## Data Sources

The server ingests data from several official government sources:

| Source | What it provides | Fetch method |
|--------|-----------------|--------------|
| **eCFR** (ecfr.gov) | 8 CFR Parts 204, 214, 245 — the actual immigration regulations | Auto-fetched via API |
| **uscode.house.gov** | INA §§ 101, 203, 212, 214, 245 — the federal statutes | Auto-fetched (USLM XML) |
| **USCIS Policy Manual** | Policy guidance (Volumes 2, 6, 7) | Auto-fetched (HTML scrape) |
| **Federal Register** | Final rules (e.g., H-1B wage-weighted selection) | Manual download (XML) |
| **O\*NET** | ~1,000 occupations with task statements and tools | Manual download |
| **OFLC Wage Library** | Prevailing wages by SOC × MSA, Levels 1–4 | Manual download |
| **BLS SOC Structure** | SOC hierarchy titles and definitions | Manual download |
| **HUD ZIP-CBSA crosswalk** | ZIP code → Metropolitan Area mapping | Manual download |

See [DATA_SOURCES.md](DATA_SOURCES.md) for exact download links and file placement instructions.

---

## Setup

### Prerequisites

- Python 3.11+
- Docker (for PostgreSQL)
- An LLM API key (Gemini free tier works; used only for generating tree summaries during ingestion)

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env with your database URL and LLM API key
```

### 2. Start the Database

```bash
docker compose up db -d
```

### 3. Install Dependencies

```bash
pip install -e .
```

### 4. Download Data Files

Download the manually-sourced datasets (O\*NET, OFLC wages, SOC structure, HUD crosswalk) and place them in `./data/` as described in [DATA_SOURCES.md](DATA_SOURCES.md).

### 5. Run Ingestion

```bash
# Structured data (occupations, wages, geography)
python -m scripts.ingest_onet --data-dir ./data/onet
python -m scripts.ingest_oflc --data-dir ./data/oflc
python -m scripts.ingest_msa --data-dir ./data/zip --geo-dir ./data/oflc

# Legal documents (Federal Register XML)
python -m scripts.ingest_legal --data-dir ./data/legal

# Auto-fetch and ingest eCFR regulations
python -m scripts.ingest_ecfr --fetch --ingest --parts 204 214 245

# Auto-fetch and ingest INA statutes
python -m scripts.ingest_ina --fetch --ingest --sections 101 203 212 214 245

# Auto-fetch and ingest USCIS Policy Manual
python -m scripts.ingest_uscis --fetch --ingest --volumes 2 6 7

# Resolve cross-references between legal nodes
python -m scripts.resolve_crossrefs

# Generate LLM navigation summaries (requires LLM_API_KEY)
python -m scripts.summarize_tree
```

### 6. Run the Server

**For Claude Desktop (stdio transport):**

```bash
python -m src.server
```

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "immigration-navigator": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/immigration-mcp-server",
      "env": {
        "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/immigration_mcp"
      }
    }
  }
}
```

**For remote/Docker deployment (SSE transport):**

```bash
docker compose up
```

---

## MCP Tools Reference

| Tool | Description |
|------|-------------|
| `read_legal_node(node_id?)` | Returns children of a legal tree node (or roots if no ID). Used to navigate toward the right provision. |
| `get_legal_leaf(node_id)` | Returns verbatim legal text + citation + cross-references for a leaf node. |
| `get_legal_citations(node_ids[])` | Batch-returns citation strings for multiple nodes. Used to compile a final answer. |
| `get_soc_major_groups()` | Returns all ~23 top-level SOC groups to start occupation classification. |
| `get_soc_children(parent_soc_code, include_tasks?)` | Returns direct children of a SOC node. Optionally includes O\*NET task statements and tools. |
| `get_soc_details(soc_code)` | Returns full O\*NET profile for a confirmed SOC code. |
| `resolve_msa(zip_or_city)` | Maps a ZIP code or city name to OFLC Metropolitan Statistical Area name(s). |
| `get_wage_data(soc_code, msa_area)` | Returns Level 1–4 prevailing wages for a SOC × MSA pair. |
| `get_server_info()` | Returns data freshness, sources, and compliance status. |

---

## Compliance Note

This server is built with UPL (Unauthorized Practice of Law) compliance in mind. The disclaimer *"This is not legal advice..."* is hardcoded at the server level into every legal tool response — it cannot be omitted or suppressed by the AI client. The server also maintains a full audit log of every tool call.

This is a research and educational tool. It does not replace a licensed immigration attorney.

---

## Project Status

This is an MVP. The current scope covers:

- [x] SOC code hierarchy (BLS 2018 SOC + O\*NET)
- [x] OFLC prevailing wage lookup
- [x] ZIP/city → MSA resolution
- [x] 8 CFR Parts 204, 214, 245 (eCFR)
- [x] INA §§ 101, 203, 212, 214, 245
- [x] USCIS Policy Manual Volumes 2, 6, 7
- [x] Federal Register XML ingestion
- [x] Cross-reference resolution between legal nodes
- [x] LLM-generated navigation summaries
- [ ] Semantic search / vector index (intentionally deferred)
- [ ] Full Title 8 CFR coverage
- [ ] Reverse wage lookup (find qualifying SOC codes for a given salary)
