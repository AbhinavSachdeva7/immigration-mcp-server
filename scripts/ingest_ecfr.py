"""eCFR (Electronic Code of Federal Regulations) Ingestion Script

Two-phase ingestion of Title 8 CFR from the eCFR API (ecfr.gov):

  Phase 1 — Fetch: Download the Title 8 structure (JSON) and section-level
                    HTML content for specified parts. Cache locally.
  Phase 2 — Ingest: Parse cached data into the Node tree structure.

The eCFR is the authoritative, up-to-date version of the Code of Federal
Regulations. For immigration law, the relevant regulations are in Title 8,
primarily Parts 204 (employment-based immigrants), 214 (nonimmigrants/H-1B),
and 245 (adjustment of status).

Citation format: "8 CFR § {section}({subsection})"
  e.g., "8 CFR § 214.2(h)(4)(ii)"

API endpoints used:
  - Structure: GET /api/versioner/v1/structure/current/title-8.json
  - Content:   GET /api/renderer/v1/content/enhanced/current/title-8?part=214&section=214.2

Usage:
    # Fetch Parts 204, 214, 245 from eCFR
    python -m scripts.ingest_ecfr --fetch --parts 204 214 245

    # Ingest cached data into database
    python -m scripts.ingest_ecfr --ingest

    # Fetch and ingest in one step
    python -m scripts.ingest_ecfr --fetch --ingest --parts 204 214 245

    # Fetch all of Title 8 Chapter I (immigration regulations)
    python -m scripts.ingest_ecfr --fetch --all-chapter-i

Rate limiting: 2-second delay between requests (be respectful to eCFR servers).
"""

import argparse
import json
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from sqlalchemy.orm import Session

from src.db.database import Base, SessionLocal, engine
from src.db.models import Node, NodeCrossReference


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ECFR_BASE = "https://www.ecfr.gov"
STRUCTURE_URL = f"{ECFR_BASE}/api/versioner/v1/structure/current/title-8.json"
CONTENT_URL = f"{ECFR_BASE}/api/renderer/v1/content/enhanced/current/title-8"

DEFAULT_DATA_DIR = Path("./data/ecfr")
DEFAULT_PARTS = [204, 214, 245]

# Polite crawling
REQUEST_DELAY = 2.0
USER_AGENT = (
    "ImmigrationMCPServer/0.1 (educational research tool; "
    "respects robots.txt; contact: github.com/immigration-mcp-server)"
)


# ---------------------------------------------------------------------------
# Phase 1: Fetcher
# ---------------------------------------------------------------------------

