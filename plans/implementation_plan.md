# Implementation Plan: Immigration & Wage Navigator MCP Server

## 1. Goal Description
Build an MCP Server that provides deterministic, accurate U.S. Immigration Law navigation and SOC Code/Wage discovery. The system uses a pure tree-crawling architecture (PageIndex) stored in PostgreSQL, eliminating semantic vector search entirely for the initial version. 
- Legal document crawling is driven by the LLM client iteratively traversing the tree.
- SOC code discovery is driven by a Human-in-the-Loop (HITL) process, with the LLM fetching and presenting options to the human at each node level, including a "None of these" option to backtrack.

## 2. Architecture & Database
- **Backend:** Python + FastAPI + official Anthropic `mcp` SDK (`FastMCP`).
- **Database:** PostgreSQL (Relational ONLY, no `pgvector`).
- **State Management:** The MCP server is completely stateless. The LLM client acting as the agent maintains conversation state and provides parameters (like `parent_id`) to the MCP tools based on previous tool outputs.
- **UPL Compliance:** "This is not legal advice" disclaimer is **hardcoded** into the string return of all legal tools on the server side, ensuring it is unconditionally presented before any legal text.

### Database Schema
```sql
-- Nodes for Legal Text and overall tree structure
CREATE TABLE nodes (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,         -- 'federal_register', 'uscis_manual'
    parent_id INTEGER REFERENCES nodes(id),
    level INTEGER NOT NULL,      -- depth in tree (0 = root)
    title TEXT NOT NULL,
    summary TEXT,                -- LLM-generated summary for navigation
    full_text TEXT,              -- leaf nodes only
    citation TEXT,               -- legal citation path
    metadata JSONB               -- dict with effective_date, agency, etc.
);

-- O*NET Data
CREATE TABLE onet_occupations (
    soc_code VARCHAR(10) PRIMARY KEY,
    title TEXT,
    description TEXT
);

CREATE TABLE onet_task_statements (
    id SERIAL PRIMARY KEY,
    soc_code VARCHAR(10) REFERENCES onet_occupations(soc_code),
    task TEXT,
    task_type VARCHAR(20)
);

CREATE TABLE onet_tools_technology (
    id SERIAL PRIMARY KEY,
    soc_code VARCHAR(10) REFERENCES onet_occupations(soc_code),
    t2_type VARCHAR(20),
    t2_example TEXT,
    hot_technology BOOLEAN
);

-- OFLC Wage Data
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

## 3. Data Sources & Ingestion
- `scripts/ingest_onet.py`: Parses O*NET txt/csv files into Postgres.
- `scripts/ingest_oflc.py`: Parses OFLC CSV files.
- `scripts/ingest_legal.py`: Reads Federal Register XML and USCIS Policy Manual and constructs the `nodes` table.

## 4. MCP Tools Definitions
```python
@mcp.tool()
def read_legal_node(node_id: int | None = None) -> list[dict]:
    """Returns the children of a given node to allow the LLM to traverse the legal tree. If node_id is None, returns root topics."""

@mcp.tool()
def get_legal_leaf(node_id: int) -> str:
    """Returns the verbatim text of a leaf node. HARDCODES UPL disclaimer strictly into the response string here."""

@mcp.tool()
def get_soc_major_groups() -> list[dict]:
    """Returns top-level SOC categories for the human to choose from."""

@mcp.tool()
def get_soc_children(parent_prefix: str) -> list[dict]:
    """Given a parent SOC prefix (e.g. '15-'), returns the detailed occupations with task/tool enrichment. Also conceptually powers the 'None of these - Expand' response."""

@mcp.tool()
def resolve_msa(zip_code_or_city: str) -> str:
    """Resolves a human-friendly location to the exact OFLC MSA Area name."""

@mcp.tool()
def get_wage_data(soc_code: str, msa_area: str) -> dict:
    """Returns Level 1-4 prevailing wages for the given SOC and MSA."""
```

## 5. Verification Plan
### Automated Tests
- Unit tests for all FastMCP tool functions using a mock SQLite database to ensure the tools return the correct schemas.
- Database ingestion tests verifying correct mappings (e.g. tree structures maintain integrity, O*NET task associations are correct).

### Manual Verification
- Start the server and connect with an LLM client (Claude Desktop or local MCP client).
- Test SOC Code Traversal: Enter as a vague job ("I do AI stuff") and verify HITL presents correct options at each level.
- Test "None of these": Verify the client can gracefully ask the server for widened search if the user selects "None."
- Test Legal Navigation: Query for a specific regulation, traverse tree, and confirm the hardcoded UPL disclaimer is present.
