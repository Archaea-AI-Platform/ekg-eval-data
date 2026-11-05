"""Parse, then LLM, then save: one download -> one graph in query-master's schema, as JSONL per stage and one SQLite.

  1. adapters    build_graph's pipeline      -> <out>/records.jsonl, links.jsonl, report.json
  2. LLM fill    fill_graph's stage          -> <out>/llm/records.jsonl, links.jsonl, ledger.jsonl, patterns.json
  3. save        save_graph's SQLite export  -> <out>/graph.sqlite (ekg_registry_*, ekg_records, ekg_links)

    export LLM_BACKEND=openrouter OPENROUTER_API_KEYS=<key> EKG_LLM_MODEL=mistralai/devstral-2512
    python -I ekg-eval-data/run_pipeline.py --data ekg-eval-raw/apache-kafka
    python -I ekg-eval-data/run_pipeline.py --data ekg-eval-raw/apache-flink --llm skip     # no LLM stage
    python -I ekg-eval-data/run_pipeline.py --data ekg-eval-raw/apache-kafka ekg-eval-raw/apache-tvm \
        --combined ekg-eval-out/combined.sqlite      # each download as above, then one combined graph
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters import fill, pipeline, sqlite_store  # noqa: E402
from ekg_adapters.fill_tasks import BUILDERS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", nargs="+", default=[str(HERE.parent / "ekg-eval-raw" / "apache-kafka")],
                    help="one or more fetch_apache.py output folders")
    ap.add_argument("--query-master", default=str(HERE.parent / "query-master"), help="query-master checkout")
    ap.add_argument("--out", default=None, help="default ekg-eval-out/<data folder name> (one --data only)")
    ap.add_argument("--combined", default=None, help="also save every download's graph into this one SQLite file")
    ap.add_argument("--llm", choices=("run", "dry", "skip"), default="run", help="run the LLM stage, only count its tasks, or skip it")
    ap.add_argument("--kinds", default=",".join(BUILDERS))
    ap.add_argument("--limit", type=int, default=None, help="ask at most this many new tasks per kind")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    qm = Path(args.query_master)
    if args.out and len(args.data) > 1:
        ap.error("--out takes one --data folder; several go to ekg-eval-out/<name>")
    failed, outs = 0, []
    for d in args.data:
        data = Path(d)
        out = Path(args.out or HERE.parent / "ekg-eval-out" / data.name)
        outs.append(out)
        failed += one(data, out, qm, args)
    if args.combined:
        print(f"combined: {', '.join(o.name for o in outs)} -> {args.combined}")
        saved = sqlite_store.save(outs, qm, Path(args.combined))
        print(f"   {saved['sqlite']} ({saved['size_mb']} MB) records {saved['records_by_stage']}\n"
              f"   merged across graphs {saved['records_merged_across_graphs']}")
        failed += bool(saved["invalid_skipped"])
    return 1 if failed else 0


def one(data, out, qm, args) -> int:
    print(f"1. adapters: {data} -> {out}")
    report = pipeline.run(data, qm, out)
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    t = report["totals"]
    print(f"   {t['records']:,} records ({t['records_valid']:,} valid), {t['links']:,} links ({t['links_valid']:,} valid)")

    if args.llm != "skip":
        print(f"2. LLM fill ({args.llm})")
        res = fill.run(out, qm, None, args.kinds.split(","), args.limit, args.llm, 0, args.concurrency)
        for k, r in res["kinds"].items():
            print(f"   {k:<16} to ask {r['to_ask']:>5,}  accepted {r.get('accepted', 0):>5,}  rejected {r.get('rejected', 0):>5,}  "
                  f"error {r.get('error', 0):>4,}")
    print("3. save")
    saved = sqlite_store.save(out, qm)
    print(f"   {saved['sqlite']} ({saved['size_mb']} MB) records {saved['records_by_stage']} links {saved['links_by_stage']}")
    return 1 if saved["invalid_skipped"] or t["records"] != t["records_valid"] or t["links"] != t["links_valid"] else 0


if __name__ == "__main__":
    sys.exit(main())