class ECFRFetcher:
    """Downloads eCFR structure and content, caching locally."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=60.0,
        )
        self._request_count = 0

    def close(self):
        self.client.close()

    def _fetch(self, url: str) -> httpx.Response | None:
        """Fetch a URL with rate limiting."""
        if self._request_count > 0:
            time.sleep(REQUEST_DELAY)
        self._request_count += 1

        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            print(f"  HTTP {e.response.status_code} fetching {url}")
            return None
        except httpx.RequestError as e:
            print(f"  Request error: {e}")
            return None

    def fetch_structure(self) -> dict | None:
        """Fetch the Title 8 structure tree (JSON)."""
        print(f"Fetching Title 8 structure from eCFR...")
        resp = self._fetch(STRUCTURE_URL)
        if not resp:
            return None

        structure = resp.json()

        # Cache locally
        out = self.data_dir / "title-8-structure.json"
        out.write_text(json.dumps(structure, indent=2), encoding="utf-8")
        print(f"  Structure cached: {out}")
        return structure

    def _find_sections_in_structure(self, node: dict, target_part: int) -> list[dict]:
        """Recursively find all section nodes for a given part number."""
        sections = []

        if node.get("type") == "section":
            # Check if this section belongs to our target part
            identifier = node.get("identifier", "")
            if identifier.startswith(f"{target_part}."):
                sections.append(node)
            return sections

        for child in node.get("children", []):
            # Only descend into the right part
            if child.get("type") == "part":
                if child.get("identifier") != str(target_part):
                    continue
            sections.extend(self._find_sections_in_structure(child, target_part))

        return sections

    def _find_part_node(self, structure: dict, part_num: int) -> dict | None:
        """Find the part node in the structure tree."""
        def search(node: dict) -> dict | None:
            if node.get("type") == "part" and node.get("identifier") == str(part_num):
                return node
            for child in node.get("children", []):
                result = search(child)
                if result:
                    return result
            return None
        return search(structure)

    def fetch_part(self, structure: dict, part_num: int) -> dict:
        """Fetch all section content for a given CFR part.

        Returns a manifest dict for this part.
        """
        part_dir = self.data_dir / f"part-{part_num}"
        part_dir.mkdir(parents=True, exist_ok=True)

        # Find the part in the structure tree and save its subtree
        part_node = self._find_part_node(structure, part_num)
        if not part_node:
            print(f"  Part {part_num} not found in Title 8 structure!")
            return {"part": part_num, "sections": [], "error": "not found in structure"}

        # Save part structure
        (part_dir / "structure.json").write_text(
            json.dumps(part_node, indent=2), encoding="utf-8"
        )

        # Find all sections
        sections = self._find_sections_in_structure(structure, part_num)
        print(f"  Part {part_num}: found {len(sections)} sections in structure")

        manifest = {"part": part_num, "sections": []}

        for sect in sections:
            sect_id = sect.get("identifier", "")
            if not sect_id:
                continue

            # Fetch section HTML
            url = f"{CONTENT_URL}?part={part_num}&section={sect_id}"
            resp = self._fetch(url)
            if not resp:
                print(f"    {sect_id}: FAILED")
                continue

            html = resp.text

            # Save section HTML
            safe_name = sect_id.replace(".", "_")
            html_path = part_dir / f"section-{safe_name}.html"
            html_path.write_text(html, encoding="utf-8")

            manifest["sections"].append({
                "identifier": sect_id,
                "label": sect.get("label", ""),
                "label_description": sect.get("label_description", ""),
                "file": str(html_path),
            })
            print(f"    § {sect_id}: {sect.get('label_description', '')[:60]}")

        # Save part manifest
        (part_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        return manifest

    def fetch_parts(self, part_nums: list[int]) -> dict:
        """Fetch structure + content for specified parts."""
        structure = self.fetch_structure()
        if not structure:
            return {"error": "Failed to fetch structure"}

        master_manifest = {"title": 8, "parts": []}

        for part_num in part_nums:
            print(f"\n--- Fetching Part {part_num} ---")
            part_manifest = self.fetch_part(structure, part_num)
            master_manifest["parts"].append(part_manifest)

        # Save master manifest
        manifest_path = self.data_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(master_manifest, indent=2), encoding="utf-8"
        )

        total_sections = sum(len(p["sections"]) for p in master_manifest["parts"])
        print(f"\nFetch complete. {total_sections} sections cached across {len(part_nums)} parts.")
        return master_manifest


# ---------------------------------------------------------------------------
# Phase 2: HTML Parser → Node tree
# ---------------------------------------------------------------------------

def _extract_section_text(html: str) -> list[dict]:
    """Parse eCFR section HTML into structured paragraphs.

    eCFR HTML uses semantic CSS classes for paragraph levels:
      div.section — section container
      div.section-heading — section number and subject
      p.indent-1 through p.indent-7 — paragraph nesting levels
      span.paragraph-hierarchy — paragraph markers like (a), (1), (ii)

    Returns list of {"marker": str, "text": str, "indent": int}
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find the section content — eCFR wraps it in div.section
    section_div = soup.select_one("div.section")
    if not section_div:
        # Fallback: try the whole body
        section_div = soup.find("body") or soup
        if not section_div:
            return []

    paragraphs = []

    for el in section_div.find_all(["p", "div"]):
        # Skip nested divs that aren't paragraph containers
        if el.name == "div" and "authority" not in el.get("class", []):
            continue

        classes = el.get("class", [])

        # Determine indent level from CSS class
        indent = 0
        for cls in classes:
            m = re.match(r"indent-(\d+)", cls)
            if m:
                indent = int(m.group(1))
                break

        # Extract paragraph marker from span.paragraph-hierarchy
        marker = ""
        hierarchy_span = el.select_one("span.paragraph-hierarchy")
        if hierarchy_span:
            marker_text = hierarchy_span.get_text(strip=True)
            # Extract the marker: "(a)" → "a", "(1)" → "1"
            m = re.match(r"^\(([^)]+)\)", marker_text)
            if m:
                marker = m.group(1)

        # Get full text, removing the hierarchy span to avoid duplication
        if hierarchy_span:
            hierarchy_span.decompose()

        text = el.get_text(" ", strip=True)
        if not text:
            continue

        paragraphs.append({
            "marker": marker,
            "text": text,
            "indent": indent,
        })

    return paragraphs


