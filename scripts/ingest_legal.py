"""Federal Register XML Ingestion Script

Parses Federal Register XML (GPO/GovInfo format) into the Node tree structure.
Handles three main document sections:
  1. Preamble (PREAMB) — metadata fields (summary, dates, agency info)
  2. Supplementary Information (SUPLINF) — discussion organized by HD headings
  3. Regulatory Text (REGTEXT) — actual CFR amendments with section/paragraph structure

Citation strategy:
  - Preamble fields: "{doc_id}, Preamble, {field_name}"
  - SUPLINF headings: "{doc_id}, {heading_path}" (e.g., "II. Discussion, A. General Comments")
  - REGTEXT sections: "{title} CFR §{part}.{section}" with subsection detail

Cross-references (§ patterns) are extracted and stored with target_node_id=None,
to be resolved in a separate pass (Phase 2c).
"""

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from src.db.database import Base, SessionLocal, engine
from src.db.models import Node, NodeCrossReference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_all_text(element) -> str:
    """Recursively extract all text content from an XML element and its children.

    Handles mixed content like: <P>Text <E T="03">emphasized</E> more text</P>
    """
    parts = []
    if element.text:
        parts.append(element.text.strip())
    for child in element:
        parts.append(_get_all_text(child))
        if child.tail:
            parts.append(child.tail.strip())
    return " ".join(p for p in parts if p)


def _collect_paragraphs(element) -> str:
    """Collect text from all <P> children (and direct text) of an element."""
    texts = []
    for child in element:
        if child.tag == "P":
            p_text = _get_all_text(child)
            if p_text:
                texts.append(p_text)
    return "\n\n".join(texts)


def _extract_cross_references(text: str) -> list[str]:
    """Find cross-reference patterns in legal text.

    Looks for patterns like:
      - "see § 214.2(h)(19)"
      - "§ 214.2"
      - "8 CFR 214.2(h)"
      - "section 214(g)(3)"
      - "paragraph (h)(19)(ii)"
    """
    patterns = [
        r'(?:see|See|under|per)\s+(§\s*[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
        r'(\d+\s+CFR\s+(?:§\s*)?[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
        r'(?:section|Section)\s+([\d]+(?:\([a-zA-Z0-9]+\))*\s+of\s+the\s+\w+)',
        r'(?:paragraph|Paragraph)\s+(\([a-zA-Z0-9]+\)(?:\([a-zA-Z0-9]+\))*)',
    ]
    refs = []
    for pattern in patterns:
        refs.extend(re.findall(pattern, text))
    return list(set(refs))


def _store_cross_references(db: Session, source_node_id: int, text: str):
    """Extract and store cross-references from text. target_node_id left None for Phase 2c."""
    refs = _extract_cross_references(text)
    for ref in refs:
        db.add(NodeCrossReference(
            source_node_id=source_node_id,
            target_node_id=None,
            reference_text=ref.strip(),
        ))


# ---------------------------------------------------------------------------
# Document metadata extraction
# ---------------------------------------------------------------------------

def _extract_document_metadata(root) -> dict:
    """Extract document-level metadata from the XML root and preamble."""
    meta = {
        "doc_type": root.tag,  # RULE, PRORULE, NOTICE, etc.
    }

    preamb = root.find("PREAMB")
    if preamb is None:
        return meta

    # Agency
    agency_el = preamb.find("AGENCY")
    if agency_el is not None:
        meta["agency"] = _get_all_text(agency_el)

    # Sub-agency
    subagy_el = preamb.find("SUBAGY")
    if subagy_el is not None:
        meta["sub_agency"] = _get_all_text(subagy_el)

    # CFR title and parts
    cfr_el = preamb.find("CFR")
    if cfr_el is not None:
        title_el = cfr_el.find("TITLE")
        if title_el is not None and title_el.text:
            meta["cfr_title"] = title_el.text.strip()
        parts_el = cfr_el.find("PARTNOS")
        if parts_el is not None and parts_el.text:
            meta["cfr_parts"] = parts_el.text.strip()

    # Document number
    depdoc_el = preamb.find("DEPDOC")
    if depdoc_el is not None and depdoc_el.text:
        meta["doc_number"] = depdoc_el.text.strip()

    # RIN
    rin_el = preamb.find("RIN")
    if rin_el is not None and rin_el.text:
        meta["rin"] = rin_el.text.strip()

    # Subject (document title)
    subj_el = preamb.find("SUBJECT")
    if subj_el is not None:
        meta["subject"] = _get_all_text(subj_el)

    return meta


