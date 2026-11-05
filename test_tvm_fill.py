"""Behaviour tests for the TVM-shaped fixtures (fixtures/tvm, see make_tvm.py) and for the LLM fill stage.

The fill stage runs with a stand-in for the model that returns fixed answers, so it costs nothing and
needs no key: it checks the writer (records, origin links, people links, schema checks), not the model.

    python -I ekg-eval-data/fixtures/make_tvm.py   (once, or after changing it)
    python -I ekg-eval-data/test_tvm_fill.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters import common  # noqa: E402
from ekg_adapters.fill import Writer, settle  # noqa: E402
from ekg_adapters.fill_tasks import Item, Task, View, doc_knowledge, knowledge  # noqa: E402
from ekg_adapters.pipeline import run  # noqa: E402
from test_adapters import G  # noqa: E402

FIXTURES = HERE / "fixtures" / "tvm"
QM = HERE.parent / "query-master"


class FakeResponses:
    """Answers the extract call with items quoted from the task's own sources, and accepts them all."""

    def __init__(self, items_for):
        self.items_for = items_for

    async def ask(self, task_id, prompt, name, schema, **kw):
        if name.endswith("_extract"):
            return {"items": self.items_for}
        n = len(self.items_for)
        return {"verdicts": [{"item": f"K{i + 1}", "verdict": "accept", "reason": "test"} for i in range(n)]}


