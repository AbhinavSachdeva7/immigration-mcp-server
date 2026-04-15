"""Bottom-Up LLM Summarization Pipeline (Phase 2b)

Implements the PageIndex pattern: walks the legal node tree bottom-up,
generating summaries at each level using an LLM.

Process:
  1. Find all leaf nodes (have full_text, no children) — summarize their text
  2. Find all nodes whose children are all summarized — summarize from children
  3. Repeat level by level until the root is reached

Within each level, nodes are processed concurrently (up to the rate limit).
This is the map-reduce pattern described in the architecture docs.

Rate limiting:
  Controlled by LLM_RPM env var (default 14, safe for Gemini free tier at 15 RPM).
  Transient failures (429, 5xx) are retried with exponential backoff.

Usage:
    # Summarize all unsummarized nodes
    python -m scripts.summarize_tree

    # Re-summarize everything (overwrite existing summaries)
    python -m scripts.summarize_tree --force

    # Dry run — show what would be summarized without calling LLM
    python -m scripts.summarize_tree --dry-run

Environment variables:
    LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, LLM_RPM
    See src/llm.py for details.
"""

import argparse
import asyncio
import sys

from sqlalchemy.orm import Session

from src.db.database import Base, SessionLocal, engine
from src.db.models import Node
from src.llm import LLMClient


# ---------------------------------------------------------------------------
# Tree analysis
# ---------------------------------------------------------------------------

_LEGAL_SOURCES = {"federal_register", "uscis_manual", "ecfr", "ina"}


def _build_tree_info(db: Session) -> tuple[dict[int, list[int]], dict[int, Node]]:
    """Build parent→children mapping and node lookup for the entire legal tree.

    Returns (children_map, node_map).
    """
    all_nodes = db.query(Node).filter(Node.source.in_(_LEGAL_SOURCES)).all()

    node_map: dict[int, Node] = {}
    children_map: dict[int, list[int]] = {}

    for node in all_nodes:
        node_map[node.id] = node
        if node.parent_id is not None:
            children_map.setdefault(node.parent_id, []).append(node.id)

    return children_map, node_map


def _compute_levels(
    children_map: dict[int, list[int]],
    node_map: dict[int, Node],
) -> list[list[int]]:
    """Group nodes into bottom-up processing levels.

    Level 0: leaf nodes (no children)
    Level 1: nodes whose children are all leaves
    Level N: nodes whose children are all in levels < N

    Returns a list of lists, where levels[0] = leaf node IDs, etc.
    """
    # Compute depth from bottom (reverse topological order)
    depths: dict[int, int] = {}

    def compute_depth(node_id: int) -> int:
        if node_id in depths:
            return depths[node_id]

        child_ids = children_map.get(node_id, [])
        if not child_ids:
            depths[node_id] = 0
            return 0

        max_child_depth = max(compute_depth(cid) for cid in child_ids)
        depths[node_id] = max_child_depth + 1
        return depths[node_id]

    for node_id in node_map:
        compute_depth(node_id)

    # Group by depth
    max_depth = max(depths.values()) if depths else 0
    levels: list[list[int]] = [[] for _ in range(max_depth + 1)]

    for node_id, depth in depths.items():
        levels[depth].append(node_id)

    return levels


# ---------------------------------------------------------------------------
# Summarization workers
# ---------------------------------------------------------------------------

async def _summarize_leaf(
    client: LLMClient,
    node: Node,
    force: bool,
) -> bool:
    """Summarize a leaf node's full_text.

    Returns True if summary was generated, False if skipped.
    """
    if node.summary and not force:
        return False

    if not node.full_text or node.full_text.strip() == "(No content)":
        node.summary = "(No content to summarize)"
        return False

    # Skip very short text — use it as its own summary
    if len(node.full_text) < 200:
        node.summary = node.full_text
        return False

    try:
        summary = await client.summarize_text(node.full_text)
        node.summary = summary
        return True
    except Exception as e:
        print(f"    ERROR summarizing node {node.id} ({node.title[:50]}): {e}")
        node.summary = f"(Summarization failed: {e})"
        return False


