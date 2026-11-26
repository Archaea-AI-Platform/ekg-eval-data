"""Run a question set through the graph agent (LLM tool calling) and grade the answers.

Any question file against any graph: --questions names the set, --graph the graph.sqlite, --tenant one
tenant of a combined graph, --asker who asks (default: each question's own asker, else the public). Each
question is asked as of its `as_of` date when it has one (the tools see the graph as it stood at the end
of that day; without one, as it stands now), and graded as eval_grading.py describes: acceptable variants
for script-graded questions, an LLM judge with the question's checklist for the rest, must_not_say always.
Everything the agent did is kept, so a failure can be read call by call.

The model, for the agent and the judge alike, comes from the environment (../glimmer.env through
../with_env.sh sets all of it: EKG_AGENT_BACKEND, EKG_AGENT_MODEL, EKG_JUDGE_MODEL, OPENROUTER_API_KEYS and
query-master's own JSON-mode and output-cap settings):

    PY=.venv/Scripts/python.exe
    ./with_env.sh $PY -I ekg-eval-data/run_eval.py --questions ekg-eval-data/eval/acme_queries.jsonl \
        --graph ekg-eval-out/acme-docs/graph.sqlite --out ekg-eval-data/eval/results/acme_<run name>
    ./with_env.sh $PY -I ekg-eval-data/run_eval.py --questions ekg-eval-data/eval/kafka_shared_queries.jsonl \
        --graph <graph.sqlite> --out ekg-eval-data/eval/results/kafka_<run name>
    ... run_eval.py --questions ... --ids XPD-031,KNW-066 --pillars code,people --limit 10 --concurrency 4

Questions run in parallel (--concurrency, default 8). A run can be stopped and started again: every finished
question is appended to <out>/results.jsonl at once, and a later run with the same --out asks only what is not
answered and graded there yet (a question that timed out, failed or was not graded is asked again). The run's
graph, model and question file are kept in <out>/run.json; a different graph or model in the same folder is
refused unless --fresh starts it over.

With --api, every question goes to a running query-master over HTTP instead (the graph agent's
search endpoint, streamed, as the web app calls it), reading the graph from query-master's own store:

    QUERY_MASTER_SERVICE_TOKEN=<token> ... run_eval.py --questions ... --api http://127.0.0.1:7111
    ... run_eval.py --questions ... --api http://127.0.0.1:7111 --user-token <a signed-in user's token>

Writes <out>/results.jsonl (one row per question: answer, grade, tool calls, tokens), <out>/summary.json,
and prints a table by pillar and category.
"""
import argparse
import asyncio
import collections
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "query-master"))
sys.path.insert(0, str(HERE))

from app.ekg.query import Graph, Tools, View, ask, default_client  # noqa: E402
from eval_grading import as_of_time, grade, load  # noqa: E402
from qm_client import stream_ask  # noqa: E402

RUN_KEYS = ("questions_file", "graph", "tenant", "model", "judge", "via", "asker", "as_of")


def judge_client():
    """The judge: the agent's own backend and settings, on EKG_JUDGE_MODEL when that names another model."""
    model = os.environ.get("EKG_JUDGE_MODEL")
    if not model or model == (os.environ.get("EKG_AGENT_MODEL") or os.environ.get("EKG_LLM_MODEL")):
        return default_client()
    from app.services.retrieval_engine.llm_tools import make_tool_client
    backend = (os.environ.get("EKG_JUDGE_BACKEND") or os.environ.get("EKG_AGENT_BACKEND")
               or os.environ.get("AGENTIC_LLM_BACKEND") or "openrouter")
    key = ""
    if backend == "openrouter":
        key = (os.environ.get("OPENROUTER_API_KEYS") or os.environ.get("OPENROUTER_API_KEY") or "").split(",")[0].strip()
    return make_tool_client(backend, model=model, api_key=key)


