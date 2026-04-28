import csv
import argparse
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from src.db.database import SessionLocal, engine, Base
from src.db.models import SOCHierarchy, ONetTaskStatement, ONetToolTechnology


def clear_onet_tables(db: Session):
    """Clear existing O*NET data to make ingestion idempotent."""
    db.query(ONetToolTechnology).delete()
    db.query(ONetTaskStatement).delete()
    db.query(SOCHierarchy).delete()
    db.commit()


def _determine_soc_level(soc_code: str) -> int:
    """Determine hierarchy level from SOC code format.

    BLS SOC structure (works for both 2010 and 2018 SOC):
      XX-0000  → Level 0 (Major Group, ~23 groups)
      XX-XX00  → Level 1 (Minor Group, e.g. 15-1000 or 15-1200)
      XX-XXX0  → Level 2 (Broad Occupation, e.g. 15-1010 or 15-1250)
      XX-XXXX  → Level 3 (Detailed Occupation)

    O*NET extensions:
      XX-XXXX.XX → Level 4 (O*NET-specific specialization)
    """
    if "." in soc_code:
        return 4
    base = soc_code.split("-")
    if len(base) != 2:
        return 3
    suffix = base[1]
    if suffix == "0000":
        return 0
    elif suffix.endswith("00"):
        return 1
    elif suffix.endswith("0"):
        return 2
    else:
        return 3


def _determine_parent_code(soc_code: str, level: int, known_codes: set | None = None) -> str | None:
    """Determine parent SOC code. Uses known_codes set to find nearest real ancestor."""
    if level == 0:
        return None

    if "." in soc_code:
        return soc_code.split(".")[0]

    parts = soc_code.split("-")
    major = parts[0]
    suffix = parts[1]

    if level == 1:
        return f"{major}-0000"

    # For levels 2 and 3, try candidates from most specific to least specific.
    # This handles irregular BLS SOC groupings where the "obvious" parent code
    # (e.g. 11-3100 for 11-3120) may not exist in the actual data file.
    if level == 2:
        candidates = [
            f"{major}-{suffix[:2]}00",  # natural minor group
            f"{major}-{suffix[0]}000",  # old-style minor group
            f"{major}-0000",            # major group fallback
        ]
    else:  # level == 3
        candidates = [
            f"{major}-{suffix[:3]}0",   # natural broad occupation
            f"{major}-{suffix[:2]}00",  # minor group fallback
            f"{major}-{suffix[0]}000",  # old-style minor group fallback
            f"{major}-0000",            # major group fallback
        ]

    if known_codes is not None:
        for candidate in candidates:
            if candidate != soc_code and candidate in known_codes:
                return candidate
        return f"{major}-0000"

    # No known_codes provided — return best guess (first candidate)
    return candidates[0]


