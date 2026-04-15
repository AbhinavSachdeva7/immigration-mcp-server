"""USCIS Policy Manual Ingestion Script (Phase 2d)

Two-phase ingestion of the USCIS Policy Manual from https://www.uscis.gov/policy-manual:

  Phase 1 — Fetch: Crawl the USCIS website and cache HTML pages locally.
  Phase 2 — Ingest: Parse cached HTML into the Node tree structure.

The Policy Manual is organized as:
  Volume → Part → Chapter → Sections (from HTML headings)

Citation format: "USCIS-PM Vol. {N}, Pt. {L}, Ch. {N}, § {heading}"

Usage:
    # Fetch volumes 2, 6, 7 (immigration-relevant subset)
    python -m scripts.ingest_uscis --fetch --volumes 2 6 7

    # Ingest cached HTML into database
    python -m scripts.ingest_uscis --ingest

    # Fetch and ingest in one step
    python -m scripts.ingest_uscis --fetch --ingest --volumes 2 6 7

    # Ingest from a custom cache directory
    python -m scripts.ingest_uscis --ingest --data-dir ./data/uscis

Rate limiting: 2-second delay between requests during fetch (be respectful to USCIS servers).
"""

import argparse
import json
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag
from sqlalchemy.orm import Session

from src.db.database import Base, SessionLocal, engine
from src.db.models import Node, NodeCrossReference


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.uscis.gov"
POLICY_MANUAL_URL = f"{BASE_URL}/policy-manual"

DEFAULT_DATA_DIR = Path("./data/uscis")
DEFAULT_VOLUMES = [2, 6, 7]  # Immigration-relevant subset

# Polite crawling settings
REQUEST_DELAY = 2.0  # seconds between requests
USER_AGENT = (
    "ImmigrationMCPServer/0.1 (educational research tool; "
    "respects robots.txt; contact: github.com/immigration-mcp-server)"
)

# HTML selectors — these target the USCIS Policy Manual page structure.
# If the USCIS website redesigns, these selectors may need updating.
# The script logs warnings when expected elements are not found.
CONTENT_SELECTORS = [
    "div.field--name-body",           # Drupal body field (primary)
    "article .content",               # Article content wrapper
    "div.node__content",              # Node content (Drupal)
    ".policy-manual-chapter-content", # PM-specific class
    "main .content",                  # Generic main content
    "main",                           # Fallback to main element
]

# Heading tags that define section structure within a chapter
SECTION_HEADING_TAGS = {"h2", "h3", "h4", "h5"}


# ---------------------------------------------------------------------------
# Phase 1: Fetcher
# ---------------------------------------------------------------------------