def finished(row: dict) -> bool:
    """A row that need not be asked again: answered and graded (a timeout, an error or no grade is asked again)."""
    return row.get("pass") is not None and not str(row.get("stopped") or "").startswith(("error", "timeout"))


def read_rows(path: Path) -> dict:
    """The latest row per question id in a results file (it is appended to while a run goes on)."""
    rows: dict = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:  # a line cut off when a run was stopped
                    continue
                rows[row["id"]] = row
    return rows


def summarise(rows: list, run: dict, extra: dict) -> dict:
    def table(key):
        t = collections.defaultdict(lambda: [0, 0])
        for r in rows:
            if r["pass"] is not None:
                t[str(r.get(key))][0] += bool(r["pass"])
                t[str(r.get(key))][1] += 1
        return {k: f"{v[0]}/{v[1]}" for k, v in sorted(t.items())}

    graded = [r for r in rows if r["pass"] is not None]
    return {"questions": len(rows), "graded": len(graded), "passed": sum(bool(r["pass"]) for r in graded),
            "by_pillar": table("pillar"), "by_category": table("category"), "by_method": table("method"),
            "by_difficulty": table("difficulty"),
            "tool_calls_per_question": round(sum(len(r["tool_calls"] or []) for r in rows) / max(1, len(rows)), 1),
            "tools_used": dict(collections.Counter(c["tool"] for r in rows for c in (r["tool_calls"] or [])).most_common()),
            "tokens": {k: sum((r["usage"] or {}).get(k, 0) for r in rows) for k in ("prompt_tokens", "completion_tokens")},
            "seconds_per_question": round(sum(r.get("seconds") or 0 for r in rows) / max(1, len(rows)), 1),
            "results_paged": sum(1 for r in rows if r.get("paged")), "compactions": sum(r.get("compactions") or 0 for r in rows),
            "stopped": dict(collections.Counter(str(r["stopped"]) for r in rows)),
            "model": next((r.get("model") for r in rows if r.get("model")), run.get("model")),
            "judge": run.get("judge"), "via": run.get("via"), "graph": run.get("graph"), "tenant": run.get("tenant"),
            "questions_file": run.get("questions_file"), **extra}