def _extract_section_heading(html: str) -> tuple[str, str]:
    """Extract section number and subject from eCFR HTML.

    The eCFR renderer uses h4 tags with a data-hierarchy-metadata attribute:
      <h4 data-hierarchy-metadata='{"path":"...","citation":"8 CFR 214.1"}'>
        § 214.1 Requirements for admission...
      </h4>

    Returns (section_number, subject).
    e.g., ("214.1", "Requirements for admission, extension, and maintenance of status")
    """
    soup = BeautifulSoup(html, "html.parser")

    # Primary: find heading with data-hierarchy-metadata (most reliable)
    heading = soup.find(attrs={"data-hierarchy-metadata": True})
    if heading:
        # Try to extract citation from metadata attribute
        try:
            meta = json.loads(heading["data-hierarchy-metadata"])
            citation = meta.get("citation", "")
            # "8 CFR 214.1" → "214.1"
            m = re.search(r'CFR\s+([\d.]+)', citation)
            if m:
                sect_num = m.group(1)
                # Get subject from text: "§ 214.1 Subject line here."
                full_text = heading.get_text(strip=True)
                m2 = re.match(r'§\s*[\d.]+\s*(.*)', full_text)
                subject = m2.group(1).rstrip(".") if m2 else ""
                return sect_num, subject
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: scan for § pattern in any heading tag
    for tag in ["h4", "h3", "h2", "h1"]:
        el = soup.find(tag)
        if el:
            text = el.get_text(strip=True)
            m = re.match(r'§\s*([\d.]+)\s*(.*)', text)
            if m:
                return m.group(1), m.group(2).rstrip(".")

    return "", ""


def _classify_marker(marker: str) -> str:
    """Classify a paragraph marker for CFR nesting hierarchy.

    CFR paragraph designation order:
      Level 1: lowercase letters (a), (b), (c)
      Level 2: numbers (1), (2), (3)
      Level 3: roman numerals (i), (ii), (iii)
      Level 4: uppercase letters (A), (B), (C)
      Level 5: italic numbers (rarely used)
    """
    if not marker:
        return "none"
    if re.match(r'^[ivxlc]+$', marker) and not re.match(r'^[a-z]$', marker):
        return "roman"
    if re.match(r'^[a-z]+$', marker):
        return "lower"
    if re.match(r'^[0-9]+$', marker):
        return "number"
    if re.match(r'^[A-Z]+$', marker):
        return "upper"
    return "other"


_MARKER_DEPTH = {"lower": 0, "number": 1, "roman": 2, "upper": 3, "other": 4}