def _make_doc_citation_prefix(meta: dict) -> str:
    """Build a citation prefix from document metadata.

    e.g., "88 FR 1234" or just the doc number.
    """
    doc_num = meta.get("doc_number", "")
    if doc_num:
        # Clean brackets: "[2026-04321]" → "2026-04321"
        doc_num = doc_num.strip("[]")
        return f"FR Doc. {doc_num}"
    return "FR Doc."


# ---------------------------------------------------------------------------
# Preamble parser
# ---------------------------------------------------------------------------

# Preamble fields we extract as individual leaf nodes
_PREAMBLE_FIELDS = [
    ("SUM", "Summary"),
    ("DATES", "Dates"),
    ("ADD", "Addresses"),
    ("FURINF", "For Further Information Contact"),
    ("ACT", "Action"),
]


def _parse_preamble(db: Session, root, doc_node_id: int, doc_citation: str) -> Optional[int]:
    """Parse the <PREAMB> section into nodes.

    Creates a Preamble parent node (level 1) with leaf children for each
    metadata field (Summary, Dates, etc.).

    Returns the preamble node ID, or None if no preamble found.
    """
    preamb = root.find("PREAMB")
    if preamb is None:
        return None

    preamble_node = Node(
        source="federal_register",
        parent_id=doc_node_id,
        level=1,
        title="Preamble",
        summary="Document preamble containing summary, dates, agency information, and contact details.",
        citation=f"{doc_citation}, Preamble",
        metadata_={"section": "PREAMB"},
    )
    db.add(preamble_node)
    db.flush()

    for tag, field_name in _PREAMBLE_FIELDS:
        el = preamb.find(tag)
        if el is None:
            continue

        text = _collect_paragraphs(el)
        if not text:
            text = _get_all_text(el)
        if not text:
            continue

        field_node = Node(
            source="federal_register",
            parent_id=preamble_node.id,
            level=2,
            title=field_name,
            full_text=text,
            citation=f"{doc_citation}, Preamble, {field_name}",
            metadata_={"section": "PREAMB", "field": tag},
        )
        db.add(field_node)
        db.flush()
        _store_cross_references(db, field_node.id, text)

    print(f"  Preamble: parsed")
    return preamble_node.id


# ---------------------------------------------------------------------------
# Supplementary Information parser (HD heading hierarchy)
# ---------------------------------------------------------------------------

# Map HD SOURCE attribute values to relative depth within SUPLINF
_HD_LEVEL_MAP = {
    "HED": 0,   # Section header ("SUPPLEMENTARY INFORMATION")
    "HD1": 1,   # Roman numeral sections (I., II., III.)
    "HD2": 2,   # Letter subsections (A., B., C.)
    "HD3": 3,   # Sub-subsections (i., ii., 1., 2.)
    "HD4": 4,   # Rare but possible
}


