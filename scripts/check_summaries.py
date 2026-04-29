"""Diagnostic script: check summary status across all legal nodes.

Run from the project root:
    python scripts/check_summaries.py

Reports:
  - Total nodes by level
  - Empty / missing summaries
  - Summary length distribution
"""

import sys
from collections import defaultdict

sys.path.insert(0, "src")

from db.database import SessionLocal
from db.models import Node

_LEGAL_SOURCES = {"federal_register", "uscis_manual", "ecfr", "ina"}


def main():
    with SessionLocal() as db:
        all_nodes = db.query(Node).filter(Node.source.in_(_LEGAL_SOURCES)).all()

        if not all_nodes:
            print("No legal nodes found.")
            return

        # Build children map to determine leaf vs intermediate
        children_map: dict[int, list[int]] = {}
        node_map: dict[int, Node] = {}
        for node in all_nodes:
            node_map[node.id] = node
            if node.parent_id is not None:
                children_map.setdefault(node.parent_id, []).append(node.id)

        is_leaf = {n.id: n.id not in children_map for n in all_nodes}

        # Categorise by level (leaf=0, intermediate=1+)
        by_level: dict[str, list[Node]] = defaultdict(list)
        for node in all_nodes:
            kind = "leaf" if is_leaf[node.id] else "intermediate"
            by_level[kind].append(node)

        print(f"Total nodes: {len(all_nodes)}\n")
        c = 0
        for kind in ("leaf", "intermediate"):
            nodes = by_level[kind]
            if not nodes:
                c+=1
                continue

            empty      = [n for n in nodes if not n.summary]
            blank      = [n for n in nodes if n.summary is not None and n.summary.strip() == ""]
            no_content = [n for n in nodes if n.summary == "(No content to summarize)"]
            failed     = [n for n in nodes if n.summary and n.summary.startswith("(Summarization failed")]
            real       = [n for n in nodes if n.summary and n.summary.strip() and
                          not n.summary.startswith("(") and
                          not n.summary.startswith("Contains sections:") and
                          not n.summary.startswith("Section with") and
                          not n.summary.startswith("Covers:")]

            lengths = [len(n.summary) for n in nodes if n.summary and n.summary.strip()]

            print(f"--- {kind.upper()} nodes ({len(nodes)} total) ---")
            print(f"  Real LLM summaries : {len(real)}")
            print(f"  No summary (NULL)  : {len(empty)}")
            print(f"  Blank ('')         : {len(blank)}")
            print(f"  No content nodes   : {len(no_content)}")
            print(f"  Failed             : {len(failed)}")
            print(c)

            if lengths:
                print(f"  Summary length     : min={min(lengths)}, max={max(lengths)}, avg={sum(lengths)//len(lengths)}")

            if blank:
                print(f"\n  Blank summary node IDs (first 20): {[n.id for n in blank[:20]]}")
            if empty:
                print(f"  NULL summary node IDs (first 20) : {[n.id for n in empty[:20]]}")

            if real:
                print(f"\n  No-content nodes:")
                print(f"  {'ID':<8} {'Source':<20} {'Citation':<30} {'Title'}")
                print(f"  {'-'*8} {'-'*20} {'-'*30} {'-'*40}")
                for n in real:
                    citation = (n.citation or "")[:28]
                    title = (n.title or "")[:60]
                    print(f"  {n.id:<8} {n.source:<20} {citation:<30} {title}")
            
            print()


if __name__ == "__main__":
    main()
