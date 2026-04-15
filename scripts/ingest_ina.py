"""INA (Immigration and Nationality Act) Ingestion Script

Parses the US Code Title 8 XML (USLM format from uscode.house.gov) and
extracts key INA sections into the Node tree structure.

The INA is codified in 8 USC Chapter 12. Practitioners cite INA section
numbers (e.g., "INA § 214(i)(1)") which map to USC sections:
  INA § 101 → 8 USC § 1101  (Definitions)
  INA § 203 → 8 USC § 1153  (Immigrant visas — employment-based)
  INA § 212 → 8 USC § 1182  (Inadmissibility)
  INA § 214 → 8 USC § 1184  (Nonimmigrant admission — H-1B, etc.)
  INA § 245 → 8 USC § 1255  (Adjustment of status)

Citation format: "INA § {section}({subsection})"
  e.g., "INA § 214(i)(1)" — also stored as "8 USC § 1184(i)(1)"

Two-phase:
  Phase 1 — Fetch: Download Title 8 XML from uscode.house.gov
  Phase 2 — Ingest: Parse XML into Node tree for specified INA sections

Usage:
    # Fetch Title 8 XML
    python -m scripts.ingest_ina --fetch

    # Ingest key INA sections into database
    python -m scripts.ingest_ina --ingest

    # Fetch and ingest in one step
    python -m scripts.ingest_ina --fetch --ingest

    # Specify custom INA sections
    python -m scripts.ingest_ina --fetch --ingest --sections 101 203 214 245
"""

import argparse
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from src.db.database import Base, SessionLocal, engine
from src.db.models import Node, NodeCrossReference


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Download URL template — update the release point as needed
USC_DOWNLOAD_URL = (
    "https://uscode.house.gov/download/releasepoints/us/pl/119/73not60/"
    "xml_usc08@119-73not60.zip"
)

DEFAULT_DATA_DIR = Path("./data/ina")

# INA section → 8 USC section mapping (Chapter 12 of Title 8)
# This is the standard INA-to-USC crosswalk used in immigration practice.
INA_TO_USC = {
    101: 1101,  # Definitions
    201: 1151,  # Worldwide level of immigration
    202: 1152,  # Per-country numerical limitation
    203: 1153,  # Allocation of immigrant visas (EB categories)
    212: 1182,  # Inadmissible aliens
    214: 1184,  # Admission of nonimmigrants (H-1B, L-1, etc.)
    245: 1255,  # Adjustment of status
    248: 1258,  # Change of nonimmigrant classification
}

# MVP default: the sections most frequently cited in employment immigration
DEFAULT_SECTIONS = [101, 203, 212, 214, 245]

USLM_NS = "http://xml.house.gov/schemas/uslm/1.0"

# USLM element hierarchy for nesting depth
_USLM_LEVELS = {
    "subsection": 0,      # (a), (b), (c)
    "paragraph": 1,       # (1), (2), (3)
    "subparagraph": 2,    # (A), (B), (C)
    "clause": 3,          # (i), (ii), (iii)
    "subclause": 4,       # (I), (II), (III)
    "item": 5,            # (aa), (bb)
}

USER_AGENT = (
    "ImmigrationMCPServer/0.1 (educational research tool; "
    "respects robots.txt; contact: github.com/immigration-mcp-server)"
)


# ---------------------------------------------------------------------------
# Phase 1: Fetch
# ---------------------------------------------------------------------------

def fetch_title8_xml(data_dir: Path):
    """Download Title 8 USC XML from uscode.house.gov."""
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading Title 8 XML from uscode.house.gov...")
    client = httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=120.0,
    )

    try:
        resp = client.get(USC_DOWNLOAD_URL)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  ERROR downloading: {e}")
        return
    finally:
        client.close()

    # Extract XML from zip
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
        if not xml_names:
            print("  ERROR: No XML files found in zip archive")
            return

        xml_name = xml_names[0]
        xml_content = zf.read(xml_name)

        out_path = data_dir / "usc08.xml"
        out_path.write_bytes(xml_content)
        print(f"  Saved: {out_path} ({len(xml_content) // 1024}KB)")