class PolicyManualFetcher:
    """Crawls the USCIS Policy Manual website and caches HTML pages locally."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )
        self._request_count = 0

    def close(self):
        self.client.close()

    def _fetch_page(self, url: str) -> str | None:
        """Fetch a page with rate limiting and error handling."""
        # Rate limit
        if self._request_count > 0:
            time.sleep(REQUEST_DELAY)
        self._request_count += 1

        try:
            response = self.client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            print(f"  HTTP {e.response.status_code} fetching {url}")
            return None
        except httpx.RequestError as e:
            print(f"  Request error fetching {url}: {e}")
            return None

    def _save_html(self, path: Path, html: str, metadata: dict):
        """Save HTML content and metadata to local cache."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        # Save metadata alongside
        meta_path = path.with_suffix(".json")
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def _extract_links(self, html: str, pattern: str) -> list[tuple[str, str]]:
        """Extract (url, title) pairs from links matching a URL pattern."""
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Normalize relative URLs
            if href.startswith("/"):
                href = BASE_URL + href
            if pattern in href and href not in [l[0] for l in links]:
                title = a.get_text(strip=True)
                links.append((href, title))
        return links

    def fetch_volume_index(self, volume_num: int) -> list[dict]:
        """Fetch the volume page and extract part links.

        Returns list of {"url": ..., "title": ..., "part_letter": ...}
        """
        url = f"{POLICY_MANUAL_URL}/volume-{volume_num}"
        print(f"\nFetching Volume {volume_num} index: {url}")

        html = self._fetch_page(url)
        if not html:
            return []

        # Save the volume index page
        vol_dir = self.data_dir / f"volume-{volume_num}"
        self._save_html(
            vol_dir / "index.html", html,
            {"type": "volume_index", "volume": volume_num, "url": url},
        )

        # Extract part links: /policy-manual/volume-{N}-part-{L}
        pattern = f"/policy-manual/volume-{volume_num}-part-"
        part_links = self._extract_links(html, pattern)

        # Also try to extract volume title from the page
        soup = BeautifulSoup(html, "html.parser")
        vol_title_el = soup.find("h1") or soup.find("title")
        vol_title = vol_title_el.get_text(strip=True) if vol_title_el else f"Volume {volume_num}"

        # Save volume metadata
        (vol_dir / "volume_meta.json").write_text(json.dumps({
            "volume": volume_num,
            "title": vol_title,
            "url": url,
            "parts_found": len(part_links),
        }, indent=2), encoding="utf-8")

        parts = []
        for part_url, part_title in part_links:
            # Extract part letter from URL: volume-2-part-a → "a"
            m = re.search(r'part-([a-zA-Z]+)', part_url)
            part_letter = m.group(1).upper() if m else "?"
            parts.append({
                "url": part_url,
                "title": part_title,
                "part_letter": part_letter,
            })

        print(f"  Found {len(parts)} parts")
        return parts

    def fetch_part_chapters(self, volume_num: int, part_info: dict) -> list[dict]:
        """Fetch a part page and extract chapter links.

        Returns list of {"url": ..., "title": ..., "chapter_num": ...}
        """
        url = part_info["url"]
        part_letter = part_info["part_letter"]
        print(f"  Fetching Part {part_letter}: {url}")

        html = self._fetch_page(url)
        if not html:
            return []

        # Save the part index page
        vol_dir = self.data_dir / f"volume-{volume_num}"
        self._save_html(
            vol_dir / f"part-{part_letter.lower()}" / "index.html", html,
            {"type": "part_index", "volume": volume_num, "part": part_letter, "url": url},
        )

        # Extract chapter links: /policy-manual/volume-{N}-part-{L}-chapter-{N}
        pattern = f"volume-{volume_num}-part-{part_letter.lower()}-chapter-"
        chapter_links = self._extract_links(html, pattern)

        chapters = []
        for ch_url, ch_title in chapter_links:
            m = re.search(r'chapter-(\d+)', ch_url)
            ch_num = int(m.group(1)) if m else 0
            chapters.append({
                "url": ch_url,
                "title": ch_title,
                "chapter_num": ch_num,
            })

        print(f"    Found {len(chapters)} chapters")
        return chapters

    def fetch_chapter(self, volume_num: int, part_letter: str, chapter_info: dict) -> Path | None:
        """Fetch a chapter page and save it locally.

        Returns the path to the saved HTML file, or None on failure.
        """
        url = chapter_info["url"]
        ch_num = chapter_info["chapter_num"]

        html = self._fetch_page(url)
        if not html:
            return None

        file_path = (
            self.data_dir
            / f"volume-{volume_num}"
            / f"part-{part_letter.lower()}"
            / f"chapter-{ch_num}.html"
        )

        self._save_html(file_path, html, {
            "type": "chapter",
            "volume": volume_num,
            "part": part_letter,
            "chapter": ch_num,
            "title": chapter_info["title"],
            "url": url,
        })

        return file_path

    def fetch_volumes(self, volume_nums: list[int]) -> dict:
        """Fetch all specified volumes, parts, and chapters.

        Returns a manifest dict describing what was fetched.
        """
        manifest = {"volumes": [], "total_chapters": 0}

        for vol_num in volume_nums:
            vol_data = {"volume": vol_num, "parts": []}

            parts = self.fetch_volume_index(vol_num)
            for part_info in parts:
                part_data = {
                    "part_letter": part_info["part_letter"],
                    "title": part_info["title"],
                    "chapters": [],
                }

                chapters = self.fetch_part_chapters(vol_num, part_info)
                for ch_info in chapters:
                    file_path = self.fetch_chapter(
                        vol_num, part_info["part_letter"], ch_info
                    )
                    if file_path:
                        part_data["chapters"].append({
                            "chapter_num": ch_info["chapter_num"],
                            "title": ch_info["title"],
                            "file": str(file_path),
                        })
                        manifest["total_chapters"] += 1
                        print(f"      Ch. {ch_info['chapter_num']}: {ch_info['title'][:60]}")

                vol_data["parts"].append(part_data)

            manifest["volumes"].append(vol_data)

        # Save manifest
        manifest_path = self.data_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\nFetch complete. {manifest['total_chapters']} chapters saved.")
        print(f"Manifest: {manifest_path}")

        return manifest