def _parse_suplinf(db: Session, root, doc_node_id: int, doc_citation: str) -> Optional[int]:
    """Parse the <SUPLINF> section into a heading-based tree.

    Federal Register SUPLINF uses flat sibling <HD> elements with SOURCE
    attributes (HD1, HD2, HD3) to indicate nesting. We use a stack-based
    approach to reconstruct the tree:

    <HD SOURCE="HD1">I. Background</HD>        → level 2
    <P>Some text...</P>                         → content for "I. Background"
    <HD SOURCE="HD2">A. General</HD>            → level 3 (child of I.)
    <P>Comment text...</P>                      → content for "A. General"
    <HD SOURCE="HD1">II. Discussion</HD>        → level 2 (closes I.)

    Returns the SUPLINF node ID, or None if not found.
    """
    suplinf = root.find("SUPLINF")
    if suplinf is None:
        return None

    suplinf_node = Node(
        source="federal_register",
        parent_id=doc_node_id,
        level=1,
        title="Supplementary Information",
        summary="Discussion of background, public comments, regulatory analysis, and statutory authority.",
        citation=f"{doc_citation}, Supplementary Information",
        metadata_={"section": "SUPLINF"},
    )
    db.add(suplinf_node)
    db.flush()

    # Stack tracks (node_id, hd_level) — the currently open heading at each depth
    # Initialize with the SUPLINF node itself at level 0
    stack: list[tuple[int, int]] = [(suplinf_node.id, 0)]

    # Accumulate paragraphs for the current heading
    current_paragraphs: list[str] = []
    current_node_id: int = suplinf_node.id
    heading_count = 0

    for child in suplinf:
        if child.tag == "HD":
            # Flush accumulated paragraphs to the current node
            _flush_paragraphs(db, current_node_id, current_paragraphs)
            current_paragraphs = []

            source = child.attrib.get("SOURCE", "HD1")
            hd_level = _HD_LEVEL_MAP.get(source, 1)

            # Skip the HED-level header (it's just "SUPPLEMENTARY INFORMATION")
            if hd_level == 0:
                continue

            heading_text = _get_all_text(child)
            if not heading_text:
                continue

            # Pop stack back to find the correct parent
            while len(stack) > 1 and stack[-1][1] >= hd_level:
                stack.pop()

            parent_id = stack[-1][0]
            tree_level = 1 + hd_level  # SUPLINF is level 1, HD1 is level 2, etc.

            # Build citation path from heading text
            heading_citation = f"{doc_citation}, {heading_text}"

            heading_node = Node(
                source="federal_register",
                parent_id=parent_id,
                level=tree_level,
                title=heading_text,
                citation=heading_citation,
                metadata_={"section": "SUPLINF", "hd_source": source},
            )
            db.add(heading_node)
            db.flush()

            stack.append((heading_node.id, hd_level))
            current_node_id = heading_node.id
            heading_count += 1

        elif child.tag == "P":
            p_text = _get_all_text(child)
            if p_text:
                current_paragraphs.append(p_text)

        elif child.tag == "EXTRACT":
            # Quoted/extracted text blocks
            extract_text = _collect_paragraphs(child)
            if not extract_text:
                extract_text = _get_all_text(child)
            if extract_text:
                current_paragraphs.append(extract_text)

        elif child.tag == "FTNT":
            # Footnotes — append as text
            fn_text = _get_all_text(child)
            if fn_text:
                current_paragraphs.append(f"[Footnote: {fn_text}]")

        elif child.tag in ("GPH", "GID"):
            # Graphics/images — note their presence
            current_paragraphs.append("[Graphic/Table omitted]")

    # Flush any remaining paragraphs
    _flush_paragraphs(db, current_node_id, current_paragraphs)

    # Now walk back through and set full_text on leaf nodes, summary on intermediates
    _finalize_suplinf_nodes(db, suplinf_node.id)

    print(f"  Supplementary Information: {heading_count} headings parsed")
    return suplinf_node.id


def _flush_paragraphs(db: Session, node_id: int, paragraphs: list[str]):
    """Store accumulated paragraph text on a node."""
    if not paragraphs:
        return

    text = "\n\n".join(paragraphs)
    node = db.query(Node).filter(Node.id == node_id).first()
    if node:
        # Append to existing text if any
        if node.full_text:
            node.full_text = node.full_text + "\n\n" + text
        else:
            node.full_text = text
        _store_cross_references(db, node_id, text)
    db.flush()


