"""A local showcase of the graph agent: pick a question from the evaluation set (or type one), watch the
agent's tool calls live, and see its answer beside the gold answer and the grade.

    export LLM_BACKEND=openrouter OPENROUTER_API_KEYS=<key> EKG_LLM_MODEL=mistralai/devstral-2512
    query-master/.venv/Scripts/python.exe -I ekg-eval-data/demo_app.py
    -> http://127.0.0.1:8765

It runs query-master's own query layer (app.ekg.query: the per-person view, the tools and the agent loop,
the same code behind query-master's /api/v1/ekg/internal/query endpoints) in this process, on the built
graph. It changes nothing in query-master and listens on localhost only. Results of the last run_eval.py run
are shown next to each question, so the whole evaluation can be browsed without spending on new calls.

With --api http://127.0.0.1:7111 the questions go to a running query-master instead (its graph agent's
streamed search endpoint, on query-master's own store), the way the web app asks; QUERY_MASTER_SERVICE_TOKEN
or --user-token authenticates.

Options: --graph (default ekg-eval-out/apache-kafka/graph.sqlite), --questions (default
eval/kafka_shared_queries.jsonl), --results (default eval/results/<questions stem>/results.jsonl), --port.
"""
import argparse
import asyncio
import json
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "query-master"))
sys.path.insert(0, str(HERE))

from app.ekg.query import Graph, Tools, View, ask, default_client  # noqa: E402
from eval_grading import as_of_time, grade, load  # noqa: E402
from qm_client import stream_ask  # noqa: E402

STATE: dict = {}


def view_for(asker: str, at: str | None) -> View:
    key = (asker or "public", at)
    with STATE["lock"]:
        if key not in STATE["views"]:
            STATE["views"][key] = View(STATE["graph"], asker=asker or "public", at=at)
        return STATE["views"][key]


