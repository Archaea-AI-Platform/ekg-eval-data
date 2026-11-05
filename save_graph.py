"""Save a graph (adapters' records/links.jsonl, plus the LLM stage's <graph>/llm/ when present) as SQLite in
query-master's storage shape: ekg_registry_types, ekg_registry_links, ekg_records, ekg_links.

    python -I ekg-eval-data/save_graph.py                                  # -> ekg-eval-out/apache-kafka/graph.sqlite
    python -I ekg-eval-data/save_graph.py --graph ekg-eval-out/apache-flink --sqlite /tmp/flink.sqlite
    python -I ekg-eval-data/save_graph.py --graph ekg-eval-out/apache-kafka ekg-eval-out/apache-tvm \
        --sqlite ekg-eval-out/combined.sqlite                               # one combined graph

With several --graph folders, the same item from two downloads (a person, an address, a ticket one
download only had as a stub) is one row, so links made in one download reach records from the other.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters.sqlite_store import save  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", nargs="+", default=[str(HERE.parent / "ekg-eval-out" / "apache-kafka")],
                    help="one or more build_graph.py output folders")
    ap.add_argument("--query-master", default=str(HERE.parent / "query-master"), help="query-master checkout")
    ap.add_argument("--sqlite", default=None, help="output file (default <graph>/graph.sqlite; required for several)")
    args = ap.parse_args()
    if len(args.graph) > 1 and not args.sqlite:
        ap.error("several --graph folders need --sqlite")
    report = save([Path(x) for x in args.graph], Path(args.query_master), Path(args.sqlite) if args.sqlite else None)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 1 if report["invalid_skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
