# Phase 3: Reverse Wage Lookup

**Status:** Planned (not started)
**Motivation:** Remote workers need to know which locations they are competitive in given their current salary, before committing to a visa filing location.

---

## Feature Summary

Inverse of the existing `get_wage_data` tool. A user provides:
1. A SOC code (already discoverable via the existing SOC tree)
2. Their annual wage (e.g. $150,000)

The server returns:
- Which MSA locations their wage qualifies them at (and at which level: 1–4)
- Optionally filtered by target level (e.g. "show me only Level 4 locations")
- Optionally filtered by state
- Paginated for navigation

---

## Key Data Facts (from schema review)

- `oflc_wages` table columns: `soc_code`, `soc_title`, `msa_area`, `wage_level` (1–4), `hourly_wage`, `yearly_wage`
- `msa_mapping` table columns: `zip_code`, `city_name`, `state_abbr`, `msa_area`
- `soc_crosswalk` maps O*NET codes → OFLC codes (must be applied before querying wages)
- `oflc_wages` has ~2M rows — composite index on `(soc_code, msa_area)` exists
- Wage level semantics: Level 1 = entry (17th pct), Level 2 = qualified (34th), Level 3 = experienced (50th), Level 4 = fully competent (67th)
- A user's wage "qualifies" for a level in a given MSA when their wage ≥ that level's prevailing wage threshold

---

## New Tools (2 tools → server.py)

### Tool 1: `get_wage_qualification_summary`

**Signature:**
```python
def get_wage_qualification_summary(soc_code: str, annual_wage: float) -> Dict[str, Any]
```

**Purpose:** Entry point for the reverse lookup flow. Returns counts (not the full list) so the LLM can present a high-level picture to the user before they choose to drill down.

**Logic:**
1. Resolve SOC crosswalk (same pattern as `get_wage_data`)
2. Query `oflc_wages WHERE soc_code = X AND yearly_wage <= annual_wage`
3. For each MSA, find the **highest** qualifying wage_level (subquery with `DISTINCT ON (msa_area) ORDER BY wage_level DESC`)
4. Count how many MSAs qualify at each level (1–4) and in total
5. Return summary counts + soc_title

**Example response:**
```json
{
  "soc_code": "15-1252",
  "oflc_soc_code": "15-1252",
  "soc_title": "Software Developers",
  "your_wage": 150000,
  "qualifying_summary": {
    "Level 4": 47,
    "Level 3": 89,
    "Level 2": 134,
    "Level 1": 201
  },
  "total_qualifying_locations": 201,
  "note": "Counts show highest level qualified per location. A location counted at Level 4 also qualifies at Levels 1–3.",
  "crosswalk_used": false
}
```

**LLM instruction in docstring:** "Call this first to give the user a summary. Then call find_qualifying_locations with a target_level to show the specific MSAs."

---

### Tool 2: `find_qualifying_locations`

**Signature:**
```python
def find_qualifying_locations(
    soc_code: str,
    annual_wage: float,
    target_level: Optional[int] = None,   # 1, 2, 3, or 4 — if None, return highest qualifying level per MSA
    state_filter: Optional[str] = None,   # 2-letter abbreviation e.g. "CA", "TX"
    offset: int = 0,
    limit: int = 25
) -> Dict[str, Any]
```

**Purpose:** Returns the paginated list of MSAs where the user's wage qualifies.

**Logic:**
1. Resolve SOC crosswalk
2. Build base query on `oflc_wages WHERE soc_code = X AND yearly_wage <= annual_wage`
3. If `target_level` is set: add `AND wage_level = target_level`
4. If `target_level` is None: use a subquery to get the **highest qualifying level per MSA**
   - SQLAlchemy subquery: `SELECT DISTINCT ON (msa_area) ... ORDER BY msa_area, wage_level DESC`
   - This returns one row per MSA showing the highest level their wage meets
5. For `state_filter`: JOIN with `msa_mapping` on `msa_area` and filter on `state_abbr = state_filter`
   - Fall back to a LIKE match on `msa_area` (many OFLC areas end in `, TX` or similar) if the join yields no results
6. Apply `OFFSET` / `LIMIT` for pagination
7. Return results + pagination metadata

**Example response:**
```json
{
  "soc_code": "15-1252",
  "oflc_soc_code": "15-1252",
  "soc_title": "Software Developers",
  "your_wage": 150000,
  "target_level": 4,
  "state_filter": null,
  "results": [
    {
      "msa_area": "San Jose-Sunnyvale-Santa Clara, CA",
      "qualifying_level": 4,
      "prevailing_wage_at_level": 148200,
      "hourly_at_level": 71.25,
      "surplus": 1800
    },
    {
      "msa_area": "Seattle-Tacoma-Bellevue, WA",
      "qualifying_level": 4,
      "prevailing_wage_at_level": 145100,
      "hourly_at_level": 69.76,
      "surplus": 4900
    }
  ],
  "pagination": {
    "offset": 0,
    "limit": 25,
    "returned": 25,
    "has_more": true
  },
  "crosswalk_used": false
}
```

**`surplus` field:** `annual_wage - prevailing_wage_at_level`. Helps users quickly see how much buffer they have above the threshold.