def _finalize_suplinf_nodes(db: Session, suplinf_node_id: int):
    """Walk the SUPLINF subtree and set full_text only on leaves, clear it from intermediates.

    Leaf = has full_text and no children.
    Intermediate = has children → move full_text to a child node if it also has content,
                   or keep summary placeholder for now (LLM summarization in Phase 2b).
    """
    # Get all nodes in this subtree
    all_nodes = db.query(Node).filter(
        Node.source == "federal_register",
    ).all()

    # Build parent→children mapping
    children_map: dict[int, list[int]] = {}
    node_map: dict[int, Node] = {}
    for n in all_nodes:
        node_map[n.id] = n
        if n.parent_id is not None:
            children_map.setdefault(n.parent_id, []).append(n.id)

    # Walk the SUPLINF subtree
    def process(node_id: int):
        node = node_map.get(node_id)
        if node is None:
            return

        child_ids = children_map.get(node_id, [])

        if child_ids:
            # Intermediate node — has children
            if node.full_text:
                # This node has both direct text AND children.
                # Create an "Introduction" child to hold the direct text.
                intro_node = Node(
                    source="federal_register",
                    parent_id=node_id,
                    level=node.level + 1,
                    title=f"{node.title} — Introduction",
                    full_text=node.full_text,
                    citation=f"{node.citation}, Introduction" if node.citation else None,
                    metadata_={"section": "SUPLINF", "intro": True},
                )
                db.add(intro_node)
                db.flush()
                _store_cross_references(db, intro_node.id, node.full_text)
                node.full_text = None

            # Set placeholder summary (to be replaced by LLM in Phase 2b)
            if not node.summary:
                child_titles = [node_map[cid].title for cid in child_ids if cid in node_map]
                node.summary = f"Contains sections: {', '.join(child_titles[:5])}"
                if len(child_titles) > 5:
                    node.summary += f" and {len(child_titles) - 5} more"

            # Recurse into children
            for cid in child_ids:
                process(cid)
        else:
            # Leaf node — keep full_text as is
            if not node.full_text:
                node.full_text = "(No content)"

    process(suplinf_node_id)
    db.flush()


# ---------------------------------------------------------------------------
# Regulatory Text parser
# ---------------------------------------------------------------------------

def _parse_regtext(db: Session, root, doc_node_id: int, doc_citation: str, doc_meta: dict) -> list[int]:
    """Parse <REGTEXT> sections into nodes with proper CFR citations.

    A document may have multiple REGTEXT elements (one per CFR part amended).
    Each REGTEXT has attributes PART and TITLE indicating which CFR part it amends.

    Structure within REGTEXT:
      <AMDPAR> — amendment instructions (e.g., "Amend § 214.2 by revising...")
      <PART> — part header with authority citation
      <SECTION> — actual regulatory text sections
        <SECTNO> — section number (e.g., "§ 214.2")
        <SUBJECT> — section subject line
        <P> — paragraph text, often with subsection markers like (h)(19)(ii)

    Returns list of REGTEXT node IDs.
    """
    regtext_elements = root.findall("REGTEXT")
    if not regtext_elements:
        # Some FR documents use REGTEXT inside other containers
        regtext_elements = root.findall(".//REGTEXT")

    if not regtext_elements:
        return []

    regtext_ids = []

    for regtext_el in regtext_elements:
        cfr_title = regtext_el.attrib.get("TITLE", doc_meta.get("cfr_title", ""))
        cfr_part = regtext_el.attrib.get("PART", "")

        part_label = f"{cfr_title} CFR Part {cfr_part}" if cfr_title and cfr_part else "Regulatory Text"

        regtext_node = Node(
            source="federal_register",
            parent_id=doc_node_id,
            level=1,
            title=f"Regulatory Text: {part_label}",
            summary=f"Amendments to {part_label}.",
            citation=f"{cfr_title} CFR Part {cfr_part}" if cfr_title and cfr_part else doc_citation,
            metadata_={"section": "REGTEXT", "cfr_title": cfr_title, "cfr_part": cfr_part},
        )
        db.add(regtext_node)
        db.flush()
        regtext_ids.append(regtext_node.id)

        # Parse amendment paragraphs
        amdpar_texts = []
        for amdpar in regtext_el.findall("AMDPAR"):
            amdpar_text = _get_all_text(amdpar)
            if amdpar_text:
                amdpar_texts.append(amdpar_text)

        if amdpar_texts:
            amd_node = Node(
                source="federal_register",
                parent_id=regtext_node.id,
                level=2,
                title="Amendment Instructions",
                full_text="\n\n".join(amdpar_texts),
                citation=f"{cfr_title} CFR Part {cfr_part}, Amendments" if cfr_title else None,
                metadata_={"section": "REGTEXT", "subsection": "AMDPAR"},
            )
            db.add(amd_node)
            db.flush()

        # Parse PART headers (authority citations, etc.)
        for part_el in regtext_el.findall("PART"):
            _parse_regtext_part(db, part_el, regtext_node.id, cfr_title, cfr_part)

        # Parse SECTION elements — the core regulatory text
        for section_el in regtext_el.findall("SECTION"):
            _parse_regtext_section(db, section_el, regtext_node.id, cfr_title, cfr_part)

        # Also look for sections nested inside SUBPART
        for subpart_el in regtext_el.findall("SUBPART"):
            subpart_hd = subpart_el.find("HD")
            subpart_title = _get_all_text(subpart_hd) if subpart_hd is not None else "Subpart"

            subpart_node = Node(
                source="federal_register",
                parent_id=regtext_node.id,
                level=2,
                title=subpart_title,
                summary=f"Regulatory subpart: {subpart_title}",
                citation=f"{cfr_title} CFR Part {cfr_part}, {subpart_title}",
                metadata_={"section": "REGTEXT", "subsection": "SUBPART"},
            )
            db.add(subpart_node)
            db.flush()

            for section_el in subpart_el.findall("SECTION"):
                _parse_regtext_section(db, section_el, subpart_node.id, cfr_title, cfr_part)

    print(f"  Regulatory Text: {len(regtext_ids)} REGTEXT block(s) parsed")
    return regtext_ids


