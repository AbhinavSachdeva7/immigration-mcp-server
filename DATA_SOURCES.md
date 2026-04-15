# Data Sources & Download Instructions

This project requires external data files that are **not included in the repository** due to size. Download them into the `./data/` directory before running ingestion scripts.

## Expected Directory Structure

```
data/
├── onet/
│   ├── soc_structure.csv          # BLS SOC hierarchy (Major/Minor/Broad/Detailed groups)
│   ├── Occupation Data.txt        # O*NET occupation descriptions
│   ├── Task Statements.txt        # O*NET task statements per occupation
│   ├── Technology Skills.txt      # O*NET technology skills per occupation (O*NET 28.0+)
│   └── Tools Used.txt             # O*NET tools used per occupation (O*NET 28.0+)
├── oflc/
│   ├── ALC_Export.csv             # OFLC OES-based prevailing wages (hourly, Level 1-4, wide format)
│   ├── Geography.csv              # Numeric area code → MSA name mapping
│   └── xwalk_plus.csv             # OES/OFLC SOC code → O*NET code crosswalk
├── zip/
│   └── ZIP_CBSA_MMYYYY.xlsx       # HUD ZIP-CBSA crosswalk (e.g. ZIP_CBSA_122025.xlsx)
├── legal/
│   └── *.xml                      # Federal Register XML documents
├── ecfr/                           # eCFR Title 8 (fetched automatically)
│   ├── title-8-structure.json      # Full Title 8 hierarchy
│   ├── manifest.json               # Master fetch manifest
│   └── part-{NNN}/                 # Per-part directories
│       ├── structure.json           # Part structure subtree
│       ├── manifest.json            # Part-level manifest
│       └── section-{NNN_NN}.html    # Section HTML files
├── ina/                             # INA / Title 8 USC (fetched automatically)
│   └── usc08.xml                    # Full Title 8 US Code XML (USLM format)
└── uscis/                          # USCIS Policy Manual (fetched automatically)
    └── volume-{N}/                 # Per-volume directories
```

## 1. BLS SOC Structure

**What:** The Standard Occupational Classification hierarchy — all Major, Minor, Broad, and Detailed groups with titles and definitions.

**Source:** Bureau of Labor Statistics (BLS) 2018 SOC

**Download:** https://www.bls.gov/soc/2018/home.htm
- Look for "SOC Structure" or "2018 SOC Definitions" download (Excel/CSV)
- Save as `./data/onet/soc_structure.csv`
- Expected columns: `SOC Code`, `SOC Title`, `SOC Definition` (column names are flexible — the ingestion script handles common variants)

## 2. O*NET Database

**What:** Detailed occupation data including task statements, tools, and technology for ~1,000 occupations.

**Source:** O*NET Resource Center

**Download:** https://www.onetcenter.org/database.html
- Download the "O*NET 30.2 Database" (or latest version)
- Extract the zip file
- Copy these files into `./data/onet/`:
  - `Occupation Data.txt`
  - `Task Statements.txt`
  - `Technology Skills.txt`
  - `Tools Used.txt`
- Files are tab-delimited text with headers

## 3. OFLC Prevailing Wage Data

**What:** Department of Labor prevailing wage levels (1-4) by SOC code and Metropolitan Statistical Area (MSA).

**Source:** Office of Foreign Labor Certification (OFLC)

**Download:** https://www.dol.gov/agencies/eta/foreign-labor/wages
- Download the most recent "OFLC Online Wage Library" data (July 2025 – June 2026 period)
- Convert/save as `./data/oflc/oflc_wages.csv`
- Expected columns: `SOC_CODE`, `SOC_TITLE`, `AREA_TITLE`, `WAGE_LEVEL`, `HOURLY_WAGE`, `ANNUAL_WAGE`

**SOC Crosswalk (if needed):**
- If OFLC uses different SOC codes than O*NET (common for merged/split codes), create a crosswalk CSV at `./data/oflc/soc_crosswalk.csv`
- Expected columns: `OFLC_SOC_CODE`, `ONET_SOC_CODE`, `MAPPING_TYPE` (values: exact, merged, split)

## 4. ZIP to MSA Mapping

**What:** Maps ZIP codes and city names to OFLC MSA area names for the `resolve_msa` tool.

**Source:** HUD USPS ZIP Code Crosswalk Files

**Download:** https://www.huduser.gov/portal/datasets/usps_crosswalk.html
- Download the ZIP-CBSA crosswalk file
- Reformat/save as `./data/msa/zip_to_msa.csv`
- Expected columns: `ZIP`, `CITY`, `STATE`, `MSA_NAME`

## 5. Federal Register XML (for Legal Pipeline — Phase 2)

**What:** Federal Register documents in XML format, specifically the Feb 27, 2026 Final Rule on H-1B wage-weighted selection.

**Source:** Federal Register API / GovInfo (GPO XML format)

**Download options:**

1. **Federal Register API** — https://www.federalregister.gov/developers/documentation/api/v1
   - Search for the rule by document number or keyword
   - Request the full XML version from the API response's `full_text_xml_url` field
   - Example API search: `https://www.federalregister.gov/api/v1/documents.json?conditions[term]=H-1B+wage+selection&conditions[type]=RULE`

2. **GovInfo bulk data** — https://www.govinfo.gov/bulkdata/FR
   - Navigate to the publication date and find the document XML
   - Files follow GPO XML DTD (element names: RULE, PREAMB, SUPLINF, REGTEXT, etc.)

**Save** XML files into `./data/legal/`

**Expected XML structure (GPO format):**
```
<RULE>
  <PREAMB> — agency, CFR info, summary, dates
  <SUPLINF> — discussion with <HD SOURCE="HD1/HD2/HD3"> heading hierarchy
  <REGTEXT PART="214" TITLE="8"> — actual CFR amendments with <SECTION> elements
</RULE>
```

