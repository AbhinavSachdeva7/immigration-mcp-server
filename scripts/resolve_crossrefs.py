"""Cross-Reference Resolution Script (Phase 2c)

After legal ingestion, NodeCrossReference rows exist with target_node_id=None.
This script matches reference_text patterns against Node.citation fields to
resolve the links.

Matching strategy (tried in order):
  1. Exact CFR citation match: "§ 214.2(h)(19)" → node with citation "8 CFR §214.2(h)(19)"
  2. Normalized match: strip whitespace, normalize § symbols, case-insensitive
  3. Partial/parent match: "§ 214.2(h)" matches the closest ancestor node
  4. Section-number match: bare "214.2" matches any node whose citation contains "§214.2"
  5. INA reference match: "INA 214(g)" → node with citation "INA §214(g)"
  6. USCIS PM reference match: "Volume 2, Part A, Chapter 3" → "USCIS-PM Vol. 2, Pt. A, Ch. 3"

Source priority when multiple nodes share a citation:
  ecfr > federal_register > uscis_manual (prefer consolidated regulatory text)

Run after all ingestion scripts:
    python -m scripts.resolve_crossrefs
"""

import argparse
import re

from sqlalchemy.orm import Session

from src.db.database import Base, SessionLocal, engine
from src.db.models import Node, NodeCrossReference


# ---------------------------------------------------------------------------
# Citation normalization
# ---------------------------------------------------------------------------

def _normalize_citation(text: str) -> str:
    """Normalize a citation string for fuzzy matching.

    - Lowercase
    - Strip surrounding whitespace
    - Normalize § variations (section, sec., §) to "§"
    - Collapse whitespace around §
    - Remove "see", "under", "per" prefixes
    """
    s = text.strip().lower()
    # Remove common prefixes
    s = re.sub(r'^(see|under|per|cf\.?)\s+', '', s)
    # Normalize section symbols
    s = re.sub(r'\bsection\b', '§', s)
    s = re.sub(r'\bsec\.?\b', '§', s)
    # Collapse whitespace around §
    s = re.sub(r'\s*§\s*', '§', s)
    # Collapse all whitespace
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def _extract_cfr_components(text: str) -> tuple[str, str, str]:
    """Extract (title, section_number, subsections) from a citation string.

    Examples:
        "8 CFR §214.2(h)(19)" → ("8", "214.2", "(h)(19)")
        "§ 214.2(h)"          → ("",  "214.2", "(h)")
        "214.2"               → ("",  "214.2", "")
    """
    normalized = _normalize_citation(text)

    # Pattern: optional title + CFR + § + section.number + optional subsections
    m = re.search(
        r'(?:(\d+)\s*cfr\s*)?§?(\d+\.\d+)((?:\([a-zA-Z0-9]+\))*)',
        normalized,
    )
    if m:
        return (m.group(1) or "", m.group(2), m.group(3) or "")

    return ("", "", "")


def _extract_paragraph_ref(text: str) -> str:
    """Extract paragraph markers from a "paragraph (x)(y)" reference.

    "paragraph (h)(19)(ii)" → "(h)(19)(ii)"
    """
    normalized = _normalize_citation(text)
    m = re.search(r'paragraph\s*((?:\([a-zA-Z0-9]+\))+)', normalized)
    if m:
        return m.group(1)
    return ""


def _extract_ina_components(text: str) -> tuple[str, str]:
    """Extract (section_number, subsections) from an INA reference.

    Examples:
        "INA § 214(g)(3)"              → ("214", "(g)(3)")
        "INA 101(a)(15)(H)(i)(b)"      → ("101", "(a)(15)(H)(i)(b)")
        "section 214(i) of the Act"    → ("214", "(i)")
    """
    s = text.strip()
    # "INA § 214(g)(3)" or "INA section 214(g)"
    m = re.search(
        r'(?:INA|Act)\s*(?:§\s*|section\s+)?(\d+)((?:\([a-zA-Z0-9]+\))*)',
        s, re.IGNORECASE,
    )
    if m:
        return m.group(1), m.group(2) or ""

    # "section 214(g) of the Act"
    m = re.search(
        r'section\s+(\d+)((?:\([a-zA-Z0-9]+\))*)\s+of\s+the\s+Act',
        s, re.IGNORECASE,
    )
    if m:
        return m.group(1), m.group(2) or ""

    return "", ""