def _parse_regtext_part(db: Session, part_el, parent_id: int, cfr_title: str, cfr_part: str):
    """Parse a <PART> element (usually contains part header and authority citation)."""
    hd = part_el.find("HD")
    part_title = _get_all_text(hd) if hd is not None else f"Part {cfr_part}"

    auth_el = part_el.find("AUTH")
    auth_text = None
    if auth_el is not None:
        auth_text = _collect_paragraphs(auth_el)
        if not auth_text:
            auth_text = _get_all_text(auth_el)

    if auth_text:
        auth_node = Node(
            source="federal_register",
            parent_id=parent_id,
            level=2,
            title=f"{part_title} — Authority",
            full_text=auth_text,
            citation=f"{cfr_title} CFR Part {cfr_part}, Authority",
            metadata_={"section": "REGTEXT", "subsection": "AUTH"},
        )
        db.add(auth_node)
        db.flush()


def _parse_regtext_section(db: Session, section_el, parent_id: int, cfr_title: str, cfr_part: str):
    """Parse a <SECTION> element into nodes.

    Extracts section number from <SECTNO>, subject from <SUBJECT>,
    and builds subsection tree from paragraph markers like (h)(19)(ii)(B).
    """
    sectno_el = section_el.find("SECTNO")
    subject_el = section_el.find("SUBJECT")

    sect_number = _get_all_text(sectno_el).strip() if sectno_el is not None else ""
    sect_subject = _get_all_text(subject_el).strip() if subject_el is not None else ""

    # Clean section number: "§ 214.2" → "214.2"
    sect_num_clean = re.sub(r'^§\s*', '', sect_number)

    section_title = f"{sect_number} {sect_subject}".strip() or "Section"
    section_citation = f"{cfr_title} CFR §{sect_num_clean}" if sect_num_clean else None

    section_node = Node(
        source="federal_register",
        parent_id=parent_id,
        level=2,
        title=section_title,
        citation=section_citation,
        metadata_={"section": "REGTEXT", "sectno": sect_number, "subject": sect_subject},
    )
    db.add(section_node)
    db.flush()

    # Collect all paragraphs within the section and parse subsection structure
    _parse_section_paragraphs(db, section_el, section_node.id, cfr_title, sect_num_clean)


# Pattern to detect paragraph-level subsection markers: (h), (19), (ii), (B), etc.
_PARA_MARKER_RE = re.compile(
    r'^\(([a-z]+|[0-9]+|[A-Z]+|[ivxlc]+)\)\s*'
)

# Pattern to detect stars (omitted text markers)
_STARS_TAGS = {"STARS"}