# ---------------------------------------------------------------------------
# Phase 2: HTML Parser → Node tree
# ---------------------------------------------------------------------------

def _find_content_element(soup: BeautifulSoup) -> Tag | None:
    """Find the main content container in a USCIS page."""
    for selector in CONTENT_SELECTORS:
        el = soup.select_one(selector)
        if el:
            return el
    return None


def _extract_sections_from_content(content_el: Tag) -> list[dict]:
    """Parse an HTML content element into a flat list of sections.

    Walks through the content element's children. Headings (h2-h5)
    create new sections; everything between headings is collected as
    content for the preceding heading.

    Returns list of {"tag": "h2"|"h3"|..., "title": str, "content": str}
    """
    sections: list[dict] = []
    current_title = ""
    current_tag = "h1"  # synthetic top level
    current_content: list[str] = []

    for child in content_el.children:
        if not isinstance(child, Tag):
            text = child.string
            if text and text.strip():
                current_content.append(text.strip())
            continue

        if child.name in SECTION_HEADING_TAGS:
            # Flush previous section
            if current_content or current_title:
                sections.append({
                    "tag": current_tag,
                    "title": current_title,
                    "content": "\n\n".join(current_content),
                })
            current_tag = child.name
            current_title = child.get_text(strip=True)
            current_content = []
        else:
            # Collect text content
            text = child.get_text(strip=True)
            if text:
                current_content.append(text)

    # Flush last section
    if current_content or current_title:
        sections.append({
            "tag": current_tag,
            "title": current_title,
            "content": "\n\n".join(current_content),
        })

    return sections


# Map heading tags to relative depth
_HEADING_DEPTH = {"h1": 0, "h2": 1, "h3": 2, "h4": 3, "h5": 4}


def _build_section_tree(
    db: Session,
    sections: list[dict],
    chapter_node_id: int,
    base_level: int,
    citation_prefix: str,
):
    """Build a Node tree from parsed sections using a stack-based approach.

    Similar to the SUPLINF parser: headings create tree levels,
    content between headings becomes full_text on leaf nodes.
    """
    if not sections:
        return

    # Stack: (node_id, heading_depth)
    stack: list[tuple[int, int]] = [(chapter_node_id, 0)]

    for section in sections:
        tag = section["tag"]
        title = section["title"]
        content = section["content"]
        heading_depth = _HEADING_DEPTH.get(tag, 1)

        if not title and not content:
            continue

        # If this is the introductory content before any heading
        if tag == "h1" and not title:
            if content:
                intro_node = Node(
                    source="uscis_manual",
                    parent_id=chapter_node_id,
                    level=base_level + 1,
                    title="Introduction",
                    full_text=content,
                    citation=f"{citation_prefix}, Introduction",
                    metadata_={"section_type": "intro"},
                )
                db.add(intro_node)
                db.flush()
                _store_uscis_cross_references(db, intro_node.id, content)
            continue

        # Pop stack to find correct parent
        while len(stack) > 1 and stack[-1][1] >= heading_depth:
            stack.pop()

        parent_id = stack[-1][0]
        node_level = base_level + heading_depth

        section_citation = f"{citation_prefix}, {title}" if title else citation_prefix

        node = Node(
            source="uscis_manual",
            parent_id=parent_id,
            level=node_level,
            title=title or "(Untitled section)",
            full_text=content if content else None,
            citation=section_citation,
            metadata_={"heading_tag": tag},
        )
        db.add(node)
        db.flush()

        if content:
            _store_uscis_cross_references(db, node.id, content)

        stack.append((node.id, heading_depth))

    # Finalize: set summary on intermediate nodes, full_text only on leaves
    _finalize_chapter_nodes(db, chapter_node_id)


