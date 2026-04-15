import csv
import argparse
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from src.db.database import SessionLocal, engine, Base
from src.db.models import OFLCWage, SOCCrosswalk

# Wage cap used by OFLC when OEWS data is top-coded ("High Wage" label)
HIGH_WAGE_HOURLY = 115.00
HIGH_WAGE_YEARLY = 239200.00

# Standard annual hours used by DOL to convert hourly → annual wage
ANNUAL_HOURS = 2080


def clear_oflc_tables(db: Session):
    """Clear existing OFLC data to make ingestion idempotent."""
    db.query(OFLCWage).delete()
    db.query(SOCCrosswalk).delete()
    db.commit()


def _load_geography(geo_path: Path) -> dict:
    """Load Geography.csv into a dict mapping area code → area name.

    Geography.csv columns: Area, AreaName, StateAb, State, CountyTownName
    """
    geo = {}
    with open(geo_path, 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            geo[row['Area'].strip()] = row['AreaName'].strip()
    print(f"  Loaded {len(geo):,} area mappings from {geo_path.name}")
    return geo


def ingest_wages(db: Session, alc_path: Path, geo_path: Path):
    """Ingest ALC_Export.csv (OFLC OES-based prevailing wages).

    ALC_Export.csv columns: Area, SocCode, GeoLvl, Level1, Level2, Level3, Level4, Average, Label
    - Wages are hourly. Annual = hourly * 2080.
    - Level1-4 are separate columns (wide format) — pivoted to one row per level here.
    - Area is a numeric code resolved to MSA name via Geography.csv.
    - Rows with Label='High Wage' use the OFLC top-coded cap ($115/hr, $239,200/yr).
    """
    geo = _load_geography(geo_path)

    print(f"Ingesting OFLC wages from {alc_path.name}...")
    inserted = 0
    skipped = 0
    batch = []

    with open(alc_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            area_code = row['Area'].strip()
            soc_code = row['SocCode'].strip()
            label = row.get('Label', '').strip()
            is_high_wage = (label == 'High Wage')

            msa_area = geo.get(area_code)
            if not msa_area:
                skipped += 1
                continue

            # Fetch SOC title lazily from oes_soc_occs if available — leave blank for now,
            # the crosswalk + soc_hierarchy has titles already
            soc_title = ''

            for level in range(1, 5):
                raw = row.get(f'Level{level}', '').strip()

                if is_high_wage:
                    hourly = HIGH_WAGE_HOURLY
                    yearly = HIGH_WAGE_YEARLY
                elif not raw:
                    # No wage data for this level in this area — skip this level
                    continue
                else:
                    try:
                        hourly = float(raw)
                        yearly = round(hourly * ANNUAL_HOURS, 2)
                    except ValueError:
                        skipped += 1
                        continue

                batch.append(OFLCWage(
                    soc_code=soc_code,
                    soc_title=soc_title,
                    msa_area=msa_area,
                    wage_level=level,
                    hourly_wage=hourly,
                    yearly_wage=yearly,
                ))

            if len(batch) >= 2000:
                db.bulk_save_objects(batch)
                db.commit()
                inserted += len(batch)
                batch = []

                if inserted % 100000 == 0:
                    print(f"  ...{inserted:,} rows inserted")

    if batch:
        db.bulk_save_objects(batch)
        db.commit()
        inserted += len(batch)

    print(f"  OFLC wages: {inserted:,} rows inserted, {skipped:,} skipped.")


def ingest_crosswalk(db: Session, xwalk_path: Path):
    """Ingest xwalk_plus.csv — maps OES/OFLC SOC codes to O*NET SOC codes.

    xwalk_plus.csv columns: OES_SOCCODE, OES_SOCTITLE, TruncOnetCode, OnetCode, ONetTitle
    - OES_SOCCODE: the SOC code used by OFLC/OES (e.g. '15-1252')
    - OnetCode:    the full O*NET code (e.g. '15-1252.00')
    - TruncOnetCode: O*NET base code without .XX suffix (e.g. '15-1252')
    - mapping_type: 'exact' when OES and O*NET base codes match, 'aggregated' otherwise
    """
    print(f"Ingesting SOC crosswalk from {xwalk_path.name}...")
    inserted = 0
    skipped = 0

    with open(xwalk_path, 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            oflc_code = row['OES_SOCCODE'].strip()
            onet_code = row['OnetCode'].strip()
            trunc_code = row['TruncOnetCode'].strip()

            if not oflc_code or not onet_code:
                continue

            mapping_type = 'exact' if oflc_code == trunc_code else 'aggregated'

            try:
                db.add(SOCCrosswalk(
                    oflc_soc_code=oflc_code,
                    onet_soc_code=onet_code,
                    mapping_type=mapping_type,
                ))
                db.flush()
                inserted += 1
            except IntegrityError:
                db.rollback()
                skipped += 1

    db.commit()
    print(f"  SOC crosswalk: {inserted} rows inserted, {skipped} skipped (FK violations — run ingest_onet first).")


def main(data_dir: str):
    base_path = Path(data_dir)
    if not base_path.exists():
        print(f"Data directory {data_dir} not found.")
        return

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        print("Clearing existing OFLC tables...")
        clear_oflc_tables(db)

        alc_file = base_path / "ALC_Export.csv"
        geo_file = base_path / "Geography.csv"
        xwalk_file = base_path / "xwalk_plus.csv"

        if alc_file.exists() and geo_file.exists():
            ingest_wages(db, alc_file, geo_file)
        else:
            missing = [f for f in [alc_file, geo_file] if not f.exists()]
            print(f"WARNING: Missing files: {[f.name for f in missing]} — skipping wage ingestion.")

        if xwalk_file.exists():
            ingest_crosswalk(db, xwalk_file)
        else:
            print(f"WARNING: {xwalk_file.name} not found — skipping crosswalk ingestion.")

        print("OFLC Ingestion complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest OFLC wage data (ALC_Export + Geography + xwalk_plus)")
    parser.add_argument("--data-dir", type=str, default="./data/oflc",
                        help="Path to folder containing ALC_Export.csv, Geography.csv, xwalk_plus.csv")
    args = parser.parse_args()
    main(args.data_dir)
