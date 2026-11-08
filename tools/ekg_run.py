"""Run our downloads through ekg-eval-data: layout -> inventory -> parse -> LLM fill -> SQLite. Resumable, nothing lost.

    python ekg-eval-data/tools/ekg_run.py --llm dry flink iceberg-python maka ossie      # everything but LLM calls
    python ekg-eval-data/tools/ekg_run.py --llm run flink iceberg-python maka ossie      # resumes: stored responses are reused

Everything lands in test_data_sorce_graph/ekg_runs/<name>/:
  input/            the adapters' layout (links to our raw files, PR bundles, commits.jsonl, manifest.json)
  inventory.json    every raw file: read by which adapter, or why not (no file is dropped silently)
  out/              records.jsonl, links.jsonl, report.json, llm/ (ledger, responses, patterns), graph.sqlite(.gz)
  logs/<step>.log   full output of each step;  state.json  which steps are done, with their counts
A step marked done is skipped on the next run; the LLM step always runs and answers from llm/responses.jsonl
first, so a stopped run costs nothing it already paid for.
"""
import argparse
import collections
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # archaea_beta/
DATA = Path(os.environ.get("EKG_OUR_DATA", ROOT / "test_data_sorce_graph"))  # our downloads: <name>/raw, <name>/codebase
RUNS = DATA / "ekg_runs"
QM = Path(os.environ.get("EKG_QUERY_MASTER", ROOT / "query-master-new-knowledge-graph"))  # a branch's worktree, to test it
EKG = Path(os.environ.get("EKG_DATA_CODE", ROOT / "ekg-eval-data"))
sys.path.insert(0, str(EKG))

from ekg_adapters import fill, pipeline, sqlite_store  # noqa: E402

# raw file -> (status, what reads it). First matching prefix wins.
USE = [
    ("jira/search/", "used", "jira adapter (issues, changelog, comments)"),
    ("jira/versions.json", "used", "jira adapter (releases)"),
    ("jira/components.json", "linked", "in the layout; the jira adapter does not read components"),
    ("jira/remotelinks/", "not read", "no adapter reads Jira remote links (full URLs, incl. other repos)"),
    ("jira/referenced/", "not read", "issues outside the window that window issues name; no adapter reads them"),
    ("github/pulls/", "used", "github adapter (regrouped into one bundle per PR)"),
    ("github/reviews/", "used", "github adapter (in the PR bundle)"),
    ("github/review_comments/", "used", "github adapter (in the PR bundle)"),
    ("github/issue_comments/", "used", "github adapter (PR bundle) and issues adapter (issue comments)"),
    ("github/issues/", "used", "issues adapter (PRs in the same pages are left to the github adapter)"),
    ("github/repo.json", "used", "github adapter (repository)"),
    ("github/graphql/", "not read", "no adapter reads GitHub's commit -> PR answers"),
    ("github/users/", "not read", "no adapter reads user profiles (names, emails)"),
    ("git/", "replaced", "commits.jsonl is regenerated from codebase/ for the same window (same commits)"),
    ("mail/", "used", "mail adapter (.mbox); stats_*.json are download counters"),
    ("slack/", "not downloaded", "NOT_DOWNLOADED.md"),
]
META = ("manifest.json", "manifest.jsonl", "fields.json", "project.json", "NOT_DOWNLOADED.md", "branches.txt",
        "branches_skipped.txt")