def _finalize_chapter_nodes(db: Session, chapter_node_id: int):
    """Ensure leaf/intermediate separation for a chapter's subtree.

    Intermediate nodes (with children): clear full_text, set placeholder summary.
    Leaf nodes (no children): keep full_text.
    """
    # Get all nodes in this chapter subtree
    all_nodes = _get_subtree(db, chapter_node_id)
    children_map: dict[int, list[int]] = {}
    node_map: dict[int, Node] = {}

    for node in all_nodes:
        node_map[node.id] = node
        if node.parent_id is not None:
            children_map.setdefault(node.parent_id, []).append(node.id)

    for node_id, node in node_map.items():
        child_ids = children_map.get(node_id, [])
        if child_ids:
            # Intermediate node
            if node.full_text:
                # Move direct text to an "Introduction" child
                intro = Node(
                    source="uscis_manual",
                    parent_id=node_id,
                    level=node.level + 1,
                    title=f"{node.title} — Introduction",
                    full_text=node.full_text,
                    citation=f"{node.citation}, Introduction" if node.citation else None,
                    metadata_={"section_type": "intro"},
                )
                db.add(intro)
                db.flush()
                if node.full_text:
                    _store_uscis_cross_references(db, intro.id, node.full_text)
                node.full_text = None

            if not node.summary:
                child_titles = [
                    node_map[cid].title
                    for cid in child_ids
                    if cid in node_map
                ]
                node.summary = f"Contains: {', '.join(child_titles[:5])}"
                if len(child_titles) > 5:
                    node.summary += f" and {len(child_titles) - 5} more"
        else:
            # Leaf node
            if not node.full_text:
                node.full_text = "(No content)"

    db.flush()


def _get_subtree(db: Session, root_id: int) -> list[Node]:
    """Get all nodes in a subtree (including root) via iterative BFS."""
    result = []
    queue = [root_id]
    while queue:
        current_id = queue.pop(0)
        node = db.query(Node).filter(Node.id == current_id).first()
        if node:
            result.append(node)
            children = db.query(Node).filter(Node.parent_id == current_id).all()
            queue.extend(c.id for c in children)
    return result


def _store_uscis_cross_references(db: Session, source_node_id: int, text: str):
    """Extract and store cross-references from USCIS Policy Manual text.

    Common patterns:
      - "See Volume 2, Part A, Chapter 3"
      - "See 8 CFR 214.2(h)"
      - "INA 214(g)"
      - "Section 101(a)(15)(H)(i)(b)"
    """
    patterns = [
        # Volume/Part/Chapter references within the Policy Manual
        r'(?:See|see|See also)\s+(Volume\s+\d+,\s*Part\s+[A-Z],\s*Chapter\s+\d+(?:,\s*Section\s+[A-Z])?)',
        # CFR references
        r'(\d+\s+CFR\s+(?:§\s*)?[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
        # INA references
        r'(INA\s+(?:§\s*)?[\d]+(?:\([a-zA-Z0-9]+\))*)',
        # Section references with § symbol
        r'(?:see|See|under)\s+(§\s*[\d]+\.[\d]+(?:\([a-zA-Z0-9]+\))*)',
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

def clear_uscis_nodes(db: Session):
    """Clear existing USCIS Policy Manual nodes (preserves Federal Register nodes)."""
    # Delete cross-references for USCIS nodes
    uscis_node_ids = [
        n.id for n in db.query(Node.id).filter(Node.source == "uscis_manual").all()
    ]
    if uscis_node_ids:
        db.query(NodeCrossReference).filter(
            NodeCrossReference.source_node_id.in_(uscis_node_ids)
            | NodeCrossReference.target_node_id.in_(uscis_node_ids)
        ).delete(synchronize_session=False)
    db.query(Node).filter(Node.source == "uscis_manual").delete()
    db.commit()


def ingest_from_cache(db: Session, data_dir: Path):
    """Parse cached HTML files into the Node tree.

    Reads the manifest.json to understand the volume/part/chapter structure,
    then parses each chapter HTML into nodes.
    """
    manifest_path = data_dir / "manifest.json"

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"Using manifest: {manifest['total_chapters']} chapters across {len(manifest['volumes'])} volumes")
        _ingest_from_manifest(db, data_dir, manifest)
    else:
        # No manifest — discover structure from directory layout
        print("No manifest.json found. Discovering structure from directory layout...")
        _ingest_from_directory(db, data_dir)