def _normalize_uscis_pm_ref(text: str) -> str:
    """Normalize a USCIS Policy Manual cross-reference to match citation format.

    "Volume 2, Part A, Chapter 3"       → "uscis-pm vol. 2, pt. a, ch. 3"
    "See Volume 6, Part E, Chapter 2"   → "uscis-pm vol. 6, pt. e, ch. 2"
    """
    s = text.strip().lower()
    s = re.sub(r'^(see|see also)\s+', '', s)

    m = re.search(
        r'volume\s+(\d+),?\s*part\s+([a-z]),?\s*chapter\s+(\d+)',
        s, re.IGNORECASE,
    )
    if m:
        return f"uscis-pm vol. {m.group(1)}, pt. {m.group(2).lower()}, ch. {m.group(3)}"
    return ""


# ---------------------------------------------------------------------------
# Citation index builder
# ---------------------------------------------------------------------------

# Source priority: prefer consolidated text (ecfr) over amendment fragments
_SOURCE_PRIORITY = {"ecfr": 0, "ina": 1, "federal_register": 2, "uscis_manual": 3}


def _sort_by_source_priority(
    matches: list[tuple[int, str]],
    source_map: dict[int, str],
) -> list[tuple[int, str]]:
    """Sort matches so preferred sources (ecfr) come first."""
    return sorted(
        matches,
        key=lambda m: _SOURCE_PRIORITY.get(source_map.get(m[0], ""), 9),
    )


def _build_citation_index(
    db: Session,
) -> tuple[dict[str, list[tuple[int, str]]], dict[int, str]]:
    """Build an index of normalized citations → [(node_id, original_citation)].

    Returns (index, source_map) where:
      - index: dict of normalized citation → list of (node_id, original_citation)
      - source_map: dict of node_id → source string (for priority sorting)
    """
    nodes = db.query(Node.id, Node.citation, Node.source).filter(
        Node.citation.isnot(None)
    ).all()

    index: dict[str, list[tuple[int, str]]] = {}
    source_map: dict[int, str] = {}

    for node_id, citation, source in nodes:
        source_map[node_id] = source
        normalized = _normalize_citation(citation)
        index.setdefault(normalized, []).append((node_id, citation))

        # Index CFR components: "8 cfr §214.2(h)(19)"
        title, sect, subs = _extract_cfr_components(citation)
        if sect:
            key = f"§{sect}{subs}"
            index.setdefault(key, []).append((node_id, citation))
            if title:
                key_with_title = f"{title} cfr §{sect}{subs}"
                index.setdefault(key_with_title, []).append((node_id, citation))

        # Index INA citations: "INA §214(g)(3)"
        ina_sect, ina_subs = _extract_ina_components(citation)
        if ina_sect:
            key = f"ina §{ina_sect}{ina_subs}"
            index.setdefault(key, []).append((node_id, citation))
            # Also without subsections for partial matching
            if ina_subs:
                key_bare = f"ina §{ina_sect}"
                index.setdefault(key_bare, []).append((node_id, citation))

        # Index USCIS PM citations: "uscis-pm vol. 2, pt. a, ch. 3"
        if "uscis-pm" in normalized or "uscis" in normalized:
            index.setdefault(normalized, []).append((node_id, citation))

    # Sort all match lists by source priority
    for key in index:
        index[key] = _sort_by_source_priority(index[key], source_map)

    return index, source_map


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _find_best_match(
    ref_text: str,
    citation_index: dict[str, list[tuple[int, str]]],
    all_citations: list[tuple[int, str]],
) -> int | None:
    """Try multiple strategies to match a reference to a node.

    Returns the best-matching node_id, or None if no match found.
    """
    normalized_ref = _normalize_citation(ref_text)

    # Strategy 1: Direct normalized match
    matches = citation_index.get(normalized_ref)
    if matches:
        return matches[0][0]

    # Strategy 2: Extract CFR components and match
    title, sect, subs = _extract_cfr_components(ref_text)
    if sect:
        # Try full match with subsections
        key = f"§{sect}{subs}"
        matches = citation_index.get(key)
        if matches:
            return matches[0][0]

        # Try with title prefix
        if title:
            key_with_title = f"{title} cfr §{sect}{subs}"
            matches = citation_index.get(key_with_title)
            if matches:
                return matches[0][0]

        # Strategy 3: Partial/parent match — strip subsection levels from the right
        # until we find a match (e.g., §214.2(h)(19)(ii) → §214.2(h)(19) → §214.2(h) → §214.2)
        if subs:
            current_subs = subs
            while current_subs:
                # Remove last parenthetical
                current_subs = re.sub(r'\([^)]*\)$', '', current_subs)
                key = f"§{sect}{current_subs}"
                matches = citation_index.get(key)
                if matches:
                    # Return the most specific (deepest) match
                    return matches[0][0]

        # Strategy 4: Just the section number
        key = f"§{sect}"
        matches = citation_index.get(key)
        if matches:
            return matches[0][0]

    # Strategy 5: INA reference match
    ina_sect, ina_subs = _extract_ina_components(ref_text)
    if ina_sect:
        key = f"ina §{ina_sect}{ina_subs}"
        matches = citation_index.get(key)
        if matches:
            return matches[0][0]
        # Try without subsections (partial match)
        if ina_subs:
            current_subs = ina_subs
            while current_subs:
                current_subs = re.sub(r'\([^)]*\)$', '', current_subs)
                key = f"ina §{ina_sect}{current_subs}"
                matches = citation_index.get(key)
                if matches:
                    return matches[0][0]
        # Bare section number
        key = f"ina §{ina_sect}"
        matches = citation_index.get(key)
        if matches:
            return matches[0][0]

    # Strategy 6: USCIS Policy Manual cross-reference
    uscis_key = _normalize_uscis_pm_ref(ref_text)
    if uscis_key:
        matches = citation_index.get(uscis_key)
        if matches:
            return matches[0][0]

    # Strategy 7: Paragraph reference — match against subsection markers
    para_ref = _extract_paragraph_ref(ref_text)
    if para_ref:
        # Search all citations for one ending with these subsection markers
        for node_id, citation in all_citations:
            if citation and citation.endswith(para_ref):
                return node_id

    # Strategy 8: Substring search — last resort, find citations containing the reference
    if len(normalized_ref) > 5:
        for node_id, citation in all_citations:
            if citation and normalized_ref in _normalize_citation(citation):
                return node_id

    return None