def _parse_section_paragraphs(
    db: Session, section_el, section_node_id: int, cfr_title: str, sect_num: str
):
    """Parse paragraphs within a SECTION, building subsection nodes from markers.

    Federal Register regulatory text uses flat <P> elements with leading markers
    to indicate nested subsections:
        <P>(h) <E T="03">Temporary workers—</E></P>
        <P>(19) <E T="03">H-1B registration.</E></P>
        <P>(ii) <E T="03">Selection process.</E></P>
        <P>(A) <E T="03">General.</E> If USCIS determines...</P>

    We detect these markers and build a tree. Paragraphs without markers
    are treated as continuation text for the current subsection.
    """
    paragraphs: list[tuple[str, str]] = []  # (marker_or_empty, full_text)

    for child in section_el:
        if child.tag == "P":
            p_text = _get_all_text(child)
            if not p_text:
                continue

            match = _PARA_MARKER_RE.match(p_text)
            if match:
                marker = match.group(1)
                content = p_text[match.end():].strip()
                paragraphs.append((marker, content))
            else:
                paragraphs.append(("", p_text))

        elif child.tag in _STARS_TAGS:
            paragraphs.append(("", "* * *"))

    if not paragraphs:
        return

    # If there are no markers at all, store everything as section full_text
    has_markers = any(m for m, _ in paragraphs)
    if not has_markers:
        all_text = "\n\n".join(t for _, t in paragraphs)
        node = db.query(Node).filter(Node.id == section_node_id).first()
        if node:
            node.full_text = all_text
        db.flush()
        _store_cross_references(db, section_node_id, all_text)
        return

    # Build subsection tree from markers using a stack approach
    # Stack: list of (marker, node_id, marker_type)
    # marker_type helps determine nesting: lowercase letter, number, roman, uppercase letter
    stack: list[tuple[str, int, str]] = []
    accumulated_text: list[str] = []
    current_node_id = section_node_id

    for marker, content in paragraphs:
        if not marker:
            # Continuation text or stars — append to current
            accumulated_text.append(content)
            continue

        # Flush accumulated text
        if accumulated_text:
            _append_text_to_node(db, current_node_id, "\n\n".join(accumulated_text))
            accumulated_text = []

        marker_type = _classify_marker(marker)

        # Determine where this marker fits in the hierarchy
        # Pop stack until we find the right parent level
        while stack and _should_pop_stack(stack[-1][2], marker_type, stack[-1][0], marker):
            stack.pop()

        parent_id = stack[-1][1] if stack else section_node_id

        # Build citation: §214.2(h)(19)(ii)(B)
        citation_parts = [f"({s[0]})" for s in stack]
        citation_parts.append(f"({marker})")
        subsection_citation = f"{cfr_title} CFR §{sect_num}{''.join(citation_parts)}" if sect_num else None

        # Extract title from emphasized text if present
        title = f"({marker})"
        if content:
            # Try to get a short title from the beginning of content
            title_match = re.match(r'^([^.—–\-]{1,80})[.—–\-]', content)
            if title_match:
                title = f"({marker}) {title_match.group(1).strip()}"
            else:
                title = f"({marker}) {content[:80]}{'...' if len(content) > 80 else ''}"

        sub_node = Node(
            source="federal_register",
            parent_id=parent_id,
            level=3 + len(stack),
            title=title,
            full_text=content if content else None,
            citation=subsection_citation,
            metadata_={"section": "REGTEXT", "marker": marker, "marker_type": marker_type},
        )
        db.add(sub_node)
        db.flush()

        if content:
            _store_cross_references(db, sub_node.id, content)

        stack.append((marker, sub_node.id, marker_type))
        current_node_id = sub_node.id

    # Flush any remaining text
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


def _classify_marker(marker: str) -> str:
    """Classify a paragraph marker to determine its nesting level.

    CFR paragraph designation hierarchy (8 CFR convention):
      Level 1: lowercase letters (a), (b), (c)
      Level 2: numbers (1), (2), (3)
      Level 3: roman numerals (i), (ii), (iii)
      Level 4: uppercase letters (A), (B), (C)
      Level 5: italic numbers — rare, treated as numbers
    """
    if re.match(r'^[ivxlc]+$', marker) and not re.match(r'^[a-z]$', marker):
        return "roman"
    if re.match(r'^[a-z]+$', marker):
        return "lower"
    if re.match(r'^[0-9]+$', marker):
        return "number"
    if re.match(r'^[A-Z]+$', marker):
        return "upper"
    return "other"


# Nesting order for CFR paragraphs
_MARKER_DEPTH = {"lower": 0, "number": 1, "roman": 2, "upper": 3, "other": 4}