def _ingest_from_manifest(db: Session, data_dir: Path, manifest: dict):
    """Ingest using the manifest created during fetch."""
    for vol_data in manifest["volumes"]:
        vol_num = vol_data["volume"]

        # Read volume metadata
        vol_meta_path = data_dir / f"volume-{vol_num}" / "volume_meta.json"
        vol_title = f"Volume {vol_num}"
        if vol_meta_path.exists():
            vol_meta = json.loads(vol_meta_path.read_text(encoding="utf-8"))
            vol_title = vol_meta.get("title", vol_title)

        # Create volume root node
        vol_citation = f"USCIS-PM Vol. {vol_num}"
        vol_node = Node(
            source="uscis_manual",
            parent_id=None,
            level=0,
            title=vol_title,
            summary=f"USCIS Policy Manual Volume {vol_num}: {vol_title}",
            citation=vol_citation,
            metadata_={"volume": vol_num, "doc_type": "uscis_policy_manual"},
        )
        db.add(vol_node)
        db.flush()
        print(f"\nVolume {vol_num}: {vol_title}")

        for part_data in vol_data["parts"]:
            part_letter = part_data["part_letter"]
            part_title = part_data.get("title", f"Part {part_letter}")
            part_citation = f"{vol_citation}, Pt. {part_letter}"

            part_node = Node(
                source="uscis_manual",
                parent_id=vol_node.id,
                level=1,
                title=part_title,
                citation=part_citation,
                metadata_={"volume": vol_num, "part": part_letter},
            )
            db.add(part_node)
            db.flush()
            print(f"  Part {part_letter}: {part_title[:60]}")

            for ch_data in part_data["chapters"]:
                ch_num = ch_data["chapter_num"]
                ch_title = ch_data.get("title", f"Chapter {ch_num}")
                ch_file = Path(ch_data["file"])

                if not ch_file.exists():
                    print(f"    Ch. {ch_num}: MISSING FILE {ch_file}")
                    continue

                _ingest_chapter(
                    db, ch_file, part_node.id,
                    vol_num, part_letter, ch_num, ch_title,
                )

    db.commit()


def _ingest_from_directory(db: Session, data_dir: Path):
    """Discover and ingest from directory structure without a manifest."""
    vol_dirs = sorted(data_dir.glob("volume-*"))
    if not vol_dirs:
        print(f"No volume directories found in {data_dir}")
        return

    for vol_dir in vol_dirs:
        m = re.search(r'volume-(\d+)', vol_dir.name)
        if not m:
            continue
        vol_num = int(m.group(1))

        # Read volume metadata if available
        vol_meta_path = vol_dir / "volume_meta.json"
        vol_title = f"Volume {vol_num}"
        if vol_meta_path.exists():
            meta = json.loads(vol_meta_path.read_text(encoding="utf-8"))
            vol_title = meta.get("title", vol_title)

        vol_citation = f"USCIS-PM Vol. {vol_num}"
        vol_node = Node(
            source="uscis_manual",
            parent_id=None,
            level=0,
            title=vol_title,
            summary=f"USCIS Policy Manual {vol_title}",
            citation=vol_citation,
            metadata_={"volume": vol_num, "doc_type": "uscis_policy_manual"},
        )
        db.add(vol_node)
        db.flush()
        print(f"\nVolume {vol_num}: {vol_title}")

        part_dirs = sorted(vol_dir.glob("part-*"))
        for part_dir in part_dirs:
            m = re.search(r'part-([a-zA-Z]+)', part_dir.name)
            if not m:
                continue
            part_letter = m.group(1).upper()

            # Read part metadata if available
            part_meta_path = part_dir / "index.json"
            part_title = f"Part {part_letter}"
            if part_meta_path.exists():
                meta = json.loads(part_meta_path.read_text(encoding="utf-8"))
                part_title = meta.get("title", part_title) if "title" in meta else part_title

            part_citation = f"{vol_citation}, Pt. {part_letter}"
            part_node = Node(
                source="uscis_manual",
                parent_id=vol_node.id,
                level=1,
                title=part_title,
                citation=part_citation,
                metadata_={"volume": vol_num, "part": part_letter},
            )
            db.add(part_node)
            db.flush()
            print(f"  Part {part_letter}: {part_title[:60]}")

            chapter_files = sorted(part_dir.glob("chapter-*.html"))
            for ch_file in chapter_files:
                m = re.search(r'chapter-(\d+)', ch_file.name)
                if not m:
                    continue
                ch_num = int(m.group(1))

                # Read chapter metadata if available
                ch_meta_path = ch_file.with_suffix(".json")
                ch_title = f"Chapter {ch_num}"
                if ch_meta_path.exists():
                    meta = json.loads(ch_meta_path.read_text(encoding="utf-8"))
                    ch_title = meta.get("title", ch_title)

                _ingest_chapter(
                    db, ch_file, part_node.id,
                    vol_num, part_letter, ch_num, ch_title,
                )

    db.commit()


