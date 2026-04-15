import csv
import argparse
from pathlib import Path
from sqlalchemy.orm import Session
from src.db.database import SessionLocal, engine, Base
from src.db.models import MSAMapping

NON_METRO_CBSA = 99999  # HUD uses 99999 for ZIP codes outside any MSA


def clear_msa_tables(db: Session):
    """Clear existing MSA data to make ingestion idempotent."""
    db.query(MSAMapping).delete()
    db.commit()


def _load_geography(geo_path: Path) -> dict:
    """Load Geography.csv into a dict mapping numeric area code (str) → MSA area name.

    Reuses the same Geography.csv from the OFLC wage data directory.
    """
    geo = {}
    with open(geo_path, 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            geo[row['Area'].strip()] = row['AreaName'].strip()
    print(f"  Loaded {len(geo):,} area mappings from {geo_path.name}")
    return geo


def ingest_msa_mapping(db: Session, xlsx_path: Path, geo_path: Path):
    """Ingest HUD ZIP-CBSA crosswalk (ZIP_CBSA_MMYYYY.xlsx).

    HUD file columns: ZIP, CBSA, USPS_ZIP_PREF_CITY, USPS_ZIP_PREF_STATE,
                      RES_RATIO, BUS_RATIO, OTH_RATIO, TOT_RATIO

    - A ZIP can span multiple CBSAs (e.g., border ZIPs). We keep the dominant
      CBSA per ZIP (highest TOT_RATIO) so resolve_msa returns one MSA per ZIP.
    - CBSA=99999 means non-metropolitan — no MSA name available, skipped.
    - ZIP codes are zero-padded to 5 digits (e.g., 501 → '00501').
    - CBSA numeric codes are joined with Geography.csv to get OFLC MSA names.
    """
    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas is required — run: pip install pandas openpyxl")
        return

    geo = _load_geography(geo_path)

    print(f"Ingesting HUD ZIP-CBSA crosswalk from {xlsx_path.name}...")
    df = pd.read_excel(xlsx_path, dtype={'ZIP': str, 'CBSA': str})

    # Zero-pad ZIP to 5 digits
    df['ZIP'] = df['ZIP'].str.zfill(5)

    # Drop non-metro (CBSA=99999) — no MSA name to map to
    before = len(df)
    df = df[df['CBSA'] != str(NON_METRO_CBSA)]
    print(f"  Dropped {before - len(df):,} non-metro rows (CBSA=99999), {len(df):,} remaining")

    # For ZIPs spanning multiple CBSAs, keep the dominant one (highest TOT_RATIO)
    df = df.sort_values('TOT_RATIO', ascending=False).drop_duplicates(subset='ZIP', keep='first')
    print(f"  {len(df):,} unique ZIPs after deduplication")

    # Join CBSA code → OFLC MSA area name from Geography.csv
    df['msa_area'] = df['CBSA'].map(geo)

    no_match = df['msa_area'].isna().sum()
    df = df.dropna(subset=['msa_area'])
    print(f"  {no_match:,} ZIPs had no Geography match (skipped), {len(df):,} will be inserted")

    # Bulk insert
    batch = []
    inserted = 0
    for _, row in df.iterrows():
        batch.append(MSAMapping(
            zip_code=row['ZIP'],
            city_name=str(row['USPS_ZIP_PREF_CITY']).strip().title(),
            state_abbr=str(row['USPS_ZIP_PREF_STATE']).strip().upper(),
            msa_area=row['msa_area'],
        ))

        if len(batch) >= 2000:
            db.bulk_save_objects(batch)
            db.commit()
            inserted += len(batch)
            batch = []

    if batch:
        db.bulk_save_objects(batch)
        db.commit()
        inserted += len(batch)

    print(f"  MSA mapping: {inserted:,} rows inserted.")


def main(data_dir: str, geo_dir: str):
    base_path = Path(data_dir)
    geo_path = Path(geo_dir) / "Geography.csv"

    if not base_path.exists():
        print(f"Data directory {data_dir} not found.")
        return

    if not geo_path.exists():
        print(f"Geography.csv not found at {geo_path} — needed to resolve CBSA codes to MSA names.")
        print("  It lives in the OFLC data directory (data/oflc/Geography.csv).")
        return

    Base.metadata.create_all(bind=engine)

    # Find the HUD xlsx — accept any ZIP_CBSA_*.xlsx in the zip dir
    xlsx_candidates = sorted(base_path.glob("ZIP_CBSA_*.xlsx"))
    if not xlsx_candidates:
        print(f"No ZIP_CBSA_*.xlsx file found in {data_dir}.")
        print("  Download from: https://www.huduser.gov/portal/datasets/usps_crosswalk.html")
        return

    xlsx_path = xlsx_candidates[-1]  # use most recent if multiple
    print(f"Using: {xlsx_path.name}")

    with SessionLocal() as db:
        print("Clearing existing MSA tables...")
        clear_msa_tables(db)
        ingest_msa_mapping(db, xlsx_path, geo_path)
        print("MSA Ingestion complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest HUD ZIP-CBSA crosswalk into MSA mapping table")
    parser.add_argument("--data-dir", type=str, default="./data/zip",
                        help="Directory containing ZIP_CBSA_*.xlsx")
    parser.add_argument("--geo-dir", type=str, default="./data/oflc",
                        help="Directory containing Geography.csv (from OFLC wage data)")
    args = parser.parse_args()
    main(args.data_dir, args.geo_dir)
