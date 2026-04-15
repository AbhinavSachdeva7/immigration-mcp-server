# Task Checklist for MCP Server

- [ ] **Phase 1: Project Setup & Database Layer**
  - [ ] Initialize Python project (FastAPI / FastMCP setup).
  - [ ] Define SQLAlchemy database models for `nodes`, `onet_*`, and `oflc_wages`.
  - [ ] Setup Docker Compose with PostgreSQL.
- [ ] **Phase 2: Ingestion Scripts**
  - [ ] Script: O*NET txt/csv to `onet_*` tables.
  - [ ] Script: OFLC CSV to `oflc_wages` table.
  - [ ] Script: Federal Register XML parser to `nodes` table.
  - [ ] Script: MSA Resolution mapping data.
- [ ] **Phase 3: MCP Tools (FastMCP)**
  - [ ] Core Legal Tree Tools (`read_legal_node`, `get_legal_leaf`).
  - [ ] Core SOC HITL Tools (`get_soc_major_groups`, `get_soc_children`).
  - [ ] Helper Tools (`resolve_msa`, `get_wage_data`).
  - [ ] Implement hardcoded UPL disclaimer logic in `get_legal_leaf` and anywhere legal text is returned.
- [ ] **Phase 4: Testing & Verification**
  - [ ] Implement Pytest suite with mock database.
  - [ ] Manually test tool interactions via MCP Client.
