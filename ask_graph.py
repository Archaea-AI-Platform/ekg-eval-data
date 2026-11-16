"""Ask the graph one question (LLM tool calling), or call one tool directly (no LLM).

    ./with_env.sh .venv/Scripts/python.exe -I ekg-eval-data/ask_graph.py --graph <graph.sqlite> "<a question>"
    ... ask_graph.py --graph <graph.sqlite> --profile                      # what the graph holds, read from its data
    ... ask_graph.py --graph <graph.sqlite> --tool overview --args '{"id": "<a key or name>"}'
    ... ask_graph.py --graph <graph.sqlite> --tool read --args '{"id": "<a document>", "outline": true}'
    ... ask_graph.py --tool experts --args '{"name": "<a folder path>"}' --as "<a person>"
    ... ask_graph.py --list-tools

--as names the asker (a person's name, email or login; default public): every tool
shows only what that person may see. The LLM comes from the environment
(EKG_AGENT_BACKEND / AGENTIC_LLM_BACKEND, default openrouter; OPENROUTER_API_KEYS;
EKG_AGENT_MODEL or EKG_LLM_MODEL); ../glimmer.env through ../with_env.sh sets them.
--tenant reads one tenant of a combined graph file.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "query-master"))

from app.ekg.query import Graph, SPECS, Tools, View, ask, call  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question", nargs="?")
    ap.add_argument("--graph", default=str(HERE.parent / "ekg-eval-out" / "apache-kafka" / "graph.sqlite"))
    ap.add_argument("--tenant", default=None, help="one tenant of the graph file (default: everything in it)")
    ap.add_argument("--profile", action="store_true", help="print what the graph holds (the profile the agent is given)")
    ap.add_argument("--as", dest="asker", default="public")
    ap.add_argument("--as-of", default=None, help="the 'now' the tools use (default: today)")
    ap.add_argument("--tool", default=None)
    ap.add_argument("--args", default="{}")
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--list-tools", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.list_tools:
        for name, (desc, props, req) in SPECS.items():
            print(f"{name:<13} {desc}\n{'':<13} args: {', '.join(p + ('*' if p in req else '') for p in props) or '-'}")
        return 0
    if not a.tool and not a.question and not a.profile:
        ap.error("give a question, --tool or --profile")
    g = Graph.from_sqlite(a.graph, tenant=a.tenant)
    tools = Tools(View(g, asker=a.asker, at=a.as_of))
    if a.profile:
        print(tools.profile_text())
        return 0
    if a.tool:
        print(json.dumps(call(tools, a.tool, json.loads(a.args)), indent=1, ensure_ascii=False, default=str))
        return 0
    res = asyncio.run(ask(a.question, tools, max_steps=a.max_steps,
                          on_event=lambda e: print(f"  -> {e['tool']} {json.dumps(e.get('args'))}")
                          if e["type"] == "tool_start" else None))
    print(f"\n{res['answer']}\n\n[{res['steps']} tool calls, {res['usage']}, model {res['model']}, {res['stopped']}, "
          f"{res.get('paged', 0)} results paged, {res.get('compactions', 0)} compactions]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