def _ingest_chapter(
    db: Session,
    html_path: Path,
    part_node_id: int,
    vol_num: int,
    part_letter: str,
    ch_num: int,
    ch_title: str,
):
    """Parse a single chapter HTML file and create nodes."""
    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    # Try to get better title from the page itself
    page_title_el = soup.find("h1")
    if page_title_el:
        page_title = page_title_el.get_text(strip=True)
        if page_title and len(page_title) > 3:
            ch_title = page_title

    ch_citation = f"USCIS-PM Vol. {vol_num}, Pt. {part_letter}, Ch. {ch_num}"

    chapter_node = Node(
        source="uscis_manual",
        parent_id=part_node_id,
        level=2,
        title=ch_title,
        citation=ch_citation,
        metadata_={
            "volume": vol_num,
            "part": part_letter,
            "chapter": ch_num,
            "source_file": str(html_path),
        },
    )
    db.add(chapter_node)
    db.flush()

    # Find main content
    content_el = _find_content_element(soup)
    if not content_el:
        print(f"    Ch. {ch_num}: WARNING — could not find content container")
        chapter_node.full_text = "(Content extraction failed — HTML structure not recognized)"
        db.flush()
        return

    # Parse sections from content
    sections = _extract_sections_from_content(content_el)

    if not sections:
        # No headings — treat entire content as a single leaf
        all_text = content_el.get_text(strip=True)
        if all_text:
            chapter_node.full_text = all_text
            _store_uscis_cross_references(db, chapter_node.id, all_text)
        db.flush()
        print(f"    Ch. {ch_num}: {ch_title[:50]} (no sections, stored as leaf)")
        return

    # Build section tree
    _build_section_tree(
        db, sections, chapter_node.id,
        base_level=2,
        citation_prefix=ch_citation,
    )

    section_count = len(sections)
    print(f"    Ch. {ch_num}: {ch_title[:50]} ({section_count} sections)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch and ingest USCIS Policy Manual",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch volumes 2, 6, 7 from USCIS website
  python -m scripts.ingest_uscis --fetch --volumes 2 6 7

  # Ingest cached HTML into database
  python -m scripts.ingest_uscis --ingest

  # Both in one step
  python -m scripts.ingest_uscis --fetch --ingest --volumes 2 6 7
        """,
    )
    parser.add_argument(
        "--fetch", action="store_true",
        help="Crawl USCIS website and cache HTML pages locally",
    )
    parser.add_argument(
        "--ingest", action="store_true",
        help="Parse cached HTML into the database Node tree",
    )
    parser.add_argument(
        "--volumes", type=int, nargs="+", default=DEFAULT_VOLUMES,
        help=f"Volume numbers to fetch (default: {DEFAULT_VOLUMES})",
    )
    parser.add_argument(
        "--data-dir", type=str, default=str(DEFAULT_DATA_DIR),
        help=f"Directory for cached HTML files (default: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()

    if not args.fetch and not args.ingest:
        parser.print_help()
        print("\nError: specify --fetch, --ingest, or both.")
        return

    data_dir = Path(args.data_dir)

    if args.fetch:
        print(f"=== Fetching USCIS Policy Manual (Volumes: {args.volumes}) ===")
        print(f"Cache directory: {data_dir}")
        print(f"Rate limit: {REQUEST_DELAY}s between requests\n")

        fetcher = PolicyManualFetcher(data_dir)
        try:
            fetcher.fetch_volumes(args.volumes)
        finally:
            fetcher.close()

    if args.ingest:
        print(f"\n=== Ingesting USCIS Policy Manual from {data_dir} ===")

        if not data_dir.exists():
            print(f"Error: {data_dir} does not exist. Run with --fetch first.")
            return

        Base.metadata.create_all(bind=engine)

        with SessionLocal() as db:
            print("Clearing existing USCIS nodes...")
            clear_uscis_nodes(db)
            ingest_from_cache(db, data_dir)

            # Print stats
            total = db.query(Node).filter(Node.source == "uscis_manual").count()
            leaves = db.query(Node).filter(
                Node.source == "uscis_manual",
                Node.full_text.isnot(None),
            ).count()
            print(f"\n=== Ingestion Complete ===")
            print(f"Total USCIS nodes: {total}")
            print(f"Leaf nodes: {leaves}")
            print("\nNext steps:")
            print("  python -m scripts.resolve_crossrefs  # Resolve cross-references")
            print("  python -m scripts.summarize_tree      # Generate LLM summaries")


if __name__ == "__main__":
    main()
