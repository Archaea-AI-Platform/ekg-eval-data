"""Fill what the adapters left unfilled, with query-master's LLM: each gap accepted or rejected, no person after it.

Reads a graph build_graph.py wrote, and writes next to it, in <graph>/llm/: records.jsonl and links.jsonl
(only what this stage added or changed), ledger.jsonl (every verdict), responses.jsonl (every LLM
response with its prompt, so a stopped run resumes without paying twice),
patterns.json (the cues behind accepted answers: rules an adapter could learn) and report.json.

The LLM backend and key come from query-master's settings in the environment, never from this repo:

    export LLM_BACKEND=openrouter OPENROUTER_API_KEYS=<key> EKG_LLM_MODEL=mistralai/devstral-2512
    python -I ekg-eval-data/fill_graph.py --dry-run                 # find gaps and candidates, no LLM
    python -I ekg-eval-data/fill_graph.py --smoke 2                 # two real calls, nothing written
    python -I ekg-eval-data/fill_graph.py --kinds pr_ticket,identity --limit 50
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters.fill import run  # noqa: E402
from ekg_adapters.fill_tasks import BUILDERS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", default=str(HERE.parent / "ekg-eval-out" / "apache-kafka"), help="a build_graph.py output folder")
    ap.add_argument("--query-master", default=str(HERE.parent / "query-master"), help="query-master checkout")
    ap.add_argument("--out", default=None, help="where to write (default <graph>/llm)")
    ap.add_argument("--kinds", default=",".join(BUILDERS), help=f"comma list of {', '.join(BUILDERS)}")
    ap.add_argument("--limit", type=int, default=None, help="ask at most this many new tasks per kind")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--model", default=None, help="override EKG_LLM_MODEL / AGENTIC_LLM_MODEL")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="build tasks and sample prompts only")
    mode.add_argument("--smoke", type=int, metavar="N", help="ask N tasks (one per kind) and print the answers")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    kinds = [k for k in args.kinds.split(",") if k]
    if bad := [k for k in kinds if k not in BUILDERS]:
        ap.error(f"unknown kinds: {bad}")
    report = run(Path(args.graph), Path(args.query_master), Path(args.out) if args.out else None, kinds, args.limit,
                 "dry" if args.dry_run else "smoke" if args.smoke else "run", args.smoke or 0, args.concurrency, args.model)
    if report["mode"] == "run":
        print("\nPer kind:")
        for k, r in report["kinds"].items():
            print(f"  {k:<16} {json.dumps({x: r.get(x, 0) for x in ('to_ask', 'accepted', 'rejected', 'error', 'cached')})}")
        print("Written:", json.dumps(report["written"]))
    errors = [s for s in report.get("smoke", []) if s["entry"]["status"] == "error"]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