def _should_pop_stack(stack_type: str, new_type: str, stack_marker: str, new_marker: str) -> bool:
    """Determine if we should pop the stack when encountering a new marker.

    Pop when the new marker is at the same or higher level in the hierarchy.
    """
    stack_depth = _MARKER_DEPTH.get(stack_type, 4)
    new_depth = _MARKER_DEPTH.get(new_type, 4)
    return new_depth <= stack_depth


def _append_text_to_node(db: Session, node_id: int, text: str):
    """Append text to a node's full_text field."""
    if not text:
        return
    node = db.query(Node).filter(Node.id == node_id).first()
    if node:
        if node.full_text:
            node.full_text = node.full_text + "\n\n" + text
        else:
            node.full_text = text
        _store_cross_references(db, node_id, text)
    db.flush()


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def clear_legal_tables(db: Session):
    """Clear existing legal tree data to make ingestion idempotent."""
    db.query(NodeCrossReference).delete()
    db.query(Node).delete()
    db.commit()


def parse_federal_register_xml(db: Session, file_path: Path):
    """Parse a Federal Register XML file into the Node tree.

    Creates:
      Level 0: Document root node
      Level 1: Preamble, Supplementary Information, Regulatory Text
      Level 2+: Headings, sections, subsections (varies by section type)
    """
    print(f"\nIngesting Federal Register XML: {file_path.name}")

    try:
        tree = ET.parse(file_path)
        root_element = tree.getroot()
    except ET.ParseError as e:
        print(f"  ERROR: Failed to parse XML: {e}")
        return

    # Extract document metadata
    doc_meta = _extract_document_metadata(root_element)
    doc_citation = _make_doc_citation_prefix(doc_meta)

    # Determine document title
    doc_title = doc_meta.get("subject", root_element.tag)
    doc_type = doc_meta.get("doc_type", root_element.tag)

    print(f"  Document type: {doc_type}")
    print(f"  Subject: {doc_title[:100]}")
    if "cfr_title" in doc_meta:
        print(f"  CFR: Title {doc_meta['cfr_title']}, {doc_meta.get('cfr_parts', '')}")

    # Create root node
    root_node = Node(
        source="federal_register",
        parent_id=None,
        level=0,
        title=doc_title,
        summary=f"Federal Register {doc_type}: {doc_title[:200]}",
        citation=doc_citation,
        metadata_=doc_meta,
    )
    db.add(root_node)
    db.flush()

    # Parse each major section
    _parse_preamble(db, root_element, root_node.id, doc_citation)
    _parse_suplinf(db, root_element, root_node.id, doc_citation)
    _parse_regtext(db, root_element, root_node.id, doc_citation, doc_meta)

    db.commit()

    # Print summary stats
    total_nodes = db.query(Node).filter(Node.metadata_.contains({"doc_type": doc_type})).count()
    leaf_nodes = db.query(Node).filter(Node.full_text.isnot(None)).count()
    xref_count = db.query(NodeCrossReference).count()
    print(f"  Total nodes: {total_nodes}, Leaf nodes: {leaf_nodes}, Cross-references: {xref_count}")


def main(data_dir: str):
    base_path = Path(data_dir)
    if not base_path.exists():
        print(f"Data directory {data_dir} not found.")
        return

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        print("Clearing existing legal tables...")
        clear_legal_tables(db)

        xml_files = sorted(base_path.glob("*.xml"))
        if not xml_files:
            print(f"No XML files found in {base_path}")
            return

        print(f"Found {len(xml_files)} XML file(s) to ingest.")

        for xml_file in xml_files:
            parse_federal_register_xml(db, xml_file)

        # Final stats
        total = db.query(Node).count()
        leaves = db.query(Node).filter(Node.full_text.isnot(None)).count()
        xrefs = db.query(NodeCrossReference).count()
        print(f"\n=== Ingestion Complete ===")
        print(f"Total nodes: {total}")
        print(f"Leaf nodes (with full_text): {leaves}")
        print(f"Cross-references (pending resolution): {xrefs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Federal Register XML into legal node tree")
    parser.add_argument("--data-dir", type=str, default="./data/legal", help="Path to folder with FR XML files")
    args = parser.parse_args()
    main(args.data_dir)