def run_question(params: dict, out: "queue.Queue"):
    """Runs in a worker thread: the agent, then the grade; every step goes onto `out`."""
    q = STATE["by_id"].get(params.get("id") or "")
    question = (params.get("q") or (q or {}).get("question") or "").strip()
    at = as_of_time(params.get("as_of") or (q or {}).get("as_of"))
    asker = params.get("asker") or (q or {}).get("asker") or "public"

    async def go():
        client = default_client()
        try:
            if STATE.get("api"):
                out.put({"type": "start", "question": question, "as_of": at, "asker": asker,
                         "model": f"query-master at {STATE['api']}"})

                def relay(ev):  # query-master's event names -> this page's
                    if ev.get("type") == "tool_start":
                        out.put({"type": "tool_start", "tool": ev.get("tool"), "args": ev.get("args")})
                    elif ev.get("type") == "tool_done":
                        out.put({"type": "tool_done", "tool": ev.get("tool"), "ms": ev.get("duration_ms"),
                                 "summary": ev.get("result_summary")})
                res = await stream_ask(STATE["api"], question, as_of=at, asker=asker, service_token=STATE.get("token"),
                                       user_token=STATE.get("user_token"), max_steps=STATE["max_steps"], on_event=relay)
            else:
                out.put({"type": "start", "question": question, "as_of": at, "asker": asker, "model": getattr(client, "model", None)})
                res = await ask(question, Tools(view_for(asker, at)), client=client, on_event=out.put,
                                max_steps=STATE["max_steps"])
            out.put({"type": "answer", "answer": res["answer"], "steps": res["steps"], "usage": res["usage"],
                     "stopped": res["stopped"], "tool_calls": res["tool_calls"]})
            if q and question == q["question"]:
                out.put({"type": "grade", **(await grade(q, res["answer"], client))})
        finally:
            if hasattr(client, "aclose"):
                await client.aclose()

    try:
        asyncio.run(go())
    except Exception as e:  # shown in the page; the server keeps running
        out.put({"type": "error", "error": f"{type(e).__name__}: {e}"[:400]})
    out.put({"type": "done"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  " + fmt % args + "\n")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/info":
            g = STATE["graph"]
            self._json({"graph": STATE["graph_path"], "records": len(g.rec) if g else None, "links": len(g.links) if g else None,
                        "via": f"query-master at {STATE['api']}" if STATE.get("api") else "in process",
                        "questions": len(STATE["questions"]), "questions_file": STATE["questions_path"],
                        "summary": STATE.get("summary"), "types": dict(sorted(((t, len(v)) for t, v in g.by_type.items()),
                                                                             key=lambda kv: -kv[1])[:14]) if g else {}})
        elif url.path == "/api/queries":
            res = STATE["results"]
            self._json([{**{k: q[k] for k in ("id", "pillar", "category", "difficulty", "question", "as_of", "gold_answer",
                                             "variants", "checklist", "must_not_say", "grading", "behavior")},
                         "last": ({k: res[q["id"]].get(k) for k in ("pass", "method", "answer", "missing", "said_forbidden",
                                                                     "tool_calls", "seconds", "judge", "matched")}
                                  if q["id"] in res else None)} for q in STATE["questions"]])
        elif url.path == "/api/ask":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            out: queue.Queue = queue.Queue()
            threading.Thread(target=run_question, args=(params, out), daemon=True).start()
            while True:
                try:
                    ev = out.get(timeout=15)
                except queue.Empty:
                    ev = {"type": "ping"}
                try:
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if ev["type"] == "done":
                    return
        else:
            self._json({"error": "not found"}, 404)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", default=str(HERE.parent / "ekg-eval-out" / "apache-kafka" / "graph.sqlite"))
    ap.add_argument("--questions", default=str(HERE / "eval" / "kafka_shared_queries.jsonl"))
    ap.add_argument("--results", default=None)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--api", default=None, help="a running query-master, e.g. http://127.0.0.1:7111")
    ap.add_argument("--user-token", default=None, help="with --api: a signed-in user's token (else QUERY_MASTER_SERVICE_TOKEN)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    results_path = Path(a.results or HERE / "eval" / "results" / Path(a.questions).stem / "results.jsonl")
    t = time.time()
    import os
    STATE.update(graph=None if a.api else Graph.from_sqlite(a.graph), graph_path=f"query-master store via {a.api}" if a.api else a.graph,
                 views={}, lock=threading.Lock(), max_steps=a.max_steps, questions_path=a.questions, api=a.api,
                 token=os.environ.get("QUERY_MASTER_SERVICE_TOKEN"), user_token=a.user_token)
    STATE["questions"], _ = load(a.questions)
    STATE["by_id"] = {q["id"]: q for q in STATE["questions"]}
    STATE["results"] = {}
    if results_path.exists():
        STATE["results"] = {r["id"]: r for r in map(json.loads, results_path.read_text(encoding="utf-8").splitlines()) if r.get("id")}
        sp = results_path.with_name("summary.json")
        STATE["summary"] = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
    source = f"asking query-master at {a.api}" if a.api else \
        f"graph loaded in {time.time() - t:.1f}s: {len(STATE['graph'].rec):,} records"
    print(f"{source}; {len(STATE['questions'])} questions, {len(STATE['results'])} with results")
    print(f"open http://127.0.0.1:{a.port}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Graph agent demo</title>
<style>
:root{--bg:#f6f5f1;--panel:#fff;--fg:#1c1e1a;--fg2:#5d6259;--line:#dedbd1;--accent:#1f5f5b;--ok:#2d7a4a;--bad:#b23a2a;--warn:#a86b00;--chip:#ecebe4;--code:#f1efe8}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15171a;--panel:#1d2024;--fg:#e8e6df;--fg2:#a6aaa0;--line:#33373d;--accent:#7fc4bd;--ok:#7bcf98;--bad:#f08a7a;--warn:#f0b24a;--chip:#2a2e34;--code:#262a30}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 "Segoe UI",system-ui,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
header h1{font-size:18px;margin:0;font-weight:600}header .stats{color:var(--fg2);font-size:13px}
main{display:grid;grid-template-columns:minmax(300px,420px) 1fr;height:calc(100vh - 56px)}
@media (max-width:860px){main{grid-template-columns:1fr;height:auto}}
aside{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.filters{padding:10px 12px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
input[type=text],textarea,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);font:inherit}
.chips{display:flex;flex-wrap:wrap;gap:5px}.chip{border:1px solid var(--line);background:var(--chip);border-radius:999px;padding:1px 9px;font-size:12px;cursor:pointer;color:var(--fg2)}
.chip.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
#list{overflow:auto;flex:1}.q{padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer;display:flex;gap:9px}
.q:hover,.q.sel{background:var(--panel)}.q .t{font-size:13.5px}.q .m{font-size:11.5px;color:var(--fg2)}
.dot{flex:none;width:9px;height:9px;border-radius:50%;margin-top:6px;background:var(--line)}.dot.p{background:var(--ok)}.dot.f{background:var(--bad)}
section{overflow:auto;padding:18px 22px;min-width:0}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 6px}.meta{color:var(--fg2);font-size:12.5px}
button{background:var(--accent);color:var(--bg);border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}button.sec{background:var(--chip);color:var(--fg)}
button:disabled{opacity:.5;cursor:default}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.trace{font:12.5px/1.45 Consolas,monospace;margin:0;padding:0;list-style:none}.trace li{padding:5px 0;border-top:1px dashed var(--line);overflow-wrap:anywhere}
.trace b{color:var(--accent)}.trace .s{color:var(--fg2)}
.ans{white-space:normal;overflow-wrap:anywhere}.ans code{background:var(--code);padding:0 4px;border-radius:3px;font-size:.9em}
.badge{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}.badge.p{background:var(--ok)}.badge.f{background:var(--bad)}.badge.n{background:var(--warn)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media (max-width:1100px){.grid2{grid-template-columns:1fr}}
ul.facts{margin:6px 0 0;padding-left:18px;font-size:13.5px}.miss{color:var(--bad)}.met{color:var(--ok)}
.muted{color:var(--fg2)}.small{font-size:12.5px}
</style></head><body>
<header><h1>Graph agent: live evaluation</h1><span class="stats" id="stats">loading…</span></header>
<main>
<aside>
 <div class="filters">
  <input type="text" id="search" placeholder="Filter questions…">
  <div class="chips" id="pillars"></div>
  <div class="chips" id="status"></div>
 </div>
 <div id="list"></div>
</aside>
<section>
 <div class="card">
  <h2>Ask anything</h2>
  <textarea id="free" rows="2" placeholder="e.g. Which pull request fixed KAFKA-20901, and who approved it?"></textarea>
  <div class="row"><label class="small muted">as of <input type="text" id="asof" value="2026-10-29" style="width:120px"></label>
  <label class="small muted">asked by <input type="text" id="asker" value="public" style="width:150px"></label>
  <button id="askfree">Ask</button></div>
 </div>
 <div id="detail"></div>
</section>
</main>
<script>
const $=s=>document.querySelector(s);let Q=[],sel=null,pill=new Set(),stat="all",es=null;
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function md(s){let h=esc(s);h=h.replace(/`([^`]+)`/g,"<code>$1</code>").replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>");
 return h.split(/\n/).map(l=>/^\s*[-*] /.test(l)?"&bull; "+l.replace(/^\s*[-*] /,""):l).join("<br>")}
function badge(p){return p===true?'<span class="badge p">PASS</span>':p===false?'<span class="badge f">FAIL</span>':'<span class="badge n">not graded</span>'}
async function init(){
 const info=await (await fetch("/api/info")).json();const s=info.summary;
 $("#stats").textContent=(info.records!=null?`${info.records.toLocaleString()} records · ${info.links.toLocaleString()} links · `:`${info.via} · `)+`${info.questions} questions`+
  (s?` · last run: ${s.passed}/${s.graded} passed (${s.model})`:" · no evaluation run yet");
 Q=await (await fetch("/api/queries")).json();
 const ps=[...new Set(Q.map(q=>q.pillar||q.category))].sort();
 $("#pillars").innerHTML=ps.map(p=>`<span class="chip" data-p="${esc(p)}">${esc(p)}</span>`).join("");
 $("#status").innerHTML=["all","passed","failed","not run"].map(x=>`<span class="chip${x==="all"?" on":""}" data-s="${x}">${x}</span>`).join("");
 document.querySelectorAll("[data-p]").forEach(c=>c.onclick=()=>{c.classList.toggle("on");pill.has(c.dataset.p)?pill.delete(c.dataset.p):pill.add(c.dataset.p);list()});
 document.querySelectorAll("[data-s]").forEach(c=>c.onclick=()=>{document.querySelectorAll("[data-s]").forEach(x=>x.classList.remove("on"));c.classList.add("on");stat=c.dataset.s;list()});
 $("#search").oninput=list;list();if(Q.length)show(Q[0].id);
}
function list(){
 const t=$("#search").value.toLowerCase();
 const rows=Q.filter(q=>(!pill.size||pill.has(q.pillar||q.category))&&(!t||(q.id+" "+q.question).toLowerCase().includes(t))&&
  (stat==="all"||(stat==="passed"&&q.last?.pass===true)||(stat==="failed"&&q.last?.pass===false)||(stat==="not run"&&!q.last)));
 $("#list").innerHTML=rows.map(q=>`<div class="q${q.id===sel?" sel":""}" data-id="${esc(q.id)}"><span class="dot ${q.last?.pass===true?"p":q.last?.pass===false?"f":""}"></span>
  <div><div class="t">${esc(q.question)}</div><div class="m">${esc(q.id)} · ${esc(q.pillar||q.category)} · ${esc(q.category)}${q.difficulty?" · difficulty "+q.difficulty:""}</div></div></div>`).join("")||'<p class="muted" style="padding:12px">No questions match.</p>';
 document.querySelectorAll(".q").forEach(e=>e.onclick=()=>show(e.dataset.id));
}
function gold(q){
 const facts=(q.checklist||[]).map(c=>`<li>${esc(c.fact)}${c.required===false?' <span class="muted">(optional)</span>':""}</li>`).join("");
 return `<div class="card"><h2>Gold answer</h2><div class="ans">${md(q.gold_answer||"")}</div>
  ${q.variants?.length?`<div class="meta" style="margin-top:6px">accepted: ${q.variants.map(esc).join(" · ")}</div>`:""}
  ${facts?`<div class="meta" style="margin-top:8px">facts the answer must state</div><ul class="facts">${facts}</ul>`:""}
  ${q.must_not_say?.length?`<div class="meta" style="margin-top:6px">must not say: ${q.must_not_say.map(esc).join(" · ")}</div>`:""}
  <div class="meta" style="margin-top:6px">graded by ${esc(q.grading)} · expected: ${esc(q.behavior)}</div></div>`}
function gradeHtml(g){
 if(!g)return"";const miss=(g.missing||[]).map(m=>`<li class="miss">${esc(typeof m==="string"?m:JSON.stringify(m))}</li>`).join("");
 return `<div class="row">${badge(g.pass)} <span class="meta">${esc(g.method||"")}${g.judge?.reason?" · "+esc(g.judge.reason):""}</span></div>
  ${miss?`<ul class="facts">${miss}</ul>`:""}${g.said_forbidden?.length?`<div class="miss small">said: ${g.said_forbidden.map(esc).join(", ")}</div>`:""}`}
function traceHtml(calls){return `<ul class="trace">${(calls||[]).map(c=>`<li><b>${esc(c.tool)}</b> ${esc(JSON.stringify(c.args))}<br><span class="s">→ ${esc(c.summary)} · ${c.ms} ms</span></li>`).join("")}</ul>`}
function show(id){
 sel=id;list();const q=Q.find(x=>x.id===id);const l=q.last;
 $("#detail").innerHTML=`<div class="card"><div class="meta">${esc(q.id)} · ${esc(q.pillar||"")} · ${esc(q.category)} · as of ${esc(q.as_of||"now")}</div>
  <h2 style="font-size:17px;margin-top:4px">${esc(q.question)}</h2>
  <div class="row"><button id="run">Run live</button>${l?'<span class="meta">or read the last evaluation run below</span>':""}</div></div>
  <div class="grid2"><div><div class="card" id="live" style="display:none"><h2>Live run</h2><div id="livebody"></div></div>
  ${l?`<div class="card"><h2>Last evaluation run ${badge(l.pass)}</h2><div class="ans">${md(l.answer||"(no answer)")}</div>${gradeHtml(l)}
   <details style="margin-top:8px"><summary class="meta">${(l.tool_calls||[]).length} tool calls · ${l.seconds}s</summary>${traceHtml(l.tool_calls)}</details></div>`:""}</div>
  <div>${gold(q)}</div></div>`;
 $("#run").onclick=()=>run({id:q.id});
}
$("#askfree").onclick=()=>{const t=$("#free").value.trim();if(!t)return;sel=null;list();
 $("#detail").innerHTML=`<div class="card" id="live"><h2>Live run</h2><div id="livebody"></div></div>`;run({q:t,as_of:$("#asof").value,asker:$("#asker").value})};
function run(p){
 if(es)es.close();$("#live").style.display="";const b=$("#livebody");
 b.innerHTML=`<div class="meta" id="lm">starting…</div><ul class="trace" id="lt"></ul><div id="la"></div>`;
 document.querySelectorAll("#run,#askfree").forEach(x=>x.disabled=true);
 es=new EventSource("/api/ask?"+new URLSearchParams(p));const t0=Date.now();
 es.onmessage=m=>{const e=JSON.parse(m.data);
  if(e.type==="start")$("#lm").textContent=`asked by ${e.asker} · as of ${e.as_of||"now"} · ${e.model}`;
  else if(e.type==="tool_start")$("#lt").insertAdjacentHTML("beforeend",`<li><b>${esc(e.tool)}</b> ${esc(JSON.stringify(e.args))}<br><span class="s">…</span></li>`);
  else if(e.type==="tool_done"){const li=[...document.querySelectorAll("#lt li")].reverse().find(x=>x.querySelector("b").textContent===e.tool&&x.querySelector(".s").textContent==="…");if(li)li.querySelector(".s").textContent=`→ ${e.summary} · ${e.ms} ms`}
  else if(e.type==="answer")$("#la").innerHTML=`<div class="ans" style="margin-top:10px">${md(e.answer||"(no answer)")}</div><div class="meta" style="margin-top:6px">${e.steps} tool calls · ${((Date.now()-t0)/1000).toFixed(1)}s · ${e.usage?.prompt_tokens||0} prompt tokens</div>`;
  else if(e.type==="grade")$("#la").insertAdjacentHTML("beforeend",gradeHtml(e));
  else if(e.type==="error")$("#la").insertAdjacentHTML("beforeend",`<div class="miss">${esc(e.error)}</div>`);
  else if(e.type==="done"){es.close();document.querySelectorAll("#run,#askfree").forEach(x=>x.disabled=false)}};
 es.onerror=()=>{es.close();document.querySelectorAll("#run,#askfree").forEach(x=>x.disabled=false)};
}
init();
</script></body></html>"""

if __name__ == "__main__":
    sys.exit(main())