---

## SQLAlchemy Query Design

### Highest-qualifying-level subquery (for `target_level=None` case)

```python
from sqlalchemy import func, distinct
from sqlalchemy.orm import aliased

# Subquery: for each (soc_code, msa_area), get max wage_level where yearly_wage <= annual_wage
subq = (
    db.query(
        OFLCWage.msa_area,
        func.max(OFLCWage.wage_level).label("max_level")
    )
    .filter(
        OFLCWage.soc_code == query_soc,
        OFLCWage.yearly_wage <= annual_wage
    )
    .group_by(OFLCWage.msa_area)
    .subquery()
)

# Join back to get the full row for that (msa_area, max_level)
query = (
    db.query(OFLCWage)
    .join(subq, (OFLCWage.msa_area == subq.c.msa_area) & (OFLCWage.wage_level == subq.c.max_level))
    .filter(OFLCWage.soc_code == query_soc)
    .order_by(OFLCWage.wage_level.desc(), OFLCWage.msa_area)
)
```

### State filter join

```python
if state_filter:
    msa_subq = (
        db.query(MSAMapping.msa_area)
        .filter(MSAMapping.state_abbr == state_filter.upper())
        .distinct()
        .subquery()
    )
    query = query.filter(OFLCWage.msa_area.in_(msa_subq))
```

### Summary counts (for Tool 1)

```python
from sqlalchemy import case

subq = (
    db.query(
        OFLCWage.msa_area,
        func.max(OFLCWage.wage_level).label("max_level")
    )
    .filter(OFLCWage.soc_code == query_soc, OFLCWage.yearly_wage <= annual_wage)
    .group_by(OFLCWage.msa_area)
    .subquery()
)

counts = db.query(subq.c.max_level, func.count().label("cnt")).group_by(subq.c.max_level).all()
```

---

## Implementation Steps

### Step 1 — Add `get_wage_qualification_summary` to `src/server.py`
- Place in the `# Wage & MSA Tools` section, after `get_wage_data`
- Reuse the crosswalk resolution pattern from `get_wage_data`
- SQLAlchemy subquery for max-level-per-MSA counts

### Step 2 — Add `find_qualifying_locations` to `src/server.py`
- Place immediately after Step 1 tool
- Handle `target_level=None` (highest level per MSA) vs `target_level=N` (exact level filter)
- Handle `state_filter` via MSAMapping join
- Compute `surplus` = `annual_wage - yearly_wage`
- Return pagination envelope

### Step 3 — Index review
- Existing composite index `ix_oflc_wages_soc_msa` on `(soc_code, msa_area)` helps but the new query filters on `(soc_code, yearly_wage)`
- Add index `ix_oflc_wages_soc_wage` on `(soc_code, yearly_wage)` in `src/db/models.py` to support the reverse lookup
- This is the primary performance concern: without it, the new queries scan all rows for a given SOC code

### Step 4 — Update `get_server_info`
- Add the two new tools to any tool list or capability description returned

### Step 5 — Update `task_status.md`
- Add Phase 3 section documenting the new tools

---

## Files Changed

| File | Change |
|------|--------|
| `src/server.py` | Add 2 new tools in the Wage & MSA section |
| `src/db/models.py` | Add index `ix_oflc_wages_soc_wage` on `(soc_code, yearly_wage)` |
| `plans/task_status.md` | Add Phase 3 section |

No new files required. No schema migrations needed (index only).

---

## Edge Cases to Handle

| Case | Handling |
|------|---------|
| SOC code not in OFLC data after crosswalk | Return `{"error": "..."}` with the resolved OFLC code |
| annual_wage = 0 or negative | Return `{"error": "annual_wage must be a positive number"}` |
| No qualifying locations found | Return empty results list with `total = 0`, clear message |
| state_filter yields no MSAMapping matches | Fall back: LIKE filter on `OFLCWage.msa_area` for state abbreviation |
| target_level out of range (not 1–4) | Return `{"error": "target_level must be 1, 2, 3, or 4"}` |
| Hourly-only wage (some OFLC rows have null yearly_wage) | Filter `yearly_wage IS NOT NULL`; note in response if any rows skipped |

---

## Intended HITL Flow (LLM guidance via docstrings)

```
User: "I earn $150K as a software developer. Where am I competitive?"
  → LLM calls get_wage_qualification_summary(soc_code="15-1252", annual_wage=150000)
  → Returns: Level 4 in 47 locations, Level 3 in 89, etc.
  → LLM presents summary, asks user which level they want to explore

User: "Show me Level 4 locations"
  → LLM calls find_qualifying_locations(soc_code="15-1252", annual_wage=150000, target_level=4)
  → Returns first 25 MSAs with surplus amounts

User: "Any in Texas?"
  → LLM calls find_qualifying_locations(..., target_level=4, state_filter="TX")

User: "Show more"
  → LLM calls find_qualifying_locations(..., offset=25)
```

---

## Not In Scope (v1)

- Hourly wage input (user must provide annual; server converts if needed by multiplying hourly × 2080)
- Filtering by cost-of-living adjusted wage
- Comparing multiple SOC codes simultaneously
- Saving/exporting results
