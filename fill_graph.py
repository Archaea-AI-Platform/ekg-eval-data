"""Fill what the adapters left unfilled, with query-master's LLM: each gap accepted or rejected, no person after it.

Reads a graph build_graph.py wrote, and writes next to it, in <graph>/llm/: records.jsonl and links.jsonl
(only what this stage added or changed), ledger.jsonl (every verdict), responses.jsonl (every LLM
response with its prompt, so a stopped run resumes without paying twice),
patterns.json (the cues behind accepted answers: rules an adapter could learn) and report.json.

Every document is read whole, in windows sized from the model's context window; every table is mapped
with one call and written row by row in code. See ekg_adapters/fill.py.

The LLM backend and key come from query-master's settings in the environment, never from this repo:

    export LLM_BACKEND=openrouter OPENROUTER_API_KEYS=<key> EKG_LLM_MODEL=meta/muse-glimmer-30b
    python -I ekg-eval-data/fill_graph.py --dry-run                 # tasks, windows and tokens per kind, no LLM
    python -I ekg-eval-data/fill_graph.py --smoke 2                 # two real calls, nothing written
    python -I ekg-eval-data/fill_graph.py --kinds doc_facts,table_map --docs 20     # a cheap test: 20 documents
    python -I ekg-eval-data/fill_graph.py --kinds pr_ticket,identity --limit 50
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters.fill import run  # noqa: E402
from ekg_adapters.fill_tasks import BUILDERS, DEFAULT_KINDS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", default=str(HERE.parent / "ekg-eval-out" / "apache-kafka"), help="a build_graph.py output folder")
    ap.add_argument("--query-master", default=str(HERE.parent / "query-master"), help="query-master checkout")
    ap.add_argument("--out", default=None, help="where to write (default <graph>/llm)")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS),
                    help=f"comma list of {', '.join(BUILDERS)} (default: all but doc_knowledge, which doc_facts covers)")
    ap.add_argument("--limit", type=int, default=None, help="ask at most this many new tasks per kind (test runs)")
    ap.add_argument("--docs", type=int, default=None, metavar="N",
                    help="read only N documents, evenly spaced over all of them (test runs; the report says so)")
    ap.add_argument("--concurrency", type=int, default=16, help="model calls in flight at once (default 16)")
    ap.add_argument("--window-tokens", type=int, default=None,
                    help="the model's context window, when the backend cannot say (default: asked from query-master)")
    ap.add_argument("--max-reply-tokens", type=int, default=None,
                    help="the provider's cap on one reply, when the backend cannot say (OpenRouter lists it)")
    ap.add_argument("--model", default=None, help="override EKG_LLM_MODEL / AGENTIC_LLM_MODEL")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="build tasks, sample prompts and the token estimate only")
    mode.add_argument("--smoke", type=int, metavar="N", help="ask N tasks (one per kind) and print the answers")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # a redirected log stays current

    kinds = [k for k in args.kinds.split(",") if k]
    if bad := [k for k in kinds if k not in BUILDERS]:
        ap.error(f"unknown kinds: {bad}")
    report = run(Path(args.graph), Path(args.query_master), Path(args.out) if args.out else None, kinds, args.limit,
                 "dry" if args.dry_run else "smoke" if args.smoke else "run", args.smoke or 0, args.concurrency, args.model,
                 window_tokens=args.window_tokens, docs=args.docs, max_reply_tokens=args.max_reply_tokens)
    if report["mode"] == "dry":
        print(f"\n{'kind':<16} {'tasks':>8} {'calls':>8} {'prompt chars':>14} {'tokens (upper)':>15} {'tokens (typical)':>17}")
        for k, r in report["kinds"].items():
            up, typ = r["est_prompt_tokens"].values()
            print(f"{k:<16} {r['to_ask']:>8,} {r['to_ask'] * r['calls_per_task']:>8,} {r['prompt_chars_total']:>14,} {up:>15,} {typ:>17,}")
        t = report["totals"]
        print(f"{'total':<16} {t['tasks']:>8,} {t['calls']:>8,} {t['prompt_chars']:>14,} {t['est_prompt_tokens_upper']:>15,} "
              f"{t['est_prompt_tokens_typical']:>17,}")
        print("(prompt tokens only: the reply, thinking included, is extra and unknown until a real run)")
    if report["mode"] == "run":
        print("\nPer kind:")
        for k, r in report["kinds"].items():
            print(f"  {k:<16} {json.dumps({x: r.get(x, 0) for x in ('to_ask', 'accepted', 'rejected', 'error', 'cached')})}")
        print("Written:", json.dumps(report["written"]))
    errors = [s for s in report.get("smoke", []) if s["entry"]["status"] == "error"]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
