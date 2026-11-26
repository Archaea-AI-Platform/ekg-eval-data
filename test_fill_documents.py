"""Behaviour tests for the document side of the LLM stage: whole-document reading in windows, schema-driven
extraction, table column mappings, name resolution, confidence, and the cache under concurrency.

The model is a stand-in that returns fixed answers (as in test_tvm_fill.py): no key, no network, no cost.
The fixtures are built by hand in the shape the document adapters write: File / Document, ContentVersion
(VERSION_OF), DocSection (SECTION_OF), Piece (PART_OF a section, OF_VERSION the version; kind text | table).

    python -I ekg-eval-data/test_fill_documents.py
"""

import asyncio
import collections
import json
import random
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters import common, extract_schema, fill, fill_budget, fill_docs, llm  # noqa: E402
from ekg_adapters.common import Graph  # noqa: E402
from ekg_adapters.fill_tasks import BUILDERS, View, knowledge  # noqa: E402

QM = HERE.parent / "query-master"
TEMP = []  # folders the runs wrote, removed at the end
SCOPE = [["public"]]
FACTS = ("The payments service is owned by the Platform team. Bob Kumar decided on 2026-03-02 that refunds are "
         "retried once. Sam Lee is the person to ask about refunds. Kim Park is the expert on ledger exports. See ACME-404.")
DIRECTORY = [  # name, email, team, manager, title, start
    ("Alice Chen", "alice@acme.example", "Platform", "Bob Kumar", "Staff Engineer", "2021-04-01"),
    ("Bob Kumar", "bob@acme.example", "Platform", "", "Engineering Manager", "2019-01-15"),
    ("Carla Diaz", "carla@acme.example", "Data", "Bob Kumar", "Data Engineer", "2023-09-11"),
    ("", "nobody@acme.example", "Data", "Bob Kumar", "Contractor", "2024-01-01"),          # no name: skipped, counted
    ("Dev Patel", "dev@acme.example", "Data", "Sam Lee", "Analyst", "not a date"),          # manager is ambiguous
    ("Erin Shaw", "erin@acme.example", "Platform; Data", "Alice Chen", "Staff Engineer", "2022-02-02"),
    ("Femi Ade", "femi@acme.example", "Data", "Carla Diaz", "Data Engineer", "2025-06-30"),
]


# ---------------------------------------------------------------- fixtures

def new_graph():
    common.bind(QM)
    common.set_tenant("acme")
    return Graph(observed_at="2026-11-01T00:00:00Z")


def add_doc(g, name, sections, kind="doc", pieces_per_section=None, rtype="File", tool="docs"):
    """sections: [(heading, level, [(piece kind, text)])]. Returns (doc, [piece records])."""
    props = {"name": name, "title": name.rsplit(".", 1)[0], "path": name, "kind": kind} if rtype == "File" else \
        {"title": name, "kind": "wiki_page"}
    doc = g.rec(rtype, tool, name, props, SCOPE)
    cv = g.rec("ContentVersion", tool, f"{name}:1", {"version": "1", "valid_from": "2026-10-01T00:00:00Z"}, SCOPE, acl_from=doc)
    g.link("VERSION_OF", cv, doc, "field", kind="revision", order=1)
    pieces, order, parents = [], 0, {}
    for s_order, (heading, level, texts) in enumerate(sections):
        parent = parents.get(level - 1)
        path = f"{parent['props']['path']} > {heading}" if parent else heading
        holder = doc
        if rtype == "File":  # the registry lets a DocSection attach to a File or a DocSection only
            sec = g.rec("DocSection", tool, f"{name}:s{s_order}", {"heading": heading, "kind": "heading", "level": level,
                        "order": s_order, "path": path}, SCOPE, acl_from=doc)
            g.link("SECTION_OF", sec, parent or doc, "field", order=s_order)
            parents[level] = holder = sec
        for pkind, text in texts:
            p = g.rec("Piece", tool, f"{name}:p{order}", {"text": text, "kind": pkind, "heading": path, "order": order}, SCOPE, acl_from=doc)
            g.link("PART_OF", p, holder, "field", order=order)
            g.link("OF_VERSION", p, cv, "field")
            pieces.append(p)
            order += 1
    return doc, pieces


def base_graph():
    """People and things the documents name: one Bob, two Sam Lees, two Kim Parks, an existing Platform team."""
    g = new_graph()
    bob = g.rec("Person", "hr", "bob", {"full_name": "Bob Kumar", "work_email": "bob@acme.example"}, SCOPE)
    bob_acct = g.rec("Account", "confluence", "bkumar", {"login": "bkumar", "display_name": "Bob Kumar", "tool": "confluence"}, SCOPE)
    g.link("HAS_ACCOUNT", bob, bob_acct, "field", link_method="csv", verified=True)
    for n in (1, 2):
        g.rec("Account", "slack", f"sam{n}", {"login": f"sam.lee{n}", "display_name": "Sam Lee", "tool": "slack"}, SCOPE)
        g.rec("Account", "slack", f"kim{n}", {"login": f"kim.park{n}", "display_name": "Kim Park", "tool": "slack"}, SCOPE)
    g.rec("Group", "hr", "platform", {"name": "Platform", "kind": "team"}, SCOPE)
    g.rec("Ticket", "jira", "ACME-7", {"identifier": "ACME-7", "title": "Refund retries"}, SCOPE)
    return g


