"""Turn a fetch_apache.py download into EKG records and links: adapters, cross-source linking, identity.

Writes records.jsonl and links.jsonl (the spec's two shapes, every item validated
with query-master's validator) and a report. Exit code 1 if anything fails validation.

    python -I ekg-eval-data/build_graph.py                       # -> ekg-eval-out/apache-kafka/
    python -I ekg-eval-data/build_graph.py --raw-check --json report.json
    python -I ekg-eval-data/build_graph.py --data ekg-eval-raw/apache-flink --no-write
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # -I leaves the script's folder off the path; the adapters live next to it

from ekg_adapters.pipeline import run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=str(HERE.parent / "ekg-eval-raw" / "apache-kafka"), help="a fetch_apache.py output folder")
    ap.add_argument("--query-master", default=str(HERE.parent / "query-master"), help="query-master checkout")
    ap.add_argument("--out", default=None, help="where to write the graph (default ekg-eval-out/<data folder name>)")
    ap.add_argument("--no-write", action="store_true", help="validate and report only")
    ap.add_argument("--raw-check", action="store_true", help="also validate the raw JSON as-is (expected to fail)")
    ap.add_argument("--json", default=None, help="also write the report as JSON to this path")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    data = Path(args.data)
    out = None if args.no_write else Path(args.out or HERE.parent / "ekg-eval-out" / data.name)
    print(f"Building the graph from {data}")
    report = run(data, Path(args.query_master), out, args.raw_check)

    t = report["totals"]
    if report["raw"]:
        print("\nRaw JSON as-is (expected to fail):")
        for k, v in report["raw"].items():
            print(f"  {v['valid']:>6,} of {v['checked']:<6,} {k} valid; e.g. {next(iter(v['top_errors']), '')}")
    print(f"\n{t['records']:,} records ({t['records_valid']:,} valid), {t['links']:,} links ({t['links_valid']:,} valid), "
          f"{t['record_types']} record types, {t['link_types']} link types")
    failing = [k for k, v in {**report["records"], **report["links"]}.items() if v["checked"] != v["valid"]]
    for k in failing:
        row = report["records"].get(k) or report["links"][k]
        print(f"  FAIL {k}: {row['checked'] - row['valid']} invalid; {next(iter(row['top_errors']), '')}")
    print("\nCross-pillar links:")
    for k, v in report["cross_pillar"].items():
        print(f"  {v:>7,}  {k}")
    print("\nCoverage:")
    for k, v in report["coverage"].items():
        print(f"  {v['linked']:>6,} of {v['of']:<6,} {k}")
    print("\nIdentity:", json.dumps(report["identity"]["persons_by_tool_count"]), json.dumps(report["identity"]["links"]))
    print("Stub records for references outside the download:", report["stubs"])
    if out:
        print(f"\ngraph written to {out}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"report written to {args.json}")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