def tvm_checks(check):
    with tempfile.TemporaryDirectory() as tmp:
        report = run(FIXTURES, QM, Path(tmp), log=lambda *_: None)
        g = G(Path(tmp))
    t = report["totals"]
    check("tvm: every record and link validates", t["records"] == t["records_valid"] and t["links"] == t["links_valid"])
    check("tvm: the preset applies (branch main, RFC decisions, GitHub issues)",
          report["settings"]["default_branch"] == "main" and report["settings"]["decision_prefix"] == "RFC"
          and report["settings"]["issue_tracker"] == "github")
    check("tvm: every present source ran", all(a["status"] == "ran" for a in report["adapters"].values())
          and set(report["adapters"]) == {"issues", "git", "code", "mail", "forum", "docs"})
    check("tvm: the bad JSONL line and the broken forum file are reported, not fatal",
          any("not valid JSON" in p for p in report["adapters"]["git"]["problems"])
          and any("unreadable" in p for p in report["adapters"]["forum"]["problems"]))
    notes = report["notes"]
    check("tvm: bad rows are skipped with a note (issue with no number, commit with no sha)",
          notes.get("issues: row without a number skipped") == 1 and notes.get("git: unreadable commit row skipped"))
    check("tvm: git made the repository record itself (no github/ folder)",
          notes.get("git: repository record made from the manifest (no github/ folder)") == 1)

    i101, i99 = g.one("Ticket", identifier="apache/tvm#101"), g.one("Ticket", identifier="apache/tvm#99")
    pr102 = g.one("PullRequest", number=102)
    c_fix = g.native("Commit", "git", "a" * 40)
    c_tir = g.native("Commit", "git", "b" * 40)
    check("tvm: issue 101 LINKS_TO PR 102 (linkedPullRequests)", g.linked("LINKS_TO", i101, pr102))
    check("tvm: 'Fixes #101' in the commit IMPLEMENTS issue 101", g.linked("IMPLEMENTS", c_fix, i101))
    check("tvm: '(#102)' in the commit subject: PR 102 MERGED_AS the commit", g.linked("MERGED_AS", pr102, c_fix))
    check("tvm: 'Part of #99' resolves to the issue, not a PR", g.linked("MENTIONS", c_tir, i99))
    closed = [lk for lk in g.links if lk["type"] == "IN_STATE" and lk["a"] == i101["id"]
              and g.by_id[lk["b"]]["props"]["name"] == "closed"]
    check("tvm: issue 101 has a dated closed state", closed and closed[0]["props"].get("valid_from", "").startswith("2026-09-20"))

    mails = [r for r in g.recs if r["type"] == "EmailMessage"]
    check("tvm: the duplicate message is kept once, the one with no Message-ID gets a stable id",
          len(mails) == 6 and notes.get("mail: no Message-ID (stable id generated)") == 1)
    check("tvm: the list address comes from the folder name when there is no List-Id",
          g.one("MailingList")["props"].get("address", "").startswith("dev"))
    notice = next(m for m in mails if m["props"].get("subject", "").startswith("[PR]"))
    check("tvm: the git@apache.org notice ANNOUNCES PR 102", g.linked("ANNOUNCES", notice, pr102))
    check("tvm: CI mail and other-repo notices are counted, not linked",
          notes.get("mail: CI notifications (noise, not linked)") == 1
          and notes.get("mail: code-host notifications for another repo, not linked") == 1)
    rfc = next(m for m in mails if m["props"].get("subject", "").startswith("[DISCUSS]"))
    check("tvm: '#101' in a mail body MENTIONS the issue", g.linked("MENTIONS", rfc, i101))
    check("tvm: mail dates in ISO form are read, not approximated",
          not notes.get("mail: no Date header (archive receive time used, precision approximate)"))

    topic = g.native("Thread", "forum", "2001")
    posts = [r for r in g.recs if r["type"] == "Comment" and r["props"]["source_tool"] == "forum"]
    check("tvm: the forum topic is a discussion Thread with its category", topic["props"]["kind"] == "discussion"
          and topic["props"]["title"].startswith("[Development]"))
    check("tvm: forum posts are IN the topic, the reply REPLY_TO the first post",
          len(posts) == 2 and all(g.linked("IN", p, topic) for p in posts)
          and any(lk["type"] == "REPLY_TO" and lk["a"] in {p["id"] for p in posts} for lk in g.links))
    check("tvm: a forum post LINKS_TO PR 102", any(g.linked("LINKS_TO", p, pr102) for p in posts))

    pass_py = g.one("File", path="python/tvm/relax/pass.py")
    check("tvm: the code tree File and the git File are one record",
          g.linked("CHANGED_IN", pass_py, c_fix) or any(lk["a"] == c_fix["id"] and lk["b"] == pass_py["id"] for lk in g.links))
    syms = {r["props"]["qualified_name"]: r for r in g.recs if r["type"] == "CodeSymbol"}
    check("tvm: Python classes, methods and functions are CodeSymbols DEFINED_IN their file",
          {"python.tvm.relax.pass.RelaxPass", "python.tvm.relax.pass.RelaxPass.run", "python.tvm.relax.pass.make_pass"} <= set(syms)
          and g.linked("DEFINED_IN", syms["python.tvm.relax.pass.make_pass"], pass_py))
    check("tvm: 'import tvm.tir.shape' resolves under the python/ source root",
          g.linked("IMPORTS", syms["python.tvm.relax.pass"], syms["python.tvm.tir.shape"]))

    roadmap = g.one("File", name="roadmap.xlsx")
    pieces = [r for r in g.recs if r["type"] == "Piece" and g.linked("PART_OF", r, roadmap)]
    check("tvm: the spreadsheet is a File with one Piece per sheet, which MENTIONS issue 99",
          len(pieces) == 1 and g.linked("MENTIONS", pieces[0], i99))

    bob = g.one("Person", full_name="Bob Kumar")
    tools = {g.by_id[lk["b"]]["props"]["source_tool"] for lk in g.links if lk["type"] == "HAS_ALIAS" and lk["a"] == bob["id"]}
    accts = {g.by_id[lk["b"]]["props"]["source_tool"]: lk["props"]["verified"] for lk in g.links
             if lk["type"] == "HAS_ACCOUNT" and lk["a"] == bob["id"]}
    check("tvm: with no directory, Bob is inferred from his commit and mail address (not verified)",
          bob["props"]["person_source"] == "inferred_from_activity" and tools == {"email"})
    check("tvm: Bob's forum account joins by full name, his GitHub account by the same login, both unverified",
          accts == {"forum": False, "github": False})