def handbook(g, n_pieces=60, facts_at=17):
    """A 60,000-character handbook: 6 sections of 10 pieces, each piece about 1,000 characters with its own marker."""
    sections = []
    for s in range(6):
        texts = []
        for i in range(n_pieces // 6):
            n = s * (n_pieces // 6) + i
            body = FACTS if n == facts_at else ""
            filler = f"Paragraph {n} of the handbook explains step {n} of the procedure in ordinary words. "
            texts.append(("text", f"MARK-{n:03d} {body} " + filler * ((1000 - len(body)) // len(filler) + 1)))
        sections.append((f"Chapter {s + 1}", 1, texts))
    return add_doc(g, "handbook.docx", sections)


def table_pieces(rows, header, per_piece):
    return [("table", "\n".join([header] + [" | ".join(r) for r in rows[k:k + per_piece]])) for k in range(0, len(rows), per_piece)]


def directory(g):
    return add_doc(g, "employees.csv", [("rows", 1, table_pieces(DIRECTORY, "Name | Email | Team | Manager | Title | Start", 4))],
                   kind="spreadsheet")


# ---------------------------------------------------------------- the stand-in model

def shown(prompt):
    """The text a prompt shows the model, between its markers (the instructions name the markers too)."""
    return prompt.split("\n<<<TEXT\n", 1)[1].split("\nTEXT>>>", 1)[0]


class StandIn:
    """Answers by schema name. Every call is recorded; `delay` makes calls overlap."""

    def __init__(self, delay=0.0):
        self.calls, self.delay, self.flying, self.most_flying = [], delay, 0, 0

    async def ask(self, prompt, name, schema):
        self.calls.append((name, prompt))
        self.flying += 1
        self.most_flying = max(self.most_flying, self.flying)
        if self.delay:
            await asyncio.sleep(random.random() * self.delay)
        self.flying -= 1
        return getattr(self, name)(prompt, schema)

    @staticmethod
    def label_of(prompt, sentence):
        """The label of the passage that holds `sentence`, or None."""
        text = shown(prompt)
        for block in re.split(r"\n(?=\[[A-Z]\d+\] )", text):
            if sentence in block and (m := re.match(r"\s*\[([A-Z]\d+)\]", block)):
                return m.group(1)
        return None

    def ekg_doc_facts(self, prompt, schema):
        p = self.label_of(prompt, "The payments service is owned")
        if p is None:
            return {"records": [], "links": []}
        rec = lambda ref, t, name, quote, statement="", fields=(): {
            "ref": ref, "type": t, "name": name, "statement": statement, "quote": quote, "source": p,
            "fields": [{"field": f, "value": val} for f, val in fields]}
        lk = lambda t, a, b, quote, fields=(): {"type": t, "from": a, "to": b, "quote": quote, "source": p,
                                                 "fields": [{"field": f, "value": val} for f, val in fields]}
        return {"records": [
            rec("r1", "Service", "payments service", "The payments service is owned by the Platform team"),
            rec("r2", "Group", "Platform", "owned by the Platform team"),
            rec("r3", "Decision", "Refunds are retried once", "Bob Kumar decided on 2026-03-02 that refunds are retried once",
                "Refunds are retried one time.", [("decided_at", "2026-03-02"), ("status", "accepted"), ("colour", "blue")]),
            rec("r4", "Person", "Bob Kumar", "Bob Kumar decided"),
            rec("r5", "Person", "Sam Lee", "Sam Lee is the person to ask about refunds"),
            rec("r6", "Person", "Zed Invented", "Zed Invented approves every refund"),          # invented quote
            rec("r7", "Topic", "refunds", "the person to ask about refunds"),
            rec("r8", "Person", "Kim Park", "Kim Park is the expert on ledger exports"),
            rec("r9", "Topic", "ledger exports", "the expert on ledger exports"),
            rec("r10", "Person", "Quinn Fabricated", "Kim Park is the expert on ledger exports"),  # real quote, invented name
            rec("r11", "Ticket", "ACME-404", "See ACME-404"),                     # not created from a document
        ], "links": [
            lk("OWNED_BY", "r1", "r2", "The payments service is owned by the Platform team", [("source", "catalog")]),
            lk("DECIDED_BY", "r3", "r4", "Bob Kumar decided on 2026-03-02", [("role", "decider")]),
            lk("KNOWS_ABOUT", "r5", "r7", "Sam Lee is the person to ask about refunds", [("role", "point_of_contact")]),
            lk("KNOWS_ABOUT", "r8", "r9", "Kim Park is the expert on ledger exports", [("role", "expert")]),
            lk("OWNED_BY", "r1", "r4", "the payments service belongs to Bob Kumar"),             # invented quote
            lk("KNOWS_ABOUT", "r6", "r7", "Sam Lee is the person to ask about refunds"),         # an end that was dropped
        ]}

    def ekg_table_shape(self, prompt, schema):
        """Step 1 of a table's mapping, from the stand-in's whole mapping (ekg_table_map): its shape, and the record
        type each link column names. Step 2 is answered by ekg_table_map itself (code takes its fields and links)."""
        full = self.ekg_table_map(prompt, schema)
        if not isinstance(full, dict) or "error" in full or "_usage" in full:
            return full
        cols = [{"column": x["column"], "type": x["other_type"]} for x in full.get("links") or []
                if isinstance(x, dict) and x.get("other_type")]
        return {**full, "columns": cols}

    def ekg_table_map(self, prompt, schema):
        if '"Manager"' not in prompt:  # the big table: name, email, team
            return {"row_type": "Person", "name_column": "c1", "key_column": "c2", "reason": "a staff list",
                    "fields": [{"column": "c2", "field": "work_email"}],
                    "links": [{"column": "c3", "link": "MEMBER_OF", "other_type": "Group", "row_is": "from", "split": "none"}]}
        return {"row_type": "Person", "name_column": "c1", "key_column": "c2", "reason": "an employee directory",
                "fields": [{"column": "c2", "field": "work_email"}, {"column": "c5", "field": "title"},
                           {"column": "c6", "field": "start_date"}, {"column": "c3", "field": "favourite_colour"}],
                "links": [{"column": "c3", "link": "MEMBER_OF", "other_type": "Group", "row_is": "from", "split": ";"},
                          {"column": "c4", "link": "REPORTS_TO", "other_type": "Person", "row_is": "from", "split": "none"},
                          {"column": "c5", "link": "HOLDS_POSITION", "other_type": "Position", "row_is": "to", "split": "none"},
                          {"column": "c2", "link": "HAS_ALIAS", "other_type": "EmailAddress", "row_is": "from", "split": "none"},
                          {"column": "c3", "link": "DECIDED_BY", "other_type": "Group", "row_is": "from", "split": "none"}]}

    def ekg_entity_choice(self, prompt, schema):
        if '"Kim Park"' in prompt:  # the text settles it (the stand-in says so): the first candidate
            return {"verdict": "accept", "choice": "C1", "quote": "Kim Park is the expert on ledger exports", "cue": "", "reason": "test"}
        return {"verdict": "reject", "choice": "none", "quote": "", "cue": "", "reason": "two people share the name"}

    def ekg_knowledge_extract(self, prompt, schema):
        return {"items": []}

    def ekg_depends_on(self, prompt, schema):   # what an item waits on (extract_gates): none, unless a test says
        return {"links": []}

    def ekg_grant_level(self, prompt, schema):   # the level of a stated grant (extract_gates): not stated, unless a test says
        return {"verdict": "reject", "choice": "none", "quote": "", "cue": "", "reason": "test"}


def run_fill(g, model, kinds, window=20_000, concurrency=16, out=None, report=None, **kw):
    """fill.run on a graph written to a temp folder, with the stand-in in place of query-master's client."""
    tmp = Path(tempfile.mkdtemp(prefix="ekg-fill-test-"))
    TEMP.append(tmp)
    g.write(tmp)
    if report:
        (tmp / "report.json").write_text(json.dumps(report), encoding="utf-8")
    llm.bind = lambda qm, m=None: "stand-in test-model"
    llm.window_tokens = lambda override=None: (override or window, "test")
    llm.ask = model.ask
    out = out or tmp / "out"
    rep = fill.run(tmp, QM, out, kinds, mode=kw.pop("mode", "run"), concurrency=concurrency, log=lambda *_: None, **kw)
    return rep, out, tmp


def rows(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")] if Path(path).exists() else []


def written(out, tmp):
    recs, links = rows(out / "records.jsonl"), rows(out / "links.jsonl")
    by_id = {r["id"]: r for r in rows(tmp / "records.jsonl")}
    by_id.update({r["id"]: r for r in recs})
    return recs, links, by_id


def all_valid(recs, links, by_id):
    bad = [res.errors for r in recs if not (res := common._vrec(r)).ok]
    bad += [res.errors for lk in links if not (res := common._vlink(lk, by_id[lk["a"]]["type"], by_id[lk["b"]]["type"])).ok]
    return bad


# ---------------------------------------------------------------- checks

def sizing_checks(check):
    b = fill_budget.sizes(131_072, "test")
    check("sizes: a 131,072-token window gives a 78,643-token prompt (235,929 chars) and leaves 52,429 for the reply",
          (b.prompt_tokens, b.prompt_chars, b.reply_tokens) == (78_643, 235_929, 52_429))
    check("sizes: an unknown window falls back to a safe one, and says so",
          fill_budget.sizes(0).window_tokens == fill_budget.FALLBACK_WINDOW_TOKENS and "fallback" in fill_budget.sizes(None).source)
    units = [f"u{i} " + "x" * 900 for i in range(50)]
    wins = fill_budget.pack(units, 4000)
    check("pack: consecutive windows of whole units, each inside the room, nothing lost or repeated",
          [u for w in wins for u in w] == units and all(sum(map(len, w)) <= 4000 for w in wins) and len(wins) > 5)
    big = "\n".join(f"line {i} " + "y" * 80 for i in range(400))
    parts = fill_budget.split_text(big, 5000, repeat="HEADER")
    check("split_text: a text larger than a window is cut at line ends only, with the header repeated",
          all(len(p) <= 5000 for p in parts) and "\n".join(p.removeprefix("HEADER\n") for p in parts) == big
          and all(p.startswith("HEADER\n") for p in parts[1:]))
    src = (HERE / "ekg_adapters" / "fill_tasks.py").read_text(encoding="utf-8") + (HERE / "ekg_adapters" / "fill.py").read_text(encoding="utf-8")
    check("no fixed caps left: DOC_TEXT_CAP, TEXT_CAP, PROMPT_CAP, max_tokens, 'At most 5', [:5], DOC_RECENT_DAYS are gone",
          not re.search(r"DOC_TEXT_CAP|\bTEXT_CAP|PROMPT_CAP|max_tokens=\d|At most 5|\[\]\)\[:5\]|DOC_RECENT_DAYS|\[:MAX_SOURCES\]|< 600", src))
    words = re.compile(r"kafka|apache|\bKIP\b|flink|mailing lists and wiki", re.I)
    prompts = " ".join([fill.HEADER, fill.EXTRACT, fill.JUDGE_ITEMS, fill.ANSWER, " ".join(sorted(__import__("ekg_adapters.fill_tasks", fromlist=["STOP"]).STOP))])
    check("prompts and stop words name no product, project or source", not words.search(prompts))


def reading_checks(check):
    g = base_graph()
    doc, pieces = handbook(g)
    wiki, wiki_pieces = add_doc(g, "Runbook", [("Restart", 1, [("h1", "WIKI-A restart the broker with care " * 30)]),
                                               ("Rollback", 1, [("h2", "WIKI-B roll back by tag " * 30)])], rtype="Document", tool="confluence")
    bare = g.rec("File", "docs", "notes.md", {"name": "notes.md", "path": "notes.md", "kind": "doc"}, SCOPE)
    lines = [f"NOTE-{i:03d} a line of plain notes with no pieces at all, number {i}." for i in range(900)]
    cv = g.rec("ContentVersion", "docs", "notes.md:1", {"version": "1", "text": "\n".join(lines)}, SCOPE, acl_from=bare)
    g.link("VERSION_OF", cv, bare, "field", kind="revision", order=1)
    old = g.rec("File", "docs", "old.md", {"name": "old.md", "path": "old.md", "kind": "doc"}, SCOPE)
    for n, (when, text) in enumerate([("2020-01-01T00:00:00Z", "OLD-TEXT first version"), ("2021-01-01T00:00:00Z", "NEW-TEXT short")], 1):
        ov = g.rec("ContentVersion", "docs", f"old.md:{n}", {"version": str(n), "text": text, "valid_from": when}, SCOPE, acl_from=old)
        g.link("VERSION_OF", ov, old, "field", kind="revision", order=n)
    code = g.rec("File", "git", "src/a.py", {"name": "a.py", "path": "src/a.py", "kind": "source"}, SCOPE)
    ccv = g.rec("ContentVersion", "git", "src/a.py:1", {"version": "1", "text": "CODE-TEXT def a(): pass"}, SCOPE, acl_from=code)
    g.link("VERSION_OF", ccv, code, "field", kind="revision", order=1)
    recs, links = list(g.records.values()), [lk for lk, _, _ in g.links.values()]
    v = View(recs, links, budget=fill_budget.sizes(20_000, "test"))
    tasks, skipped = BUILDERS["doc_facts"](v)
    mine = [t for t in tasks if t.subject == doc["id"]]
    prompts = [fill.first_prompt(t, v) for t in mine]
    texts = [shown(p) for p in prompts]
    seen = collections.Counter(m for tx in texts for m in re.findall(r"MARK-\d{3}", tx))
    check("reading: the 60,000-character document is read in several consecutive windows", len(mine) >= 3
          and sum(len(p["props"]["text"]) for p in pieces) >= 60_000)
    check("reading: every one of its 60 pieces is in exactly one window's text (none unread, none twice)",
          len(seen) == 60 and set(seen.values()) == {1})
    check("reading: the windows keep the document's order", re.findall(r"MARK-\d{3}", "".join(texts)) == [f"MARK-{i:03d}" for i in range(60)])
    check("reading: every window is headed by the title, the whole outline and its own place",
          all("DOCUMENT: handbook" in p and "OUTLINE OF THE WHOLE DOCUMENT" in p and all(f"Chapter {i}" in p.split("\n<<<TEXT\n")[0] for i in range(1, 7))
              and re.search(rf"THIS IS PART {n + 1} OF {len(prompts)}", p) for n, p in enumerate(prompts)))
    check("reading: each passage is shown under its section path", all(re.search(r"\[P\d+\] Chapter \d", tx) for tx in texts))
    check("reading: no prompt is larger than the window allows", all(len(p) <= v.budget.prompt_chars for p in prompts))
    check("reading: a wiki page whose sections are Pieces (no DocSection) is read with its outline from the pieces",
          any(t.subject == wiki["id"] and "WIKI-A" in fill.first_prompt(t, v) and "WIKI-B" in fill.first_prompt(t, v)
              and "Restart" in t.context and "Rollback" in t.context for t in tasks))
    notes = [shown(fill.first_prompt(t, v)) for t in tasks if t.subject == bare["id"]]
    got = collections.Counter(m for tx in notes for m in re.findall(r"NOTE-\d{3}", tx))
    check("reading: a document with no pieces is read from its version text, all of it, cut at line ends",
          len(notes) >= 2 and len(got) == 900 and set(got.values()) == {1})
    olds = "".join(fill.first_prompt(t, v) for t in tasks if t.subject == old["id"])
    check("reading: the current version is read, however short and however old (no recency or length skip)",
          "NEW-TEXT" in olds and "OLD-TEXT" not in olds and not any("recent" in k or "short" in k for k in skipped))
    check("reading: a code host's source files are not documents", not any(t.subject == code["id"] for t in tasks))
    v2 = View(recs, links, budget=fill_budget.sizes(20_000, "test"), doc_sample=2)
    t2, s2 = BUILDERS["doc_facts"](v2)
    check("reading: --docs N samples N documents and counts the rest", len({t.subject for t in t2}) == 2
          and s2.get("not read: outside the --docs sample") == 2)

    # conversations: every message, cut between messages
    g = base_graph()
    th = g.rec("Thread", "slack", "t1", {"kind": "chat", "title": "Long thread"}, SCOPE)
    author = g.rec("Account", "slack", "u1", {"login": "u1", "display_name": "U One", "tool": "slack"}, SCOPE)
    for i in range(40):
        m = g.rec("Message", "slack", f"m{i}", {"kind": "message", "text": f"MSG-{i:02d} " + "talk " * 400, "at": f"2026-10-01T00:{i:02d}:00Z"}, SCOPE)
        g.link("IN", m, th, "field")
        g.link("AUTHORED_BY", m, author, "field", role="author")
    v = View(list(g.records.values()), [lk for lk, _, _ in g.links.values()], budget=fill_budget.sizes(20_000, "test"))
    ktasks, _ = knowledge(v)
    kseen = collections.Counter(m for t in ktasks for m in re.findall(r"MSG-\d\d", fill.extract_prompt(t)))
    from ekg_adapters.fill_tasks import QUOTE_MARK, dequote
    left = collections.Counter()
    thread = dequote(["We ship on Friday.\nThe gate is the soak test.",
                      "> We ship on Friday.\n> The gate is the soak test.\nAgreed, and I will run it.",
                      ">> We ship on Friday.\n> Agreed, and I will run it.\n> The audit says otherwise.\nWho ran the audit?"], left, "k")
    check("conversations: lines that only quote an earlier message of the same thread are left out and counted; "
          "a quoted line from anywhere else stays",
          thread[0] == "We ship on Friday.\nThe gate is the soak test." and thread[1] == f"{QUOTE_MARK}\nAgreed, and I will run it."
          and thread[2] == f"{QUOTE_MARK}\n> The audit says otherwise.\nWho ran the audit?" and left["k"] > 80)
    check("conversations: all 40 messages of a long thread are read, in windows cut between messages (no first-8 limit)",
          len(ktasks) >= 2 and len(kseen) == 40 and set(kseen.values()) == {1} and all("part " in t.context for t in ktasks)
          and all(len(fill.extract_prompt(t)) <= v.budget.prompt_chars for t in ktasks))


def schema_checks(check):
    reg = common.bind(QM)
    cat = extract_schema.Catalog(reg, ["Ticket", "PullRequest", "Account", "Release", "Repository", "Project", "Milestone",
                                      "ContentVersion", "File", "Document"])
    check("schema: the types a document may create come from the registry (people, teams, positions, services, topics, "
          "and the knowledge types)", {"Person", "Group", "Position", "Organization", "Service", "Topic", "EmailAddress", "Decision",
                                        "Guideline", "Requirement", "Knowledge", "ActionItem", "Question"} <= set(cat.creatable))
    check("schema: tool records are link targets only, never created (tickets, accounts, releases, projects)",
          {"Ticket", "Account", "Release", "Project", "Milestone"} <= set(cat.resolvable) and not {"Ticket", "Account", "PullRequest"} & set(cat.creatable)
          and "ContentVersion" not in cat.resolvable)
    check("schema: the links offered are the registry's own between those types",
          {"MEMBER_OF", "REPORTS_TO", "HOLDS_POSITION", "OWNED_BY", "MAINTAINED_BY", "KNOWS_ABOUT", "ABOUT", "DEPENDS_ON", "DECIDED_BY",
           "ASSIGNED_TO", "AFFECTS", "TRACKED_BY", "WORKS_AT", "HAS_ALIAS"} <= set(cat.links)
          and all(reg.link_allows(n, a, b) for n, (fr, to) in cat.links.items() for a in fr[:1] for b in to[:1])
          and not any(reg.link(n).internal for n in cat.links))
    text = cat.describe()
    check("schema: the prompt's schema section lists types with their fields and links with their from -> to types",
          "- Person NEW:" in text and "Named by full_name" in text and "- Ticket:" in text
          and re.search(r"- REPORTS_TO: .*Person.* -> .*Person", text) and "strength (must|should|avoid)" in text)
    check("schema: origin links are found by rule (Knowledge EXTRACTED_FROM a Piece, Decision MADE_IN its File)",
          cat.origin_for("Knowledge", ["Piece", "File"]) == ("EXTRACTED_FROM", "Piece")
          and cat.origin_for("Decision", ["Piece", "File"]) == ("MADE_IN", "File") and cat.origin_for("Person", ["Piece", "File"]) is None)


def extraction_checks(check):
    g = base_graph()
    doc, pieces = handbook(g)
    model = StandIn()
    rep, out, tmp = run_fill(g, model, ["doc_facts"], report={"docs": {"doc_quality": {"handbook.docx": 0.9}}})
    recs, links, by_id = written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    facts_piece = pieces[17]["id"]
    new = collections.defaultdict(list)
    for r in recs:
        new[r["type"]].append(r)
    L = collections.defaultdict(list)
    for lk in links:
        L[lk["type"]].append(lk)
    first_reads = [p for n, p in model.calls if n == "ekg_doc_facts" and "NOT COVERED YET" not in p]
    check("extract: one first call per window, every window asked (follow-up readings of what yielded nothing come after)",
          len(first_reads) == rep["kinds"]["doc_facts"]["to_ask"] >= 3)
    check("extract: an invented quote is rejected by code, and so is a name the text does not contain",
          rep["kinds"]["doc_facts"]["accepted"] == 1 and not any(r["props"].get("full_name") in ("Zed Invented", "Quinn Fabricated") for r in recs)
          and any(e.get("dropped", {}).get("check: quote not in its source") == 2 and e["dropped"].get("check: name not in its source") == 1
                  for e in rows(out / "ledger.jsonl") if e["status"] == "accepted"))
    check("extract: a name with exactly one record links to it, and no duplicate is created (Bob, the Platform team)",
          not any(r["props"].get("full_name") == "Bob Kumar" for r in new["Person"]) and not any(r["props"].get("name") == "Platform" for r in new["Group"])
          and c.get("Group: resolved to the one existing record") == 1 and c.get("Person: resolved to the one existing record") == 1)
    svc = next((r for r in new["Service"] if r["props"].get("name") == "payments service"), None)
    platform = next(r for r in by_id.values() if r["type"] == "Group" and r["props"].get("name") == "Platform")
    check("extract: a new Service is created (its type may come from a document) and OWNED_BY the existing team",
          svc and any(lk["a"] == svc["id"] and lk["b"] == platform["id"] and lk["props"].get("source") == "catalog" for lk in L["OWNED_BY"]))
    dec = next((r for r in new["Decision"]), None)
    check("extract: the Decision keeps its stated date (to the day) and status, each cited to the quoted piece; unknown fields are left out",
          dec and dec["props"].get("decided_at") == "2026-03-02T00:00:00Z" and dec["props"].get("decided_at_precision") == "day"
          and dec["props"].get("status") == "accepted" and "colour" not in dec["props"]
          and set(dec["props"]["_stated"]) >= {"title", "statement", "decided_at"}
          and all(list(s["by"]) == [facts_piece] for s in dec["props"]["_stated"].values()))
    check("extract: its origin is MADE_IN the document (the schema does not let MADE_IN end at a Piece), evidence the piece (R41, R71)",
          dec and any(lk["a"] == dec["id"] and lk["b"] == doc["id"] and lk["props"]["evidence"] == [facts_piece] for lk in L["MADE_IN"]))
    bob_acct = next(r for r in by_id.values() if r["type"] == "Account" and r["props"].get("login") == "bkumar")
    check("extract: DECIDED_BY needs an Account, so Bob the Person is followed to his one account",
          dec and any(lk["a"] == dec["id"] and lk["b"] == bob_acct["id"] and lk["props"].get("role") == "decider" for lk in L["DECIDED_BY"]))
    sams = {r["id"] for r in by_id.values() if r["props"].get("display_name") == "Sam Lee"}
    kims = sorted(r["props"]["_key"] for r in by_id.values() if r["props"].get("display_name") == "Kim Park")
    know = [(by_id[lk["a"]], by_id[lk["b"]], lk) for lk in L["KNOWS_ABOUT"]]
    check("extract: an ambiguous name is never linked silently: the model is asked, and its reject leaves Sam Lee unlinked and counted",
          rep["kinds"]["entity_choice"]["to_ask"] == 2 and rep["kinds"]["entity_choice"]["rejected"] == 1
          and not any(lk["a"] in sams or lk["b"] in sams for lk in links)
          and c.get("Person: several records match and the text does not say which, not linked") == 1
          and not any(r["props"].get("full_name") in ("Sam Lee", "Kim Park") for r in new["Person"]))
    check("extract: when the model chooses among candidates (Kim Park) the link is written, with the lowest confidence",
          len(know) == 1 and know[0][0]["props"].get("display_name") == "Kim Park" and know[0][1]["props"].get("name") == "ledger exports"
          and know[0][2]["props"]["confidence"] == round(fill.CONFIDENCE["chosen"] * 0.9, 3) and know[0][2]["props"].get("role") == "expert")
    check("extract: a ticket the graph does not hold is not created from a document", not new["Ticket"]
          and c.get("Ticket: no such record in the graph (this type is not created from a document)") == 1)
    conf = {lk["type"]: lk["props"]["confidence"] for lk in links if lk["type"] in ("OWNED_BY", "DECIDED_BY", "MADE_IN")}
    check("confidence: from the table of priors times the document's quality (0.9): new record 0.63, one existing record 0.765",
          conf == {"OWNED_BY": 0.63, "DECIDED_BY": 0.63, "MADE_IN": 0.63}
          and any(lk["type"] == "MENTIONS" and lk["b"] == platform["id"] and lk["props"]["confidence"] == 0.765 for lk in links))
    check("extract: each new entity is MENTIONED by the piece that names it (its link back to a source)",
          svc and any(lk["a"] == facts_piece and lk["b"] == svc["id"] and lk["props"].get("kind") == "name" for lk in L["MENTIONS"]))
    check("extract: every written record and link validates; links are llm_judge with the quoted piece as evidence and a rule_id",
          not all_valid(recs, links, by_id) and links and all(lk["props"]["method"] == "llm_judge" and lk["props"]["evidence"] == [facts_piece]
                                                              and lk["props"].get("rule_id") for lk in links)
          and any(r["type"] == "DerivationRun" for r in recs))
    return g


def table_checks(check):
    g = base_graph()
    doc, pieces = directory(g)
    model = StandIn()
    rep, out, tmp = run_fill(g, model, ["table_map", "doc_facts"])
    recs, links, by_id = written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    people = {r["props"]["full_name"]: r for r in by_id.values() if r["type"] == "Person"}
    L = collections.defaultdict(set)
    for lk in links:
        L[lk["type"]].add((_name(by_id[lk["a"]]), _name(by_id[lk["b"]])))
    check("table: two small model calls for the whole table (its shape, then its columns), none per row (a tables-only "
          "file is not read as prose)",
          [n for n, _ in model.calls] == ["ekg_table_shape", "ekg_table_map"] and rep["kinds"]["doc_facts"]["to_ask"] == 0)
    check("table: step 1 shows the header, sample rows and the record types; step 2 the links code found between the "
          "table's types (MEMBER_OF), not a question per row",
          "c1 = \"Name\"" in model.calls[0][1] and "7 rows" in model.calls[0][1] and "- Person:" in model.calls[0][1]
          and "- MEMBER_OF:" in model.calls[1][1] and "- MEMBER_OF:" not in model.calls[0][1])
    check("table: a Person for every row with a name; the existing Bob is reused, not duplicated",
          set(people) == {"Alice Chen", "Bob Kumar", "Carla Diaz", "Dev Patel", "Erin Shaw", "Femi Ade"}
          and people["Bob Kumar"]["props"]["source_tool"] == "hr" and c.get("table rows written") == 6 and c.get("table rows read") == 7)
    check("table: fields land in the registry's fields (title, email, start date to the day); a value that does not fit is counted",
          people["Alice Chen"]["props"].get("title") == "Staff Engineer" and people["Alice Chen"]["props"].get("work_email") == "alice@acme.example"
          and people["Alice Chen"]["props"].get("start_date") == "2021-04-01T00:00:00Z" and "start_date" not in people["Dev Patel"]["props"]
          and c.get("cell skipped: start_date value does not fit its type") == 1 and people["Bob Kumar"]["props"].get("title") == "Engineering Manager")
    check("table: MEMBER_OF for every row, a cell with two teams split, the existing Platform team reused and Data created",
          L["MEMBER_OF"] == {("Alice Chen", "Platform"), ("Bob Kumar", "Platform"), ("Carla Diaz", "Data"), ("Dev Patel", "Data"),
                             ("Erin Shaw", "Platform"), ("Erin Shaw", "Data"), ("Femi Ade", "Data")}
          and sum(1 for r in by_id.values() if r["type"] == "Group" and r["props"].get("name") == "Platform") == 1)
    check("table: REPORTS_TO for every manager who is one known person, also one named in a later row",
          L["REPORTS_TO"] == {("Alice Chen", "Bob Kumar"), ("Carla Diaz", "Bob Kumar"), ("Erin Shaw", "Alice Chen"), ("Femi Ade", "Carla Diaz")})
    check("table: an ambiguous manager (two Sam Lees) is not linked, and is counted by reason",
          c.get("REPORTS_TO skipped: 2 records match, not linked") == 1)
    check("table: positions and email addresses become records with their links (direction fixed by the registry)",
          ("Alice Chen", "Staff Engineer") in L["HOLDS_POSITION"] and ("Erin Shaw", "Staff Engineer") in L["HOLDS_POSITION"]
          and sum(1 for r in by_id.values() if r["type"] == "Position" and r["props"].get("name") == "Staff Engineer") == 1
          and ("Carla Diaz", "carla@acme.example") in L["HAS_ALIAS"])
    led = rows(out / "ledger.jsonl")[0]
    check("table: the mapping is checked against the registry (an unknown field and an impossible link are dropped, counted)",
          led["dropped"].get("mapping: favourite_colour is not a field of Person in the registry") == 1
          and led["dropped"].get("mapping: DECIDED_BY cannot join a Person and a Group") == 1
          and led["dropped"].get("mapping: HOLDS_POSITION direction turned to the one the registry allows") == 1)
    check("table: rows that cannot be written are counted by reason, never dropped silently",
          c.get("row skipped: no value in the name column") == 1)
    piece_of = {"Alice Chen": pieces[0]["id"], "Femi Ade": pieces[1]["id"]}
    facts = [lk for lk in links if lk["type"] != "MENTIONS"]
    check("table: every link is template_rule at the table prior, with the row's own piece as evidence and the pass's DerivationRun",
          facts and all(lk["props"]["method"] == "template_rule" and lk["props"]["confidence"] == fill.CONFIDENCE["table"]
                        and lk["props"].get("rule_id") for lk in links)
          and all(lk["props"]["evidence"] == [piece_of[n]] for lk in facts for n in piece_of if by_id[lk["a"]]["props"].get("full_name") == n)
          and any(r["type"] == "DerivationRun" and r["props"]["rule"] == "llm.table_map" for r in recs))
    check("table: every written record and link validates", not all_valid(recs, links, by_id))

    # 50,000 rows: one call
    g = new_graph()
    big = [(f"Person {i:05d}", f"p{i:05d}@acme.example", f"Team {i % 40:02d}") for i in range(50_000)]
    add_doc(g, "everyone.csv", [("rows", 1, table_pieces(big, "Name | Email | Team", 1000))], kind="spreadsheet")
    model = StandIn()
    started = time.time()
    rep, out, tmp = run_fill(g, model, ["table_map"])
    took = time.time() - started
    recs, links, by_id = written(out, tmp)
    kinds = collections.Counter(r["type"] for r in recs)
    lt = collections.Counter(lk["type"] for lk in links)
    check(f"table: a 50,000-row table makes exactly two model calls, shape and columns ({took:.0f}s to write it all in code)",
          len(model.calls) == 2 and rep["calls"]["made"] == 2)
    check("table: all 50,000 rows become Person records with MEMBER_OF to 40 teams",
          kinds["Person"] == 50_000 and kinds["Group"] == 40 and lt["MEMBER_OF"] == 50_000
          and rep["written"]["documents_and_tables"].get("table rows written") == 50_000)
    sample = random.Random(7).sample(recs, 300) + [r for r in recs if r["type"] != "Person"]
    check("table: the big table's records and links validate (300 sampled records, every 50th link)",
          not all_valid(sample, links[::50], by_id))


def _name(r):
    p = r["props"]
    return p.get("full_name") or p.get("name") or p.get("address") or p.get("title") or p.get("text")


def concurrency_checks(check):
    g = base_graph()
    handbook(g, n_pieces=120)
    directory(g)
    model = StandIn(delay=0.02)
    rep, out, tmp = run_fill(g, model, ["table_map", "doc_facts"], window=9_000, concurrency=16)
    n_tasks = rep["kinds"]["doc_facts"]["to_ask"] + rep["kinds"]["table_map"]["to_ask"] + rep["kinds"]["entity_choice"]["to_ask"]
    resp_lines = open(out / "responses.jsonl", encoding="utf-8").read().splitlines()
    led_lines = open(out / "ledger.jsonl", encoding="utf-8").read().splitlines()
    resp, led = [json.loads(x) for x in resp_lines], [json.loads(x) for x in led_lines]
    check(f"concurrency: {n_tasks} tasks ran with up to {model.most_flying} calls in flight (16 allowed)",
          n_tasks >= 20 and 8 <= model.most_flying <= 16)
    check("concurrency: responses.jsonl is whole lines, one per call, no key twice",
          len(resp) == len(model.calls) == rep["calls"]["made"] and len({r["key"] for r in resp}) == len(resp)
          and all(r["prompt"] and isinstance(r["response"], dict) for r in resp))
    check("concurrency: the ledger has exactly one whole line per task", len(led) == n_tasks and len({e["task"] for e in led}) == n_tasks
          and all(e["status"] in ("accepted", "rejected") for e in led))
    first = (sorted(json.dumps([r["type"], r["props"]["_key"]]) for r in rows(out / "records.jsonl") if r["type"] != "DerivationRun"),
             sorted(json.dumps([lk["type"], lk["a"], lk["b"]]) for lk in rows(out / "links.jsonl")))
    again = StandIn(delay=0.02)
    rep2, out2, _ = run_fill(g, again, ["table_map", "doc_facts"], window=9_000, concurrency=16, out=out)
    second = (sorted(json.dumps([r["type"], r["props"]["_key"]]) for r in rows(out / "records.jsonl") if r["type"] != "DerivationRun"),
              sorted(json.dumps([lk["type"], lk["a"], lk["b"]]) for lk in rows(out / "links.jsonl")))
    check("concurrency: a second run makes no call (the ledger answers) and writes the same records and links",
          not again.calls and rep2["calls"]["made"] == 0 and first == second and first[1])
    (out / "ledger.jsonl").unlink()
    third = StandIn()
    rep3, _, _ = run_fill(g, third, ["table_map", "doc_facts"], window=9_000, concurrency=16, out=out)
    check("concurrency: with the ledger gone, responses.jsonl answers every prompt (a stopped run pays nothing twice)",
          not third.calls and rep3["calls"]["answered_from_responses"] + 2 * rep3["kinds"]["table_map"]["mappings_from_cache"]
          == rep["calls"]["made"] + rep["calls"]["answered_from_responses"]
          and rep3["kinds"]["table_map"]["mappings_from_cache"] >= 1)

    async def same_prompt():
        with tempfile.TemporaryDirectory() as d:
            m = StandIn(delay=0.01)
            llm.ask = m.ask
            r = fill.Responses(Path(d) / "responses.jsonl", "test-model")
            got = await asyncio.gather(*[r.ask(f"t{i}", f"prompt {i % 50}", "ekg_knowledge_extract", {}) for i in range(400)])
            r.close()
            n = len(open(Path(d) / "responses.jsonl", encoding="utf-8").read().splitlines())
            return len(m.calls), n, len(got), r.hits
    calls, lines, got, hits = asyncio.run(same_prompt())
    check("concurrency: 400 asks of 50 distinct prompts at once make 50 calls and 50 cache lines",
          (calls, lines, got, hits) == (50, 50, 400, 350))

    # an answer that fails is asked again as two halves, not lost
    g = base_graph()
    handbook(g)

    class Flaky(StandIn):
        def ekg_doc_facts(self, prompt, schema):
            if prompt.count("MARK-") > 6:
                return {"error": "reply cut off"}
            return super().ekg_doc_facts(prompt, schema)
    flaky = Flaky()
    rep, out, tmp = run_fill(g, flaky, ["doc_facts"])
    check("a window whose answer fails is asked again as halves, so its text is still read and its facts written",
          rep["kinds"]["doc_facts"].get("accepted") == 1 and rep["kinds"]["doc_facts"].get("error", 0) == 0
          and len(flaky.calls) > rep["kinds"]["doc_facts"]["to_ask"] and rep["written"]["links_by_type"].get("OWNED_BY") == 1)


def dry_run_checks(check):
    g = base_graph()
    handbook(g)
    directory(g)
    model = StandIn()
    rep, out, tmp = run_fill(g, model, ["table_map", "doc_facts", "doc_knowledge", "knowledge"], mode="dry", docs=None, limit=None)
    k = rep["kinds"]
    check("dry run: no model call; tasks, calls, prompt characters and token estimates per kind, and the window used",
          not model.calls and k["doc_facts"]["to_ask"] >= 3 and k["table_map"]["to_ask"] == 1 and k["table_map"]["table_rows"] == 7
          and k["doc_knowledge"]["calls_per_task"] == 2 and k["doc_facts"]["prompt_chars_total"] > 60_000
          and all(len(r["est_prompt_tokens"]) == 2 for r in k.values()) and rep["budget"]["window_tokens"] == 20_000
          and rep["totals"]["calls"] == sum(r["to_ask"] * r["calls_per_task"] for r in k.values())
          and "sampling" in rep and rep["schema_gaps"] and (out / "prompts_sample.txt").exists())


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    random.seed(11)
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(QM)
    try:
        sizing_checks(check)
        schema_checks(check)
        reading_checks(check)
        extraction_checks(check)
        table_checks(check)
        concurrency_checks(check)
        dry_run_checks(check)
    finally:
        for d in TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
