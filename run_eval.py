"""Run a question set through the graph agent (LLM tool calling) and grade the answers.

Each question is asked as its `asker`, as of its `as_of` date (the tools see the graph as it stood at
the end of that day), and graded as eval_grading.py describes: acceptable variants for script-graded
questions, an LLM judge with the question's checklist for the rest, must_not_say always. Everything the
agent did is kept, so a failure can be read call by call.

    export LLM_BACKEND=openrouter OPENROUTER_API_KEYS=<key> EKG_LLM_MODEL=mistralai/devstral-2512
    query-master/.venv/Scripts/python.exe -I ekg-eval-data/run_eval.py --questions ekg-eval-data/eval/kafka_shared_queries.jsonl
    ... run_eval.py                                     # this kit's generated set (eval/kafka_questions.json)
    ... run_eval.py --questions ... --ids XPD-031,KNW-066 --pillars code,people --limit 10

With --api, every question goes to a running query-master over HTTP instead (the graph agent's
search endpoint, streamed, as the web app calls it), reading the graph from query-master's own store:

    QUERY_MASTER_SERVICE_TOKEN=<token> ... run_eval.py --questions ... --api http://127.0.0.1:7111
    ... run_eval.py --questions ... --api http://127.0.0.1:7111 --user-token <a signed-in user's token>

Writes <out>/results.jsonl (one row per question: answer, grade, tool calls, tokens), <out>/summary.json,
and prints a table by pillar and category. EKG_JUDGE_MODEL picks the judge model (default: the agent's).
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


def judge_client():
    from app.services.retrieval_engine.llm_tools import make_tool_client
    model = os.environ.get("EKG_JUDGE_MODEL")
    if not model:
        return default_client()
    key = (os.environ.get("OPENROUTER_API_KEYS") or os.environ.get("OPENROUTER_API_KEY") or "").split(",")[0].strip()
    return make_tool_client("openrouter", model=model, api_key=key)


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
    g = None
    if args.api:
        token = args.user_token or os.environ.get("QUERY_MASTER_SERVICE_TOKEN")
        print(f"asking query-master at {args.api} ({'as the signed-in user' if args.user_token else 'internal route'})")
        graph_path = f"{args.api} (query-master's store)"
    else:
        t0 = time.time()
        g = Graph.from_sqlite(graph_path)
        print(f"graph {graph_path}: {len(g.rec):,} records, {len(g.links):,} links ({time.time() - t0:.1f}s)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    views: dict = {}
    sem = asyncio.Semaphore(args.concurrency)
    client, judge = default_client(), judge_client()
    print(f"model: {'(query-master decides)' if args.api else getattr(client, 'model', '?')}  judge: {getattr(judge, 'model', '?')}  "
          f"questions: {len(qs)}")

    async def one(q):
        at = as_of_time(args.as_of or q["as_of"])
        key = (q["asker"], at)
        if g is not None and key not in views:
            views[key] = View(g, asker=q["asker"], at=at)
        async with sem:
            t = time.time()
            try:
                if args.api:
                    res = await asyncio.wait_for(stream_ask(args.api, q["question"], as_of=at, asker=q["asker"],
                                           service_token=None if args.user_token else os.environ.get("QUERY_MASTER_SERVICE_TOKEN"),
                                           user_token=args.user_token, max_steps=args.max_steps), args.question_timeout)
                else:
                    res = await ask(q["question"], Tools(views[key]), client=client, max_steps=args.max_steps)
            except asyncio.TimeoutError:  # a stalled provider is a failed question, not a stalled run
                res = {"answer": "", "tool_calls": [], "usage": {}, "stopped": f"timeout after {args.question_timeout}s"}
            except Exception as e:  # a failed call is a failed question, not a failed run
                res = {"answer": "", "tool_calls": [], "usage": {}, "stopped": f"error: {type(e).__name__}: {e}"[:300]}
            secs = round(time.time() - t, 1)
            verdict = await grade(q, res["answer"], judge)
        row = {"id": q["id"], "pillar": q["pillar"], "category": q["category"], "difficulty": q["difficulty"],
               "question": q["question"], "as_of": at, "asker": q["asker"], "gold_answer": q["gold_answer"],
               "expected_behavior": q["behavior"], **verdict, "answer": res["answer"], "stopped": res.get("stopped"),
               "tool_calls": res.get("tool_calls"), "usage": res.get("usage"), "seconds": secs, "model": res.get("model")}
        mark = {True: "PASS", False: "FAIL", None: "----"}[verdict["pass"]]
        print(f"  {mark}  {row['id']:<9} {str(row['pillar'] or row['category']):<14} {verdict['method']:<6} "
              f"{len(row['tool_calls'] or [])} calls {secs:>5}s  {q['question'][:64]}", flush=True)
        return row

    rows = await asyncio.gather(*(one(q) for q in qs))
    for c in {id(client): client, id(judge): judge}.values():
        if hasattr(c, "aclose"):
            await c.aclose()
    with open(out / "results.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    def table(key):
        t = collections.defaultdict(lambda: [0, 0])
        for r in rows:
            if r["pass"] is not None:
                t[str(r.get(key))][0] += bool(r["pass"])
                t[str(r.get(key))][1] += 1
        return {k: f"{v[0]}/{v[1]}" for k, v in sorted(t.items())}

    graded = [r for r in rows if r["pass"] is not None]
    summary = {"questions": len(rows), "graded": len(graded), "passed": sum(bool(r["pass"]) for r in graded),
               "by_pillar": table("pillar"), "by_category": table("category"), "by_method": table("method"),
               "by_difficulty": table("difficulty"),
               "tool_calls_per_question": round(sum(len(r["tool_calls"] or []) for r in rows) / max(1, len(rows)), 1),
               "tools_used": dict(collections.Counter(c["tool"] for r in rows for c in (r["tool_calls"] or [])).most_common()),
               "tokens": {k: sum((r["usage"] or {}).get(k, 0) for r in rows) for k in ("prompt_tokens", "completion_tokens")},
               "stopped": dict(collections.Counter(r["stopped"] for r in rows)),
               "model": next((r.get("model") for r in rows if r.get("model")), getattr(client, "model", None)),
               "judge": getattr(judge, "model", None), "via": "query-master API " + args.api if args.api else "in process",
               "graph": graph_path, "questions_file": str(args.questions)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\n{summary['passed']} of {summary['graded']} graded questions passed ({summary['questions']} asked)")
    for name in ("by_pillar", "by_method"):
        print(f"{name}: {summary[name]}")
    print(f"results: {out / 'results.jsonl'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--questions", default=str(HERE / "eval" / "kafka_questions.json"))
    ap.add_argument("--graph", default=None, help="graph.sqlite (default: the set's, else ekg-eval-out/apache-kafka)")
    ap.add_argument("--out", default=None, help="default eval/results/<questions file stem>")
    ap.add_argument("--as-of", default=None, help="override every question's as_of")
    ap.add_argument("--ids", default=None)
    ap.add_argument("--pillars", default=None)
    ap.add_argument("--categories", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--only-failed", default=None, help="an earlier results.jsonl: ask only what did not pass there")
    ap.add_argument("--question-timeout", type=int, default=900, help="seconds one question may take (with --api)")
    ap.add_argument("--api", default=None, help="a running query-master, e.g. http://127.0.0.1:7111: ask through its endpoint")
    ap.add_argument("--user-token", default=None, help="with --api: a signed-in user's token (else QUERY_MASTER_SERVICE_TOKEN)")
    a = ap.parse_args()
    a.out = a.out or str(HERE / "eval" / "results" / Path(a.questions).stem)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