# ---------------------------------------------------------------------------
# Phase 2: XML Parser → Node tree
# ---------------------------------------------------------------------------

def _ns(tag: str) -> str:
    """Add USLM namespace prefix."""
    return f"{{{USLM_NS}}}{tag}"


def _get_text_content(element) -> str:
    """Recursively extract all text from a USLM element.

    Handles mixed content with <ref>, <i>, <sup> etc.
    """
    parts = []
    if element.text:
        parts.append(element.text)
    for child in element:
        # Skip <num> and <heading> — we extract those separately
        if child.tag in (_ns("num"), _ns("heading")):
            if child.tail:
                parts.append(child.tail)
            continue
        # For <ref> elements, include the text
        parts.append(_get_text_content(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _extract_refs(element) -> list[str]:
    """Extract cross-reference targets from <ref> elements."""
    refs = []
    for ref in element.iter(_ns("ref")):
        href = ref.get("href", "")
        if href:
            # Convert USC href to INA citation if possible
            # "/us/usc/t8/s1184" → "INA § 214" or "8 USC § 1184"
            m = re.search(r'/us/usc/t8/s(\d+)', href)
            if m:
                usc_num = int(m.group(1))
                # Reverse lookup: USC → INA
                ina_num = None
                for ina, usc in INA_TO_USC.items():
                    if usc == usc_num:
                        ina_num = ina
                        break
                if ina_num:
                    refs.append(f"INA §{ina_num}")
                else:
                    refs.append(f"8 USC §{usc_num}")
        # Also extract text-based references
        ref_text = ref.get("href", "")
        if not ref_text:
            text = _get_text_content(ref)
            if text:
                refs.append(text)
    return refs


def _get_num_value(element) -> str:
    """Get the <num> value from an element."""
    num_el = element.find(_ns("num"))
    if num_el is not None:
        return num_el.get("value", num_el.text or "").strip()
    return ""


def _get_heading(element) -> str:
    """Get the <heading> text from an element."""
    heading_el = element.find(_ns("heading"))
    if heading_el is not None:
        return _get_text_content(heading_el).strip()
    return ""


def _store_ina_cross_references(db: Session, source_node_id: int, text: str):
    """Extract and store cross-references from INA text."""
    patterns = [
        r'(\d+\s+CFR\s+(?:§\s*)?[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
        r'(§\s*[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
        r'section\s+(\d+(?:\([a-zA-Z0-9]+\))*)\s+of\s+this\s+title',
    ]
    refs = []
    for pattern in patterns:
        refs.extend(re.findall(pattern, text))

    for ref in set(refs):
        db.add(NodeCrossReference(
            source_node_id=source_node_id,
            target_node_id=None,
            reference_text=ref.strip(),
        ))


def _parse_subdivision(
    db: Session,
    element,
    parent_node_id: int,
    base_level: int,
    ina_section: int,
    citation_stack: list[str],
):
    """Recursively parse a USLM subdivision element into Node tree.

    Handles: subsection, paragraph, subparagraph, clause, subclause, item.
    """
    num = _get_num_value(element)
    heading = _get_heading(element)

    # Build citation: INA § 214(i)(1)(A)
    current_stack = citation_stack + [f"({num})"] if num else citation_stack
    ina_citation = f"INA §{ina_section}{''.join(current_stack)}"

    # Build title
    title = f"({num})" if num else "(unnumbered)"
    if heading:
        title = f"({num}) {heading}" if num else heading

    # Determine the element's level type
    local_tag = element.tag.replace(f"{{{USLM_NS}}}", "")
    level_offset = _USLM_LEVELS.get(local_tag, 0)

    node = Node(
        source="ina",
        parent_id=parent_node_id,
        level=base_level + level_offset,
        title=title,
        citation=ina_citation,
        metadata_={
            "ina_section": ina_section,
            "usc_section": INA_TO_USC.get(ina_section),
            "subdivision_type": local_tag,
            "num": num,
        },
    )
    db.add(node)
    db.flush()

    # Check for child subdivisions
    child_types = ["subsection", "paragraph", "subparagraph", "clause", "subclause", "item"]
    has_children = False

    for child_type in child_types:
        for child in element.findall(_ns(child_type)):
            has_children = True
            _parse_subdivision(db, child, node.id, base_level, ina_section, current_stack)

    if has_children:
        # Intermediate node — check for direct <content> text
        content_el = element.find(_ns("content"))
        if content_el is not None:
            content_text = _get_text_content(content_el)
            if content_text:
                # Create intro child for direct content
                intro = Node(
                    source="ina",
                    parent_id=node.id,
                    level=node.level + 1,
                    title=f"{title} — Introduction",
                    full_text=content_text,
                    citation=f"{ina_citation}, Introduction",
                    metadata_={"section_type": "intro"},
                )
                db.add(intro)
                db.flush()
                _store_ina_cross_references(db, intro.id, content_text)

        # Set placeholder summary
        children = db.query(Node).filter(Node.parent_id == node.id).all()
        child_titles = [c.title for c in children[:5]]
        node.summary = f"Contains: {', '.join(child_titles)}"
        if len(children) > 5:
            node.summary += f" and {len(children) - 5} more"
    else:
        # Leaf node — extract content text
        content_el = element.find(_ns("content"))
        if content_el is not None:
            node.full_text = _get_text_content(content_el)
            if node.full_text:
                _store_ina_cross_references(db, node.id, node.full_text)

                # Also extract <ref> elements as cross-references
                for ref_text in _extract_refs(content_el):
                    db.add(NodeCrossReference(
                        source_node_id=node.id,
                        target_node_id=None,
                        reference_text=ref_text,
                    ))
        else:
            # Try getting all text directly
            text = _get_text_content(element)
            if text:
                node.full_text = text
                _store_ina_cross_references(db, node.id, text)
            else:
                node.full_text = "(No content)"

    db.flush()


def parse_ina_section(db: Session, tree: ET.ElementTree, ina_section: int, root_node_id: int):
    """Parse a single INA section from the Title 8 XML into nodes."""
    usc_section = INA_TO_USC.get(ina_section)
    if not usc_section:
        print(f"  INA § {ina_section}: unknown USC mapping, skipping")
        return

    root = tree.getroot()
    target_id = f"/us/usc/t8/s{usc_section}"

    # Find the section element
    section_el = None
    for sect in root.iter(_ns("section")):
        if sect.get("identifier") == target_id:
            section_el = sect
            break

    if section_el is None:
        print(f"  INA § {ina_section} (8 USC § {usc_section}): NOT FOUND in XML")
        return

    # Extract section heading
    heading = _get_heading(section_el)
    section_title = f"INA § {ina_section} — {heading}" if heading else f"INA § {ina_section}"

    section_node = Node(
        source="ina",
        parent_id=root_node_id,
        level=1,
        title=section_title,
        citation=f"INA §{ina_section}",
        metadata_={
            "ina_section": ina_section,
            "usc_section": usc_section,
            "usc_identifier": target_id,
            "also_cited_as": f"8 USC §{usc_section}",
        },
    )
    db.add(section_node)
    db.flush()

    # Parse subsections
    subsection_count = 0
    for subsection in section_el.findall(_ns("subsection")):
        _parse_subdivision(db, subsection, section_node.id, 2, ina_section, [])
        subsection_count += 1

    # Set section summary
    children = db.query(Node).filter(Node.parent_id == section_node.id).all()
    child_titles = [c.title for c in children[:5]]
    section_node.summary = f"{heading}. Contains subsections: {', '.join(child_titles)}"
    if len(children) > 5:
        section_node.summary += f" and {len(children) - 5} more"
    db.flush()

    total = db.query(Node).filter(
        Node.source == "ina",
        Node.citation.like(f"INA §{ina_section}%"),
    ).count()
    print(f"  INA § {ina_section}: {heading[:50]} ({subsection_count} subsections, {total} total nodes)")


# ---------------------------------------------------------------------------
# Ingestion orchestration
# ---------------------------------------------------------------------------

def clear_ina_nodes(db: Session):
    """Clear existing INA nodes (preserves other sources)."""
    ina_node_ids = [
        n.id for n in db.query(Node.id).filter(Node.source == "ina").all()
    ]
    if ina_node_ids:
        db.query(NodeCrossReference).filter(
            NodeCrossReference.source_node_id.in_(ina_node_ids)
            | NodeCrossReference.target_node_id.in_(ina_node_ids)
        ).delete(synchronize_session=False)
    db.query(Node).filter(Node.source == "ina").delete()
    db.commit()


def ingest_ina(db: Session, data_dir: Path, sections: list[int]):
    """Parse Title 8 XML and ingest specified INA sections."""
    xml_path = data_dir / "usc08.xml"
    if not xml_path.exists():
        print(f"Error: {xml_path} not found. Run with --fetch first.")
        return

    print(f"Parsing Title 8 XML: {xml_path}")
    tree = ET.parse(str(xml_path))

    # Create INA root node
    root_node = Node(
        source="ina",
        parent_id=None,
        level=0,
        title="Immigration and Nationality Act (INA)",
        summary="The Immigration and Nationality Act — the primary federal statute governing immigration to the United States. Codified in 8 USC Chapter 12.",
        citation="INA",
        metadata_={
            "doc_type": "ina",
            "codified_at": "8 USC Chapter 12",
            "also_known_as": "McCarran-Walter Act",
        },
    )
    db.add(root_node)
    db.flush()

    for ina_section in sorted(sections):
        parse_ina_section(db, tree, ina_section, root_node.id)

    db.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch and ingest INA (Immigration and Nationality Act) sections",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch Title 8 XML from uscode.house.gov
  python -m scripts.ingest_ina --fetch

  # Ingest key INA sections
  python -m scripts.ingest_ina --ingest

  # Fetch and ingest with custom sections
  python -m scripts.ingest_ina --fetch --ingest --sections 101 203 214 245
        """,
    )
    parser.add_argument(
        "--fetch", action="store_true",
        help="Download Title 8 USC XML",
    )
    parser.add_argument(
        "--ingest", action="store_true",
        help="Parse XML and ingest INA sections into database",
    )
    parser.add_argument(
        "--sections", type=int, nargs="+", default=DEFAULT_SECTIONS,
        help=f"INA section numbers to ingest (default: {DEFAULT_SECTIONS})",
    )
    parser.add_argument(
        "--data-dir", type=str, default=str(DEFAULT_DATA_DIR),
        help=f"Cache directory (default: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()

    if not args.fetch and not args.ingest:
        parser.print_help()
        print("\nError: specify --fetch, --ingest, or both.")
        return

    data_dir = Path(args.data_dir)

    if args.fetch:
        print(f"=== Fetching Title 8 USC XML ===")
        fetch_title8_xml(data_dir)

    if args.ingest:
        print(f"\n=== Ingesting INA Sections: {args.sections} ===")

        if not (data_dir / "usc08.xml").exists():
            print(f"Error: {data_dir / 'usc08.xml'} not found. Run with --fetch first.")
            return

        Base.metadata.create_all(bind=engine)

        with SessionLocal() as db:
            print("Clearing existing INA nodes...")
            clear_ina_nodes(db)
            ingest_ina(db, data_dir, args.sections)

            # Stats
            total = db.query(Node).filter(Node.source == "ina").count()
            leaves = db.query(Node).filter(
                Node.source == "ina",
                Node.full_text.isnot(None),
            ).count()
            print(f"\n=== Ingestion Complete ===")
            print(f"Total INA nodes: {total}")
            print(f"Leaf nodes: {leaves}")
            print("\nNext steps:")
            print("  python -m scripts.resolve_crossrefs  # Resolve cross-references")
            print("  python -m scripts.summarize_tree      # Generate LLM summaries")


if __name__ == "__main__":
    main()