def _build_paragraph_tree(
    db: Session,
    paragraphs: list[dict],
    section_node_id: int,
    section_num: str,
    base_level: int,
):
    """Build a Node tree from eCFR section paragraphs.

    Uses indent levels and markers to reconstruct the CFR paragraph hierarchy.
    Creates nodes with proper citations like "8 CFR § 214.2(h)(4)(ii)".
    """
    if not paragraphs:
        return

    has_markers = any(p["marker"] for p in paragraphs)

    if not has_markers:
        # No paragraph markers — store as section full_text
        all_text = "\n\n".join(p["text"] for p in paragraphs)
        node = db.query(Node).filter(Node.id == section_node_id).first()
        if node:
            node.full_text = all_text
        db.flush()
        _store_ecfr_cross_references(db, section_node_id, all_text)
        return

    # Stack: [(marker, node_id, marker_type)]
    stack: list[tuple[str, int, str]] = []
    accumulated_text: list[str] = []
    current_node_id = section_node_id

    for para in paragraphs:
        marker = para["marker"]
        text = para["text"]

        if not marker:
            # Continuation text
            accumulated_text.append(text)
            continue

        # Flush accumulated text
        if accumulated_text:
            _append_text_to_node(db, current_node_id, "\n\n".join(accumulated_text))
            accumulated_text = []

        marker_type = _classify_marker(marker)
        marker_depth = _MARKER_DEPTH.get(marker_type, 4)

        # Pop stack to find correct parent
        while stack and _MARKER_DEPTH.get(stack[-1][2], 4) >= marker_depth:
            stack.pop()

        parent_id = stack[-1][1] if stack else section_node_id

        # Build citation: 8 CFR § 214.2(h)(4)(ii)
        citation_parts = [f"({s[0]})" for s in stack]
        citation_parts.append(f"({marker})")
        citation = f"8 CFR §{section_num}{''.join(citation_parts)}" if section_num else None

        # Build title from marker + start of text
        title = f"({marker})"
        if text:
            title_match = re.match(r'^([^.—–\-]{1,80})[.—–\-]', text)
            if title_match:
                title = f"({marker}) {title_match.group(1).strip()}"
            else:
                title = f"({marker}) {text[:80]}{'...' if len(text) > 80 else ''}"

        sub_node = Node(
            source="ecfr",
            parent_id=parent_id,
            level=base_level + 1 + len(stack),
            title=title,
            full_text=text if text else None,
            citation=citation,
            metadata_={"marker": marker, "marker_type": marker_type},
        )
        db.add(sub_node)
        db.flush()

        if text:
            _store_ecfr_cross_references(db, sub_node.id, text)

        stack.append((marker, sub_node.id, marker_type))
        current_node_id = sub_node.id

    # Flush remaining text
    if accumulated_text:
        _append_text_to_node(db, current_node_id, "\n\n".join(accumulated_text))

    # Set summary on section node
    section = db.query(Node).filter(Node.id == section_node_id).first()
    if section and not section.summary:
        children = db.query(Node).filter(Node.parent_id == section_node_id).all()
        child_titles = [c.title for c in children[:5]]
        section.summary = f"Contains subsections: {', '.join(child_titles)}"
        if len(children) > 5:
            section.summary += f" and {len(children) - 5} more"
    db.flush()


def _append_text_to_node(db: Session, node_id: int, text: str):
    """Append text to a node's full_text."""
    if not text:
        return
    node = db.query(Node).filter(Node.id == node_id).first()
    if node:
        if node.full_text:
            node.full_text += "\n\n" + text
        else:
            node.full_text = text
        _store_ecfr_cross_references(db, node_id, text)
    db.flush()