async def run(args):
    qs, meta = load(args.questions)
    if args.ids:
        keep = set(args.ids.split(","))
        qs = [q for q in qs if q["id"] in keep]
    if args.pillars:
        keep = set(args.pillars.split(","))
        qs = [q for q in qs if q["pillar"] in keep]
    if args.categories:
        keep = set(args.categories.split(","))
        qs = [q for q in qs if q["category"] in keep]
    if args.only_failed:  # rerun what did not pass in an earlier run (failed or not graded)
        done = {}
        for line in open(args.only_failed, encoding="utf-8"):
            row = json.loads(line)
            done[row["id"]] = row.get("pass")
        qs = [q for q in qs if done.get(q["id"]) is not True]
        print(f"rerunning {len(qs)} questions that did not pass in {args.only_failed}")
    if args.limit:
        qs = qs[: args.limit]
    graph_path = args.graph or meta.get("graph") or str(HERE.parent / "ekg-eval-out" / "apache-kafka" / "graph.sqlite")
    client, judge = default_client(), judge_client()
    run_id = {"questions_file": Path(args.questions).name, "graph": str(Path(graph_path).resolve()) if not args.api else
              f"{args.api} (query-master's store)", "tenant": args.tenant,
              "model": None if args.api else getattr(client, "model", None), "judge": getattr(judge, "model", None),
              "via": "query-master API " + args.api if args.api else "in process", "asker": args.asker, "as_of": args.as_of}

    # ---- an earlier run in the same folder: go on from it, or refuse to mix two different runs
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    res_path, run_path = out / "results.jsonl", out / "run.json"
    if args.fresh:
        for p in (res_path, run_path, out / "summary.json"):
            p.unlink(missing_ok=True)
    earlier = read_rows(res_path)
    if earlier:
        was = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else None
        if was is None and (out / "summary.json").is_file():  # results written before run.json existed
            old = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            was = {"questions_file": Path(str(old.get("questions_file") or "")).name, "graph": old.get("graph"),
                   "model": old.get("model"), "via": old.get("via")}
        differ = {k: (was.get(k), run_id[k]) for k in RUN_KEYS
                  if was and k in was and k != "judge" and was.get(k) != run_id[k] and not (k == "model" and args.api)}
        if differ or was is None:
            print(f"{out} already holds results of another run:" if differ else f"{out} holds results of an unknown run.")
            for k, (a, b) in differ.items():
                print(f"  {k}: was {a!r}, now {b!r}")
            print("Give another --out, or --fresh to start this folder over.")
            for c in {id(client): client, id(judge): judge}.values():
                if hasattr(c, "aclose"):
                    await c.aclose()
            return 2
    run_path.write_text(json.dumps(run_id, indent=1), encoding="utf-8")
    todo = [q for q in qs if not (q["id"] in earlier and finished(earlier[q["id"]]))]
    if len(todo) < len(qs):
        print(f"resuming: {len(qs) - len(todo)} of {len(qs)} questions are already answered and graded in {res_path}")

    g = None
    if args.api:
        print(f"asking query-master at {args.api} ({'as the signed-in user' if args.user_token else 'internal route'})")
    elif todo:
        t0 = time.time()
        g = Graph.from_sqlite(graph_path, tenant=args.tenant)
        print(f"graph {graph_path}{' tenant ' + args.tenant if args.tenant else ''}: {len(g.rec):,} records, "
              f"{len(g.links):,} links ({time.time() - t0:.1f}s)")
        if not g.rec:
            print("the graph has no records" + (f" for tenant {args.tenant!r}" if args.tenant else ""))
            for c in {id(client): client, id(judge): judge}.values():
                if hasattr(c, "aclose"):
                    await c.aclose()
            return 2
    views: dict = {}
    sem = asyncio.Semaphore(max(1, args.concurrency))
    print(f"model: {'(query-master decides)' if args.api else getattr(client, 'model', '?')}  judge: {getattr(judge, 'model', '?')}  "
          f"questions: {len(todo)} to ask, {args.concurrency} at a time")
    log = open(res_path, "a", encoding="utf-8")

    async def one(q):
        asker = args.asker or q["asker"]
        at = as_of_time(args.as_of or q["as_of"])
        async with sem:
            t = time.time()
            try:
                if args.api:
                    res = await asyncio.wait_for(stream_ask(args.api, q["question"], as_of=at, asker=asker,
                                           service_token=None if args.user_token else os.environ.get("QUERY_MASTER_SERVICE_TOKEN"),
                                           user_token=args.user_token, max_steps=args.max_steps), args.question_timeout)
                else:
                    key = (asker, at)
                    if key not in views:
                        views[key] = View(g, asker=asker, at=at)
                    res = await asyncio.wait_for(ask(q["question"], Tools(views[key]), client=client,
                                                     **({"max_steps": args.max_steps} if args.max_steps else {})),
                                                 args.question_timeout)
            except asyncio.TimeoutError:  # a stalled provider is a failed question, not a stalled run
                res = {"answer": "", "tool_calls": [], "usage": {}, "stopped": f"timeout after {args.question_timeout}s"}
            except Exception as e:  # a failed call is a failed question, not a failed run
                res = {"answer": "", "tool_calls": [], "usage": {}, "stopped": f"error: {type(e).__name__}: {e}"[:300]}
            secs = round(time.time() - t, 1)
            verdict = await grade(q, res["answer"], judge)
        row = {"id": q["id"], "pillar": q["pillar"], "category": q["category"], "difficulty": q["difficulty"],
               "question": q["question"], "as_of": at, "asker": asker, "gold_answer": q["gold_answer"],
               "expected_behavior": q["behavior"], **verdict, "answer": res["answer"], "stopped": res.get("stopped"),
               "tool_calls": res.get("tool_calls"), "usage": res.get("usage"), "seconds": secs, "model": res.get("model"),
               "paged": res.get("paged"), "compactions": res.get("compactions"),
               "calls": len(res.get("tool_calls") or []),
               "tokens": sum(int((res.get("usage") or {}).get(k) or 0) for k in ("prompt_tokens", "completion_tokens")),
               **{k: res[k] for k in ("question_plan", "budget", "notes", "evidence") if res.get(k) is not None}}
        log.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")  # kept at once: a stopped run loses nothing
        log.flush()
        mark = {True: "PASS", False: "FAIL", None: "----"}[verdict["pass"]]
        print(f"  {mark}  {row['id']:<9} {str(row['pillar'] or row['category']):<14} {verdict['method']:<6} "
              f"{len(row['tool_calls'] or [])} calls {secs:>5}s  {q['question'][:64]}", flush=True)
        return row

    try:
        new = await asyncio.gather(*(one(q) for q in todo))
    finally:
        log.close()
        for c in {id(client): client, id(judge): judge}.values():
            if hasattr(c, "aclose"):
                await c.aclose()

    # ---- the folder's results: the latest row per question, the questions of this set first and in its order
    latest = {**earlier, **{r["id"]: r for r in new}}
    order = [q["id"] for q in load(args.questions)[0]]
    rows = [latest[i] for i in order if i in latest] + [r for i, r in latest.items() if i not in set(order)]
    tmp = res_path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, res_path)
    summary = summarise(rows, run_id, {"asked_now": len(new), "kept_from_earlier": len(rows) - len(new)})
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    this = [latest[q["id"]] for q in qs if q["id"] in latest]
    if len(this) != len(rows):
        print(f"\nthis selection: {sum(r['pass'] is True for r in this)} of {len(this)} passed")
    print(f"\n{summary['passed']} of {summary['graded']} graded questions passed ({summary['questions']} in {res_path.name}, "
          f"{len(new)} asked now)")
    for name in ("by_pillar", "by_method"):
        print(f"{name}: {summary[name]}")
    print(f"results: {res_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--questions", default=str(HERE / "eval" / "kafka_questions.json"),
                    help="a question set: .jsonl in the shared shape, or this kit's .json")
    ap.add_argument("--graph", default=None, help="graph.sqlite (default: the set's, else ekg-eval-out/apache-kafka)")
    ap.add_argument("--tenant", default=None, help="one tenant of the graph file (default: everything in it)")
    ap.add_argument("--asker", default=None, help="ask every question as this person (name, email or login) or 'public' "
                                                   "(default: each question's own asker, else public)")
    ap.add_argument("--out", default=None, help="default eval/results/<questions file stem>")
    ap.add_argument("--fresh", action="store_true", help="forget what --out already holds and ask everything again")
    ap.add_argument("--as-of", default=None, help="override every question's as_of")
    ap.add_argument("--ids", default=None)
    ap.add_argument("--pillars", default=None)
    ap.add_argument("--categories", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=8, help="questions asked at the same time")
    ap.add_argument("--max-steps", type=int, default=None,
                    help="the agent's turns at most (default: none passed; the agent decides how much to look and keeps "
                         "only its own runaway brakes)")
    ap.add_argument("--only-failed", default=None, help="an earlier results.jsonl: ask only what did not pass there")
    ap.add_argument("--question-timeout", type=int, default=900, help="seconds one question may take")
    ap.add_argument("--api", default=None, help="a running query-master, e.g. http://127.0.0.1:7111: ask through its endpoint")
    ap.add_argument("--user-token", default=None, help="with --api: a signed-in user's token (else QUERY_MASTER_SERVICE_TOKEN)")
    a = ap.parse_args()
    a.out = a.out or str(HERE / "eval" / "results" / Path(a.questions).stem)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