The ingestion script handles RULE, PRORULE, and NOTICE document types.

## 6. eCFR — Title 8 (Code of Federal Regulations)

**What:** The current, consolidated Code of Federal Regulations for immigration — the actual regulations that practitioners cite (e.g., "8 CFR § 214.2(h)(4)(ii)"). This is the authoritative regulatory text that the USCIS Policy Manual interprets and the Federal Register amends.

**Source:** eCFR API (free, no authentication) — https://www.ecfr.gov

**Target parts** (MVP scope):
- **Part 204** — Immigrant Petitions (employment-based immigrant categories)
- **Part 214** — Nonimmigrant Workers (H-1B, L-1, O-1, etc.)
- **Part 245** — Adjustment of Status

**Download:** The ingestion script handles fetching automatically:
```bash
# Fetch Parts 204, 214, 245 from eCFR API (2s delay between requests)
python -m scripts.ingest_ecfr --fetch --parts 204 214 245

# Ingest cached data into database
python -m scripts.ingest_ecfr --ingest

# Both in one step
python -m scripts.ingest_ecfr --fetch --ingest --parts 204 214 245
```

Pages are cached in `./data/ecfr/` with structure JSON and section HTML files.

**Node tree structure:**
```
Title 8 — Aliens and Nationality         (level 0, citation: "8 CFR")
└── Part 214 — Nonimmigrant Workers       (level 1, citation: "8 CFR Part 214")
    └── § 214.2 Special requirements...   (level 2, citation: "8 CFR §214.2")
        └── (h) Temporary workers         (level 3, citation: "8 CFR §214.2(h)")
            └── (4) Specific requirements (level 4, citation: "8 CFR §214.2(h)(4)")
                └── (ii) Criteria         (level 5, citation: "8 CFR §214.2(h)(4)(ii)")
```

## 7. INA — Immigration and Nationality Act (8 USC Chapter 12)

**What:** The primary federal statute governing immigration. Practitioners cite INA section numbers (e.g., "INA § 214(i)(1)") which map to Title 8 of the US Code. This is the highest authority in the citation chain — Congress passed it, and the CFR regulations implement it.

**Source:** US Code XML from uscode.house.gov (USLM format)

**Target sections** (MVP scope):
- **INA § 101** (8 USC § 1101) — Definitions (including H-1B definition)
- **INA § 203** (8 USC § 1153) — Allocation of immigrant visas (EB-1 through EB-5)
- **INA § 212** (8 USC § 1182) — Inadmissibility grounds
- **INA § 214** (8 USC § 1184) — Admission of nonimmigrants (H-1B, L-1, O-1, etc.)
- **INA § 245** (8 USC § 1255) — Adjustment of status

**Download:** The ingestion script handles fetching automatically:
```bash
# Fetch Title 8 XML and ingest key INA sections
python -m scripts.ingest_ina --fetch --ingest --sections 101 203 212 214 245
```

Data is cached in `./data/ina/usc08.xml`.

**Node tree structure:**
```
Immigration and Nationality Act (INA)      (level 0, citation: "INA")
└── INA § 214 — Admission of nonimmigrants (level 1, citation: "INA §214")
    └── (i) H-1B requirements              (level 2, citation: "INA §214(i)")
        └── (1) Specialty occupation        (level 3, citation: "INA §214(i)(1)")
```

## 8. USCIS Policy Manual

**What:** The USCIS Policy Manual — comprehensive guidance on immigration policy. Organized as Volumes → Parts → Chapters → Sections.

**Source:** https://www.uscis.gov/policy-manual (HTML pages, no API or bulk download)

**Target volumes** (immigration-relevant subset):
- **Volume 2** — Nonimmigrants (H-1B, F-1, L-1, etc.)
- **Volume 6** — Immigrants (employment-based categories)
- **Volume 7** — Adjustment of Status

**Download:** The ingestion script handles fetching automatically:
```bash
# Fetch and cache HTML pages locally (2s delay between requests)
python -m scripts.ingest_uscis --fetch --volumes 2 6 7
```

Pages are cached in `./data/uscis/` with a `manifest.json` describing the structure.
Re-running `--fetch` will re-download all pages (delete `./data/uscis/` first to start fresh).

## Running Ingestion

After downloading all data files:

```bash
# Ensure PostgreSQL is running (via Docker or locally)
docker compose up db -d

# Install dependencies
pip install -e .

# Step 1: Ingest structured data
python -m scripts.ingest_onet --data-dir ./data/onet
python -m scripts.ingest_oflc --data-dir ./data/oflc
python -m scripts.ingest_msa --data-dir ./data/zip --geo-dir ./data/oflc

# Step 2: Ingest legal documents (Federal Register XML)
python -m scripts.ingest_legal --data-dir ./data/legal

# Step 3: Fetch and ingest eCFR Title 8 (immigration regulations)
python -m scripts.ingest_ecfr --fetch --ingest --parts 204 214 245

# Step 4: Fetch and ingest INA (Immigration and Nationality Act)
python -m scripts.ingest_ina --fetch --ingest --sections 101 203 212 214 245

# Step 5: Fetch and ingest USCIS Policy Manual
python -m scripts.ingest_uscis --fetch --ingest --volumes 2 6 7

# Step 6: Resolve cross-references between legal nodes
python -m scripts.resolve_crossrefs

# Step 7: Generate LLM summaries (requires LLM_API_KEY in .env)
# Preview first:
python -m scripts.summarize_tree --dry-run
# Then run:
python -m scripts.summarize_tree
# To re-summarize everything:
python -m scripts.summarize_tree --force
```