def _store_ecfr_cross_references(db: Session, source_node_id: int, text: str):
    """Extract and store cross-references from eCFR text.

    Common patterns in CFR text:
      - "8 CFR 214.2(h)"
      - "§ 214.2(h)(19)"
      - "paragraph (h)(19)(ii)"
      - "INA section 214(g)"
      - "section 101(a)(15)(H)(i)(b) of the Act"
    """
    patterns = [
        r'(\d+\s+CFR\s+(?:§\s*)?[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
        r'(?:see|See|under|per)\s+(§\s*[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
        r'(?:paragraph|Paragraph)\s+(\([a-zA-Z0-9]+\)(?:\([a-zA-Z0-9]+\))*)',
        r'(INA\s+(?:§\s*|section\s+)?[\d]+(?:\([a-zA-Z0-9]+\))*)',
        r'(?:section|Section)\s+([\d]+(?:\([a-zA-Z0-9]+\))*)\s+of\s+the\s+Act',
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


# ---------------------------------------------------------------------------
# Ingestion orchestration
# ---------------------------------------------------------------------------

def clear_ecfr_nodes(db: Session):
    """Clear existing eCFR nodes (preserves other sources)."""
    ecfr_node_ids = [
        n.id for n in db.query(Node.id).filter(Node.source == "ecfr").all()
    ]
    if ecfr_node_ids:
        db.query(NodeCrossReference).filter(
            NodeCrossReference.source_node_id.in_(ecfr_node_ids)
            | NodeCrossReference.target_node_id.in_(ecfr_node_ids)
        ).delete(synchronize_session=False)
    db.query(Node).filter(Node.source == "ecfr").delete()
    db.commit()


def ingest_from_cache(db: Session, data_dir: Path):
    """Parse cached eCFR data into the Node tree.

    Tree structure:
      Level 0: "Title 8 — Aliens and Nationality" (root)
      Level 1: Part (e.g., "Part 214 — Nonimmigrant Workers")
      Level 2: Section (e.g., "§ 214.2 Special requirements...")
      Level 3+: Paragraph subsections (a), (1), (i), (A)
    """
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json found in {data_dir}. Run with --fetch first.")
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parts_data = manifest.get("parts", [])

    if not parts_data:
        print("No parts found in manifest.")
        return

    # Create Title 8 root node
    title_node = Node(
        source="ecfr",
        parent_id=None,
        level=0,
        title="Title 8 — Aliens and Nationality",
        summary="Code of Federal Regulations, Title 8: immigration and nationality regulations.",
        citation="8 CFR",
        metadata_={"doc_type": "ecfr", "cfr_title": "8"},
    )
    db.add(title_node)
    db.flush()

    for part_data in parts_data:
        part_num = part_data["part"]
        sections = part_data.get("sections", [])

        if part_data.get("error"):
            print(f"  Part {part_num}: skipped (error during fetch)")
            continue

        # Read part structure for metadata
        part_dir = data_dir / f"part-{part_num}"
        part_structure_path = part_dir / "structure.json"
        part_label = f"Part {part_num}"
        if part_structure_path.exists():
            ps = json.loads(part_structure_path.read_text(encoding="utf-8"))
            part_label = ps.get("label", part_label)
            if ps.get("label_description"):
                part_label += f" — {ps['label_description']}"

        # Create part node
        part_node = Node(
            source="ecfr",
            parent_id=title_node.id,
            level=1,
            title=part_label,
            summary=f"8 CFR Part {part_num}: {part_label}",
            citation=f"8 CFR Part {part_num}",
            metadata_={"cfr_title": "8", "cfr_part": str(part_num)},
        )
        db.add(part_node)
        db.flush()
        print(f"\n  Part {part_num}: {part_label}")

        # Check for subpart structure
        subpart_parents = _build_subpart_nodes(db, data_dir, part_num, part_node.id)

        for sect_data in sections:
            sect_id = sect_data["identifier"]
            label_desc = sect_data.get("label_description", "")
            file_path = Path(sect_data["file"])

            if not file_path.exists():
                print(f"    § {sect_id}: MISSING FILE")
                continue

            html = file_path.read_text(encoding="utf-8")

            # Extract heading from HTML (more reliable than structure metadata)
            sect_num, subject = _extract_section_heading(html)
            if not sect_num:
                sect_num = sect_id
            if not subject:
                subject = label_desc

            section_title = f"§ {sect_num}"
            if subject:
                section_title += f" {subject}"

            # Determine parent — subpart node or part node
            parent_id = _find_subpart_parent(subpart_parents, sect_num, part_node.id)

            section_node = Node(
                source="ecfr",
                parent_id=parent_id,
                level=2,
                title=section_title,
                citation=f"8 CFR §{sect_num}",
                metadata_={
                    "cfr_title": "8",
                    "cfr_part": str(part_num),
                    "section": sect_num,
                    "subject": subject,
                },
            )
            db.add(section_node)
            db.flush()

            # Parse section content into paragraph tree
            paragraphs = _extract_section_text(html)
            _build_paragraph_tree(db, paragraphs, section_node.id, sect_num, base_level=2)

            child_count = db.query(Node).filter(Node.parent_id == section_node.id).count()
            print(f"    § {sect_num}: {subject[:50] if subject else '(no subject)'} ({child_count} subsections)")

        db.flush()

    db.commit()


def _build_subpart_nodes(
    db: Session, data_dir: Path, part_num: int, part_node_id: int
) -> dict[str, int]:
    """Create subpart nodes from the part structure if subparts exist.

    Returns a mapping of subpart identifier → node_id.
    """
    part_structure_path = data_dir / f"part-{part_num}" / "structure.json"
    if not part_structure_path.exists():
        return {}

    structure = json.loads(part_structure_path.read_text(encoding="utf-8"))
    subpart_map = {}

    for child in structure.get("children", []):
        if child.get("type") == "subpart":
            sp_id = child.get("identifier", "")
            sp_label = child.get("label", f"Subpart {sp_id}")
            if child.get("label_description"):
                sp_label += f" — {child['label_description']}"

            sp_node = Node(
                source="ecfr",
                parent_id=part_node_id,
                level=1,
                title=sp_label,
                summary=f"8 CFR Part {part_num}, {sp_label}",
                citation=f"8 CFR Part {part_num}, Subpart {sp_id}",
                metadata_={"cfr_part": str(part_num), "subpart": sp_id},
            )
            db.add(sp_node)
            db.flush()

            # Map section ranges to this subpart
            desc_range = child.get("descendant_range", "")
            if desc_range:
                subpart_map[sp_id] = {
                    "node_id": sp_node.id,
                    "range": desc_range,
                }

    return subpart_map


def _find_subpart_parent(
    subpart_map: dict, section_num: str, default_parent: int
) -> int:
    """Find which subpart a section belongs to based on section number ranges."""
    if not subpart_map:
        return default_parent

    for sp_id, info in subpart_map.items():
        range_str = info.get("range", "")
        if not range_str:
            continue

        # Range format from eCFR: "214.1 to 214.99" or similar
        parts = range_str.split(" to ")
        if len(parts) == 2:
            try:
                start = float(parts[0].strip())
                end = float(parts[1].strip())
                sect_float = float(section_num)
                if start <= sect_float <= end:
                    return info["node_id"]
            except ValueError:
                continue

    return default_parent


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch and ingest eCFR Title 8 (immigration regulations)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch Parts 204, 214, 245
  python -m scripts.ingest_ecfr --fetch --parts 204 214 245

  # Ingest cached data into database
  python -m scripts.ingest_ecfr --ingest

  # Both in one step
  python -m scripts.ingest_ecfr --fetch --ingest --parts 204 214 245
        """,
    )
    parser.add_argument(
        "--fetch", action="store_true",
        help="Download eCFR structure and content",
    )
    parser.add_argument(
        "--ingest", action="store_true",
        help="Parse cached data into the database Node tree",
    )
    parser.add_argument(
        "--parts", type=int, nargs="+", default=DEFAULT_PARTS,
        help=f"CFR part numbers to fetch (default: {DEFAULT_PARTS})",
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
        print(f"=== Fetching eCFR Title 8 (Parts: {args.parts}) ===")
        print(f"Cache directory: {data_dir}")
        print(f"Rate limit: {REQUEST_DELAY}s between requests\n")

        fetcher = ECFRFetcher(data_dir)
        try:
            fetcher.fetch_parts(args.parts)
        finally:
            fetcher.close()

    if args.ingest:
        print(f"\n=== Ingesting eCFR from {data_dir} ===")

        if not data_dir.exists():
            print(f"Error: {data_dir} does not exist. Run with --fetch first.")
            return

        Base.metadata.create_all(bind=engine)

        with SessionLocal() as db:
            print("Clearing existing eCFR nodes...")
            clear_ecfr_nodes(db)
            ingest_from_cache(db, data_dir)

            # Stats
            total = db.query(Node).filter(Node.source == "ecfr").count()
            leaves = db.query(Node).filter(
                Node.source == "ecfr",
                Node.full_text.isnot(None),
            ).count()
            xrefs = db.query(NodeCrossReference).filter(
                NodeCrossReference.source_node_id.in_(
                    db.query(Node.id).filter(Node.source == "ecfr")
                )
            ).count()
            print(f"\n=== Ingestion Complete ===")
            print(f"Total eCFR nodes: {total}")
            print(f"Leaf nodes: {leaves}")
            print(f"Cross-references: {xrefs}")
            print("\nNext steps:")
            print("  python -m scripts.resolve_crossrefs  # Resolve cross-references")
            print("  python -m scripts.summarize_tree      # Generate LLM summaries")


if __name__ == "__main__":
    main()