async def _summarize_intermediate(
    client: LLMClient,
    node: Node,
    children: list[Node],
    force: bool,
) -> bool:
    """Summarize an intermediate node from its children's summaries.

    Returns True if summary was generated, False if skipped.
    """
    if node.summary and not force:
        # Check if it's a placeholder summary
        if not node.summary.startswith("Contains sections:"):
            return False

    child_summaries = [c.summary for c in children if c.summary]
    if not child_summaries:
        node.summary = f"Section with {len(children)} subsections."
        return False

    try:
        summary = await client.summarize_children(node.title, child_summaries)
        node.summary = summary
        return True
    except Exception as e:
        print(f"    ERROR summarizing node {node.id} ({node.title[:50]}): {e}")
        # Fall back to simple concatenation
        node.summary = f"Covers: {'; '.join(s[:100] for s in child_summaries[:3])}"
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_summarization(force: bool = False, dry_run: bool = False):
    """Run the bottom-up summarization pipeline."""
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        children_map, node_map = _build_tree_info(db)

        if not node_map:
            print("No legal nodes found. Run ingest_legal.py first.")
            return

        levels = _compute_levels(children_map, node_map)

        total_nodes = sum(len(level) for level in levels)
        print(f"Found {total_nodes} legal nodes in {len(levels)} levels.")
        print(f"  Level 0 (leaves): {len(levels[0])} nodes")
        for i in range(1, len(levels)):
            print(f"  Level {i} (intermediate): {len(levels[i])} nodes")

        if dry_run:
            # Count how many would need summarization
            needs_summary = 0
            for level in levels:
                for node_id in level:
                    node = node_map[node_id]
                    if not node.summary or force:
                        needs_summary += 1
                    elif node.summary.startswith("Contains sections:"):
                        needs_summary += 1
            print(f"\nDry run: {needs_summary} nodes need summarization.")
            return

        # Initialize LLM client
        try:
            client = LLMClient.from_env()
        except ValueError as e:
            print(f"ERROR: {e}")
            print("\nSet these environment variables in .env:")
            print("  LLM_API_KEY=your-api-key")
            print("  LLM_PROVIDER=gemini  (or openai, custom)")
            print("  LLM_MODEL=gemini-2.0-flash-lite  (optional)")
            return

        try:
            llm_calls = 0
            skipped = 0

            for level_idx, level_node_ids in enumerate(levels):
                if not level_node_ids:
                    continue

                if level_idx == 0:
                    print(f"\n--- Level 0: Summarizing {len(level_node_ids)} leaf nodes ---")
                    for i, node_id in enumerate(level_node_ids):
                        node = node_map[node_id]
                        called = await _summarize_leaf(client, node, force)
                        if called:
                            llm_calls += 1
                            if llm_calls % 10 == 0:
                                print(f"    Progress: {llm_calls} LLM calls made, processing node {i+1}/{len(level_node_ids)}")
                        else:
                            skipped += 1
                        # Flush every 50 nodes to avoid large uncommitted batches
                        if (i + 1) % 50 == 0:
                            db.flush()
                else:
                    print(f"\n--- Level {level_idx}: Summarizing {len(level_node_ids)} intermediate nodes ---")
                    for i, node_id in enumerate(level_node_ids):
                        node = node_map[node_id]
                        child_ids = children_map.get(node_id, [])
                        children = [node_map[cid] for cid in child_ids if cid in node_map]
                        called = await _summarize_intermediate(client, node, children, force)
                        if called:
                            llm_calls += 1
                        else:
                            skipped += 1
                        if (i + 1) % 50 == 0:
                            db.flush()

                db.flush()
                print(f"  Level {level_idx} complete. Total LLM calls so far: {llm_calls}")

            db.commit()
            print(f"\n=== Summarization Complete ===")
            print(f"LLM calls made: {llm_calls}")
            print(f"Skipped (already summarized or too short): {skipped}")

        finally:
            await client.close()


def main():
    parser = argparse.ArgumentParser(description="Bottom-up LLM summarization of legal node tree")
    parser.add_argument("--force", action="store_true", help="Re-summarize all nodes, overwriting existing summaries")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be summarized without calling LLM")
    args = parser.parse_args()

    asyncio.run(run_summarization(force=args.force, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