def ingest_soc_structure(db: Session, file_path: Path):
    """Ingest BLS SOC structure file (levels 0-3: Major, Minor, Broad, Detailed).

    This MUST run before O*NET occupation ingestion so that parent codes exist
    for foreign key relationships.

    Expected CSV format (BLS 2018 SOC structure):
    Columns vary but typically: SOC Code (or Major/Minor/Broad/Detailed Group),
    SOC Title (or Major/Minor/Broad/Detailed Group Title), SOC Definition
    """
    print(f"Ingesting BLS SOC structure from {file_path}...")

    # Two-pass read:
    #   Pass 1 — collect every real SOC code so parent lookup can find nearest ancestor
    #   Pass 2 — build rows with verified parent codes
    raw_rows = []
    known_codes: set = set()

    with open(file_path, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            soc_code = (
                row.get('SOC Code', '') or
                row.get('SOC_CODE', '') or
                row.get('O*NET-SOC Code', '') or
                row.get('code', '')
            ).strip()
            title = (
                row.get('SOC Title', '') or
                row.get('SOC_TITLE', '') or
                row.get('Title', '') or
                row.get('title', '')
            ).strip()
            description = (
                row.get('SOC Definition', '') or
                row.get('SOC_DEFINITION', '') or
                row.get('Description', '') or
                row.get('definition', '') or
                ''
            ).strip()

            if not soc_code or not title or "." in soc_code:
                continue
            level = _determine_soc_level(soc_code)
            if level > 3:
                continue

            known_codes.add(soc_code)
            raw_rows.append((soc_code, title, description, level))

    # Build level-bucketed rows with parent resolved against actual known codes
    rows_by_level = {0: [], 1: [], 2: [], 3: []}
    for soc_code, title, description, level in raw_rows:
        parent_code = _determine_parent_code(soc_code, level, known_codes)
        rows_by_level[level].append({
            "soc_code": soc_code,
            "title": title,
            "description": description,
            "parent_soc_code": parent_code,
            "level": level,
        })

    # Insert level by level (0 → 1 → 2 → 3) so parents exist before children
    total = 0
    for level in range(4):
        for row_data in rows_by_level[level]:
            db.add(SOCHierarchy(**row_data))
        db.flush()
        total += len(rows_by_level[level])
        print(f"  Level {level}: {len(rows_by_level[level])} rows")

    db.commit()
    print(f"  BLS SOC structure: {total} total rows ingested.")


def ingest_occupations(db: Session, file_path: Path):
    """Ingest O*NET Occupation Data.txt — only level 4 (O*NET extensions with .XX suffix).

    BLS structure (levels 0-3) must already be loaded via ingest_soc_structure().
    """
    print(f"Ingesting O*NET occupations from {file_path}...")
    inserted = 0
    skipped = 0

    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            soc_code = row['O*NET-SOC Code'].strip()

            # If it's a base SOC code (no dot), it should already exist from BLS structure.
            # Update its description from O*NET if the BLS row exists.
            if "." not in soc_code:
                existing = db.query(SOCHierarchy).filter(SOCHierarchy.soc_code == soc_code).first()
                if existing:
                    # O*NET often has richer descriptions than BLS
                    if row.get('Description', '').strip():
                        existing.description = row['Description'].strip()
                continue

            # Level 4: O*NET-specific extension (e.g., 15-1252.00)
            parent_code = soc_code.split(".")[0]  # e.g., 15-1252

            # Verify parent exists
            parent_exists = db.query(SOCHierarchy).filter(SOCHierarchy.soc_code == parent_code).first()

            try:
                db.add(SOCHierarchy(
                    soc_code=soc_code,
                    title=row['Title'].strip(),
                    description=row.get('Description', '').strip(),
                    parent_soc_code=parent_code if parent_exists else None,
                    level=4,
                ))
                db.flush()
                inserted += 1

                if not parent_exists:
                    print(f"  WARNING: Parent {parent_code} not found for {soc_code} — inserted with parent=None")
                    skipped += 1
            except IntegrityError:
                db.rollback()
                print(f"  WARNING: Could not insert {soc_code} — skipping")
                skipped += 1

    db.commit()
    print(f"  O*NET occupations: {inserted} inserted, {skipped} warnings.")


def ingest_tasks(db: Session, file_path: Path):
    """Ingest Task Statements.txt"""
    print(f"Ingesting tasks from {file_path}...")
    count = 0
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            db.add(ONetTaskStatement(
                soc_code=row['O*NET-SOC Code'].strip(),
                task=row['Task'].strip(),
                task_type=row.get('Task Type', '').strip()
            ))
            count += 1
    db.commit()
    print(f"  Tasks: {count} rows ingested.")


def ingest_tools(db: Session, tech_skills_path: Path, tools_used_path: Path):
    """Ingest Technology Skills.txt and Tools Used.txt (replaces legacy Tools and Technology.txt).

    Technology Skills.txt columns: O*NET-SOC Code, Example, Commodity Code, Commodity Title, Hot Technology, In Demand
    Tools Used.txt columns:        O*NET-SOC Code, Example, Commodity Code, Commodity Title
    """
    count = 0

    if tech_skills_path.exists():
        print(f"Ingesting technology skills from {tech_skills_path}...")
        with open(tech_skills_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                db.add(ONetToolTechnology(
                    soc_code=row['O*NET-SOC Code'].strip(),
                    t2_type='Technology',
                    t2_example=row.get('Example', '').strip(),
                    hot_technology=(row.get('Hot Technology', 'N').strip() == 'Y'),
                ))
                count += 1
        db.commit()
        print(f"  Technology Skills: {count} rows ingested.")
    else:
        print(f"WARNING: {tech_skills_path} not found — skipping technology skills.")

    if tools_used_path.exists():
        print(f"Ingesting tools used from {tools_used_path}...")
        tools_count = 0
        with open(tools_used_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                db.add(ONetToolTechnology(
                    soc_code=row['O*NET-SOC Code'].strip(),
                    t2_type='Tools',
                    t2_example=row.get('Example', '').strip(),
                    hot_technology=False,
                ))
                tools_count += 1
        db.commit()
        print(f"  Tools Used: {tools_count} rows ingested.")
        count += tools_count
    else:
        print(f"WARNING: {tools_used_path} not found — skipping tools used.")

    print(f"  Tools/Technology total: {count} rows ingested.")


def main(data_dir: str):
    base_path = Path(data_dir)
    if not base_path.exists():
        print(f"Data directory {data_dir} not found.")
        return

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        print("Clearing existing O*NET tables...")
        clear_onet_tables(db)

        # Step 1: Load BLS SOC structure (levels 0-3) — MUST come first
        soc_structure_file = base_path / "soc_structure.csv"
        if soc_structure_file.exists():
            ingest_soc_structure(db, soc_structure_file)
        else:
            print(f"WARNING: {soc_structure_file} not found. Major/Minor/Broad groups will be missing.")
            print("  Download from: https://www.bls.gov/soc/2018/home.htm")

        # Step 2: Load O*NET detailed occupations (level 4) — attaches to existing hierarchy
        occ_file = base_path / "Occupation Data.txt"
        if occ_file.exists():
            ingest_occupations(db, occ_file)

        # Step 3: Load task statements
        task_file = base_path / "Task Statements.txt"
        if task_file.exists():
            ingest_tasks(db, task_file)

        # Step 4: Load tools and technology (split into two files in O*NET 28.0+)
        ingest_tools(
            db,
            tech_skills_path=base_path / "Technology Skills.txt",
            tools_used_path=base_path / "Tools Used.txt",
        )

        print("O*NET Ingestion complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest BLS SOC structure + O*NET data")
    parser.add_argument("--data-dir", type=str, default="./data/onet", help="Path to data folder with soc_structure.csv and O*NET files")
    args = parser.parse_args()
    main(args.data_dir)