# ---------------------------------------------------------------------------
# Main resolution
# ---------------------------------------------------------------------------

def resolve_cross_references(db: Session) -> tuple[int, int]:
    """Resolve all pending cross-references.

    Returns (resolved_count, total_unresolved_count).
    """
    unresolved = db.query(NodeCrossReference).filter(
        NodeCrossReference.target_node_id.is_(None)
    ).all()

    if not unresolved:
        print("No unresolved cross-references found.")
        return (0, 0)

    print(f"Found {len(unresolved)} unresolved cross-references.")

    # Build citation index
    all_citations = db.query(Node.id, Node.citation).filter(
        Node.citation.isnot(None)
    ).all()
    citation_index, source_map = _build_citation_index(db)

    print(f"Citation index: {len(citation_index)} entries from {len(all_citations)} nodes.")

    resolved = 0
    failed_refs: list[str] = []

    for xref in unresolved:
        target_id = _find_best_match(xref.reference_text, citation_index, all_citations)
        if target_id:
            # Avoid self-references
            if target_id != xref.source_node_id:
                xref.target_node_id = target_id
                resolved += 1
            else:
                failed_refs.append(f"  SELF-REF skipped: '{xref.reference_text}'")
        else:
            failed_refs.append(f"  No match: '{xref.reference_text}'")

    db.commit()

    print(f"\nResolved: {resolved}/{len(unresolved)}")
    if failed_refs:
        print(f"Unresolved ({len(failed_refs)}):")
        for ref in failed_refs[:20]:
            print(ref)
        if len(failed_refs) > 20:
            print(f"  ... and {len(failed_refs) - 20} more")

    return (resolved, len(unresolved))


def main():
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        resolve_cross_references(db)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve cross-references between legal nodes")
    parser.parse_args()
    main()