def fill_checks(check):
    """The writer, on the acme fixtures' Slack thread (it has authors with Slack accounts)."""
    with tempfile.TemporaryDirectory() as tmp:
        run(HERE / "fixtures" / "acme", QM, Path(tmp), log=lambda *_: None)
        records = [json.loads(line) for line in open(Path(tmp) / "records.jsonl", encoding="utf-8")]
        links = [json.loads(line) for line in open(Path(tmp) / "links.jsonl", encoding="utf-8")]
    v = View(records, links)
    tasks, skipped = knowledge(v)
    check("fill: short threads are skipped, not asked", skipped.get("too short to hold knowledge"))
    # the fixtures' threads are too short for the builder, so the test makes the task from two Slack messages
    msgs = [r for r in records if r["type"] == "Message" and r["props"].get("kind") == "message"
            and len(r["props"].get("text") or "") > 40 and r["id"] in {lk["a"] for lk in links if lk["type"] == "AUTHORED_BY"}][:2]
    sources = [Item(f"S{i + 1}", m["id"], f"Message by {v.author(m['id'])}", m["props"]["text"], "Message")
               for i, m in enumerate(msgs)]
    t = Task("knowledge", msgs[0]["id"], "Chat thread: test", "", sources, [], {"about": None, "decision": None})
    s1, s2 = t.sources[0], t.sources[1]
    q1, q2 = s1.text[:60].strip(), s2.text[:40].strip()
    items = [
        {"type": "decision", "kind": "none", "title": "Refund retries", "statement": "Refunds are retried once.",
         "quote": q1, "source": "S1", "rationale": "avoid double refunds", "strength": "none", "owner": "none"},
        {"type": "action_item", "kind": "none", "title": "Fix it", "statement": "Bob fixes the retry.",
         "quote": q2, "source": "S2", "rationale": "", "strength": "none", "owner": "S2"},
        {"type": "guideline", "kind": "none", "title": "Idempotency", "statement": "Refund calls must be idempotent.",
         "quote": q1, "source": "S1", "rationale": "", "strength": "must", "owner": "none"},
        {"type": "knowledge", "kind": "gotcha", "title": "Retries", "statement": "Retries can double refund.",
         "quote": q1, "source": "S1", "rationale": "", "strength": "none", "owner": "none"},
        {"type": "question", "kind": "none", "title": "Made up", "statement": "Not in the text.",
         "quote": "this sentence is not in any message", "source": "S1", "rationale": "", "strength": "none", "owner": "none"},
    ]
    e = asyncio.run(settle(t, FakeResponses(items)))
    check("fill: a quote not in its source is dropped by the code check, the rest are kept",
          e["status"] == "accepted" and len(e["items"]) == 4 and e["dropped"].get("check: quote not in its source") == 1)
    w = Writer(records, links, "2026-11-01T00:00:00Z", "test-model")
    why = w.apply(t, e)
    with tempfile.TemporaryDirectory() as tmp:
        stats = w.write(Path(tmp))
        new = [json.loads(line) for line in open(Path(tmp) / "records.jsonl", encoding="utf-8")]
        new_links = [json.loads(line) for line in open(Path(tmp) / "links.jsonl", encoding="utf-8")]
    check("fill: every item was written", why is None)
    by_type = {r["type"]: r for r in new}
    check("fill: Decision, ActionItem, Guideline and Knowledge records, plus the pass's DerivationRun",
          {"Decision", "ActionItem", "Guideline", "Knowledge", "DerivationRun"} <= set(by_type))
    check("fill: the decision keeps its rationale and date, the guideline its strength",
          by_type["Decision"]["props"].get("rationale") == "avoid double refunds" and by_type["Decision"]["props"].get("decided_at")
          and by_type["Guideline"]["props"].get("strength") == "must")
    author1 = w.author.get(s1.id)
    kinds = {(lk["type"], lk["b"]) for lk in new_links}
    check("fill: the decision is DECIDED_BY, the knowledge AUTHORED_BY, the author of its message",
          ("DECIDED_BY", author1) in kinds and ("AUTHORED_BY", author1) in kinds)
    check("fill: the action item is ASSIGNED_TO the owner's account", ("ASSIGNED_TO", w.author.get(s2.id)) in kinds)
    check("fill: every new record and link validates, with method llm_judge and evidence",
          all(common._vrec(r).ok for r in new) and all(lk["props"]["method"] == "llm_judge" and lk["props"].get("evidence")
                                                        for lk in new_links))
    check("fill: people links are counted in the report", stats["people_links"].get("DECIDED_BY: linked") == 1)
    # documents: the builder only reads stored documents and recently edited pages
    dt, _ = doc_knowledge(v)
    check("fill: document tasks have their own kind", all(x.kind == "doc_knowledge" for x in dt))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not FIXTURES.exists():
        print("fixtures/tvm is missing: run python -I ekg-eval-data/fixtures/make_tvm.py first")
        return 1
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    tvm_checks(check)
    fill_checks(check)
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