def inventory(name):
    rows = collections.defaultdict(lambda: {"files": 0, "bytes": 0})
    for p in sorted((DATA / name / "raw").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(DATA / name / "raw").as_posix()
        if p.name in META or p.name.startswith("stats_"):
            status, why = "metadata", "download bookkeeping"
        else:
            status, why = next(((s, w) for pre, s, w in USE if rel.startswith(pre)), ("not read", "no rule: check"))
        key = (rel.split("/")[0] + "/" + (rel.split("/")[1] if "/" in rel else ""), status, why)
        rows[key]["files"] += 1
        rows[key]["bytes"] += p.stat().st_size
    code = DATA / name / "codebase"
    n_code = sum(1 for p in code.rglob("*") if p.is_file() and ".git" not in p.parts)
    out = [{"path": k[0], "status": k[1], "read_by": k[2], **v} for k, v in sorted(rows.items())]
    out.append({"path": "codebase/", "status": "used", "read_by": "code adapter (tree, docs text, Python symbols)",
                "files": n_code, "bytes": None})
    return out


def logged(path, fn, *a, **kw):
    """Run fn with its output copied to a log file; re-raise after logging a failure."""
    buf = io.StringIO()

    class Tee(io.TextIOBase):
        def write(self, s):
            buf.write(s)
            sys.__stdout__.write(s)
            return len(s)

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with contextlib.redirect_stdout(Tee()), contextlib.redirect_stderr(Tee()):
            return fn(*a, **kw)
    except BaseException:
        buf.write(traceback.format_exc())
        raise
    finally:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n" + buf.getvalue())


def run_one(name, args):
    base = RUNS / name
    inp, out, logs = base / "input", base / "out", base / "logs"
    state_path = base / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    def done(step, info):
        state[step] = {"done": True, "at": f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}", **info}
        state_path.write_text(json.dumps(state, indent=1))

    print(f"\n######## {name}")
    if not state.get("layout", {}).get("done"):
        res = subprocess.run([sys.executable, str(Path(__file__).with_name("shim_all.py")), str(DATA / name), str(inp),
                              str(RUNS / "_shared/people")], capture_output=True, text=True)
        (logs).mkdir(parents=True, exist_ok=True)
        (logs / "layout.log").write_text(res.stdout + res.stderr)
        if res.returncode:
            raise SystemExit(f"{name}: layout failed, see {logs / 'layout.log'}")
        done("layout", json.loads(res.stdout.strip().splitlines()[-1]))
    print("layout:", state["layout"])
    if not state.get("inventory", {}).get("done"):
        inv = inventory(name)
        (base / "inventory.json").write_text(json.dumps(inv, indent=1))
        done("inventory", {"not_read": [r["path"] for r in inv if r["status"] == "not read"]})
    print("inventory: not read ->", state["inventory"]["not_read"])

    if not state.get("parse", {}).get("done"):
        rep = logged(logs / "parse.log", pipeline.run, inp, QM, out)
        (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
        done("parse", {"totals": rep["totals"], "adapters": {k: v.get("status", v) if isinstance(v, dict) else v
                                                             for k, v in (rep.get("adapters") or {}).items()}})
    print("parse:", state["parse"]["totals"])

    if args.llm != "skip":
        rep = logged(logs / f"llm_{args.llm}.log", fill.run, out, QM, None, None, args.limit, args.llm, 0, args.concurrency)
        info = {k: {x: v.get(x, 0) for x in ("to_ask", "accepted", "rejected", "error")} for k, v in rep["kinds"].items()}
        state[f"llm_{args.llm}"] = {"done": args.llm == "dry" or not any(v["error"] for v in info.values()),
                                    "calls": rep.get("calls"), "kinds": info}
        state_path.write_text(json.dumps(state, indent=1))
        print(f"llm ({args.llm}):", json.dumps(info))

    saved = logged(logs / "save.log", sqlite_store.save, out, QM)
    done("save", {k: saved.get(k) for k in ("size_mb", "gzip_mb", "records_by_stage", "links_by_stage", "invalid_skipped")})
    print("save:", state["save"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="+")
    ap.add_argument("--llm", choices=("dry", "run", "skip"), default="dry")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    failed = []
    for name in args.names:
        try:
            run_one(name, args)
        except BaseException as e:  # one dataset failing never stops the others; its log says why
            failed.append(name)
            print(f"!! {name} failed: {type(e).__name__}: {str(e)[:300]}")
            if isinstance(e, KeyboardInterrupt):
                raise
    print("\nfailed:", failed or "none")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
