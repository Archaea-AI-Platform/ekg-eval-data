"""Behaviour tests for the follow-up fixes to the LLM stage's document side (stand-in model: no key, no network, no cost).

  1. extraction windows are sized by the provider's reply limit, not only the context window
  2. tables: a mapped table is not read again as text; a rejected one is; heading lines, repeated headers,
     key/value tables, relation tables with link fields, split cells, columns with no field counted
  3. progress lines are flushed, and a heartbeat speaks while calls are slow
  4. a person named by one bare word is resolved when exactly one record fits, never created from it
  5. new knowledge records get the default status; Knowledge is DOCUMENTED_IN its document

    python -I ekg-eval-data/test_fill_followups.py
"""

import collections
import io
import os
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402  (fixtures and the stand-in model)
from ekg_adapters import common, fill, fill_budget, fill_docs, llm  # noqa: E402
from ekg_adapters.fill_tasks import BUILDERS, View  # noqa: E402

SCOPE = base.SCOPE
PROSE = "MARK-PROSE The ledger team keeps the books. "


def run_fill(g, model, kinds, window=20_000, reply_limit=0, **kw):
    llm.reply_limit = lambda override=None: (override or reply_limit, "test" if (override or reply_limit) else "")
    return base.run_fill(g, model, kinds, window=window, **kw)


def view(g, window=20_000, reply_limit=0):
    return View(list(g.records.values()), [lk for lk, _, _ in g.links.values()],
                budget=fill_budget.sizes(window, "test", max_reply_tokens=reply_limit))


class Model(base.StandIn):
    """The stand-in, with table answers chosen by the table's first column header."""
    tables = {}
    facts = None

    def ekg_table_map(self, prompt, schema):
        for marker, ans in self.tables.items():
            if marker in prompt:
                return ans
        return super().ekg_table_map(prompt, schema)

    def ekg_doc_facts(self, prompt, schema):
        if self.facts is not None:
            return self.facts(prompt)
        return {"records": [], "links": []}


def empty_kv(**kw):
    return {"shape": "rows", "row_type": "none", "name_column": "none", "key_column": "none", "fields": [], "links": [],
            "entity_name": "", "label_fields": [], "label_links": [], "reason": "test", **kw}


# ---------------------------------------------------------------- 1. sizing by the reply limit

def sizing_checks(check):
    b = fill_budget.sizes(131_072, "test", max_reply_tokens=16_384, reply_source="test")
    check("sizes: a 131,072-token window with a 16,384-token reply limit keeps the prompt (235,929 chars) and caps the "
          "reply at 16,384; an extraction call reads at most 24,576 characters",
          (b.prompt_chars, b.reply_tokens, b.read_chars) == (235_929, 16_384, 24_576)
          and fill_budget.READ_SHARE_OF_REPLY == 0.5)
    u = fill_budget.sizes(131_072, "test")
    check("sizes: an unknown reply limit leaves the reply as before (52,429 tokens)", u.reply_tokens == 52_429 and u.reply_limit == 0)
    check("sizes: a reply limit larger than the room left is no cap", fill_budget.sizes(131_072, "t", max_reply_tokens=900_000).reply_tokens == 52_429)
    check("sizes: the report says where the reply limit came from", b.describe()["reply_limit_from"] == "test"
          and b.describe()["extraction_text_chars_per_call"] == 24_576)

    g = base.base_graph()
    base.handbook(g)
    v = view(g, 131_072, 16_384)
    for k in ("doc_facts", "knowledge", "pr_ticket", "table_map"):
        v.overhead.setdefault(k, 4000)
    check("room: extraction kinds (doc_facts, knowledge, doc_knowledge) read no more than the reply can answer; "
          "judge kinds keep the context-based room",
          v.room("doc_facts") == 24_576 and v.room("knowledge") == 24_576 and v.room("pr_ticket") == v.budget.room(4000) > 200_000)
    tasks, _ = BUILDERS["doc_facts"](v)
    texts = [sum(len(s.text) for s in t.sources) for t in tasks]
    seen = collections.Counter(m for t in tasks for s in t.sources for m in re.findall(r"MARK-\d{3}", s.text))
    check(f"room: the 60,000-character handbook becomes {len(tasks)} short windows of at most 24,576 text characters, "
          "every piece read once", len(tasks) >= 3 and max(texts) <= 24_576 and len(seen) == 60 and set(seen.values()) == {1})
    v2 = view(g, 131_072)
    t2, _ = BUILDERS["doc_facts"](v2)
    check("room: without a reply limit the same document fits one window (the context-based room, as before)", len(t2) == 1)

    model = Model()
    rep, out, _ = run_fill(g, model, ["doc_facts"], window=131_072, reply_limit=16_384)
    check("run: the reply limit reaches the budget and the report; every call reads at most 24,576 characters",
          rep["budget"]["reply_tokens"] == 16_384 and rep["kinds"]["doc_facts"]["to_ask"] >= 3
          and rep["kinds"]["doc_facts"]["text_chars_per_window_max"] <= 24_576
          and len([p for n, p in model.calls if n == "ekg_doc_facts" and "NOT COVERED YET" not in p]) == rep["kinds"]["doc_facts"]["to_ask"]
          and all(len(base.shown(p)) <= 24_576 + 2_000 for n, p in model.calls if n == "ekg_doc_facts"))

    # where the reply limit comes from: OpenRouter's model list, through query-master's context_window
    saved = llm._context, llm._settings, llm.window_tokens, llm.reply_limit, llm._REPLY_LIMIT
    import importlib
    real = importlib.reload(llm)
    try:
        class Ctx:
            @staticmethod
            def forced_window():
                return 0

            @staticmethod
            async def openrouter_limits(http, model):
                return 131_072, 16_384

        class Settings:
            LLM_BACKEND, OPENROUTER_MODEL, AGENTIC_LLM_MODEL = "openrouter", "some/model", None
        real._context, real._settings = Ctx, Settings
        os.environ.pop("EKG_LLM_MAX_REPLY_TOKENS", None)
        window = real.window_tokens()
        limit = real.reply_limit()
        os.environ["EKG_LLM_MAX_REPLY_TOKENS"] = "8000"
        env = real.reply_limit()
        os.environ.pop("EKG_LLM_MAX_REPLY_TOKENS")
        check("llm: the OpenRouter reply limit (max_completion_tokens) is kept, not discarded; a setting overrides it",
              window[0] == 131_072 and limit == (16_384, "OpenRouter's model list (max_completion_tokens)")
              and env == (8000, "EKG_LLM_MAX_REPLY_TOKENS") and real.reply_limit(5000)[0] == 5000)
    finally:
        real._context, real._settings, _, _, real._REPLY_LIMIT = saved
        real.bind = lambda qm, m=None: "stand-in test-model"


# ---------------------------------------------------------------- 2. tables

PEOPLE_ROWS = [("Alice Chen", "alice@acme.example", "Platform"), ("Carla Diaz", "carla@acme.example", "Data"),
               ("Femi Ade", "femi@acme.example", "Data"), ("Gus Lind", "gus@acme.example", "Platform")]


def people_table(heading_lines=True, per_piece=2):
    """A table in pieces: each starts with its heading line(s), then the header row, then rows."""
    head = "Name | Email | Team"
    lead = "Staff list\nAs of October" if heading_lines else ""
    out = []
    for k in range(0, len(PEOPLE_ROWS), per_piece):
        rows = [" | ".join(r) for r in PEOPLE_ROWS[k:k + per_piece]]
        out.append(("table", "\n".join(([lead] if lead else []) + [head] + rows)))
    return out


ROWS_MAPPING = empty_kv(row_type="Person", name_column="c1", key_column="c2", fields=[{"column": "c2", "field": "work_email"}],
                        links=[{"column": "c3", "link": "MEMBER_OF", "other_type": "Group", "row_is": "from", "split": "none", "fields": []}])


def table_checks(check):
    # heading lines and continuation parts
    g = base.base_graph()
    doc, pieces = base.add_doc(g, "staff.docx", [("Intro", 1, [("text", PROSE * 20)]), ("Staff", 1, people_table())])
    v = view(g)
    d = fill_docs.read(v, v.rec[doc["id"]])
    tbs = fill_docs.tables(d)
    check("tables: a table in two pieces, each starting with heading lines and the repeated header, is one table",
          len(tbs) == 1 and tbs[0].header == ["Name", "Email", "Team"] and tbs[0].lead == "Staff list\nAs of October"
          and len(tbs[0].units) == 2)
    check("tables: its rows are the data rows only (no heading line, no repeated header)",
          [c[0] for _, c in tbs[0].rows()] == [r[0] for r in PEOPLE_ROWS])

    Model.tables = {'c1 = "Name"': ROWS_MAPPING}
    model = Model()
    seen_text = []
    model.facts = lambda p: (seen_text.append(base.shown(p)), {"records": [], "links": []})[1]
    rep, out, tmp = run_fill(g, model, ["table_map", "doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    tprompt = next(p for n, p in model.calls if n == "ekg_table_map")
    check("tables: the mapping prompt shows the heading lines above the header, and the rows count without headers",
          "ABOVE THE TABLE: Staff list\nAs of October" in tprompt and "(4 rows, 3 columns)" in tprompt)
    check("tables: a mapped table is not read again by doc_facts (its prose is)",
          seen_text and all("alice@acme.example" not in s for s in seen_text) and any("MARK-PROSE" in s for s in seen_text)
          and c.get("table rows written") == 4)

    # the same document, the mapping rejected: the table is read as text by doc_facts
    Model.tables = {'c1 = "Name"': empty_kv(shape="none", reason="a layout table")}
    model = Model()
    seen_text = []
    model.facts = lambda p: (seen_text.append(base.shown(p)), {"records": [], "links": []})[1]
    rep, out, tmp = run_fill(g, model, ["table_map", "doc_facts"])
    joined = "\n".join(seen_text)
    check("tables: a table whose mapping is rejected is read by doc_facts as text, every row of it, and the report says so",
          all(r[1] in joined for r in PEOPLE_ROWS) and "MARK-PROSE" in joined
          and rep["kinds"]["doc_facts"].get("tables_read_as_text_after_their_mapping_was_not_accepted") == 1
          and rep["kinds"]["table_map"].get("rejected") == 1)

    # a mapping that fails the checks (a type the schema does not offer) and a failed call: read as text too
    for name, ans in (("fails validation", empty_kv(row_type="Ticket", name_column="none")), ("call fails", {"error": "boom"})):
        Model.tables = {'c1 = "Name"': ans}
        model = Model()
        seen_text = []
        model.facts = lambda p: (seen_text.append(base.shown(p)), {"records": [], "links": []})[1]
        rep, out, tmp = run_fill(g, model, ["table_map", "doc_facts"])
        check(f"tables: a mapping that {name} leaves its table to doc_facts, so nothing is unread",
              all(r[1] in "\n".join(seen_text) for r in PEOPLE_ROWS))

    # dry run: the table is counted for table_map and left out of the windows
    rep, out, _ = run_fill(g, Model(), ["doc_facts", "table_map"], mode="dry")
    check("tables: a dry run builds table_map first and leaves its pieces out of the doc_facts windows",
          rep["kinds"]["table_map"]["to_ask"] == 1 and rep["kinds"]["doc_facts"]["table_pieces_left_to_table_map"] == 2)

    # key/value: each row a field or link of ONE record
    g = base.base_graph()
    kv = ("table", "Profile\nField | Value\nName | Alice Chen\nEmail | alice@acme.example\nTeam | Platform\n"
                   "Title | Staff Engineer\nManager | Bob Kumar\nDesk | 4B")
    bad = ("table", "Field | Value\nName | Hana Ito\nTeam | Data")
    doc, pieces = base.add_doc(g, "alice.docx", [("About", 1, [("text", PROSE * 5), kv]), ("Other", 1, [bad])])
    Model.tables = {
        "Desk": empty_kv(shape="key_value", row_type="Person", entity_name="Alice Chen", reason="a profile",
                         label_fields=[{"label": "Email", "field": "work_email"}, {"label": "Title", "field": "title"},
                                       {"label": "Shoe size", "field": "title"}, {"label": "Name", "field": "full_name"}],
                         label_links=[{"label": "team", "link": "MEMBER_OF", "other_type": "Group", "row_is": "from", "split": "none"},
                                      {"label": "Manager", "link": "REPORTS_TO", "other_type": "Person", "row_is": "from", "split": "none"}]),
        "Hana Ito": empty_kv(shape="key_value", row_type="Person", entity_name="Hana Tanaka",
                             label_links=[{"label": "Team", "link": "MEMBER_OF", "other_type": "Group", "row_is": "from", "split": "none"}]),
    }
    model = Model()
    seen_text = []
    model.facts = lambda p: (seen_text.append(base.shown(p)), {"records": [], "links": []})[1]
    rep, out, tmp = run_fill(g, model, ["table_map", "doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    alice = [r for r in recs if r["type"] == "Person" and r["props"].get("full_name") == "Alice Chen"]
    L = {(lk["type"], base._name(by_id[lk["a"]]), base._name(by_id[lk["b"]])) for lk in links}
    led = {e["subject"] + str(i): e for i, e in enumerate(base.rows(out / "ledger.jsonl")) if e["kind"] == "table_map"}
    check("key/value: a label | value table becomes ONE record with its fields (not one record per row)",
          len(alice) == 1 and alice[0]["props"].get("work_email") == "alice@acme.example"
          and alice[0]["props"].get("title") == "Staff Engineer" and c.get("key/value table: one record written") == 1
          and not any(r["type"] == "Person" and r["props"].get("full_name") in ("Email", "Title", "Desk") for r in recs))
    check("key/value: its link rows become links of that record (team, manager), labels matched regardless of case",
          ("MEMBER_OF", "Alice Chen", "Platform") in L and ("REPORTS_TO", "Alice Chen", "Bob Kumar") in L)
    check("key/value: a row with no field is counted, a label not in the table is dropped and counted",
          c.get("no field for this row: Desk") == 1
          and any(e.get("dropped", {}).get("mapping: label that is not in the table") == 1 for e in led.values()))
    check("key/value: a mapping whose record name is not in the table is rejected, and that table is read as text",
          any(e["status"] == "rejected" and "name is not in the table" in e["reason"] for e in led.values())
          and any("Hana Ito" in s for s in seen_text) and not any("Staff Engineer" in s for s in seen_text))

    # relation tables: each row a relation, with values OF THE LINK; split cells; a column with no field
    g = base.base_graph()
    for who, login in (("Emma Stone", "emma"), ("Mahesh Rao", "mahesh")):
        p = g.rec("Person", "hr", login, {"full_name": who, "work_email": f"{login}@acme.example"}, SCOPE)
        a = g.rec("Account", "github", login, {"login": login, "display_name": who, "tool": "github"}, SCOPE)
        g.link("HAS_ACCOUNT", p, a, "field", link_method="csv", verified=True)
    rel = ("table", "Joined | Person | Team | Role | Commits\n2025-01-06 | Alice Chen | Platform | lead | 42\n"
                    "2025-03-01 | Carla Diaz | Data | member | 17\nsoon | Femi Ade | Data | member | 3")
    appr = ("table", "Module | Approvers | Since\nledger-sync | emma, mahesh | 2026-08-01\npay-auth | mahesh | 2026-10-08")
    base.add_doc(g, "activity.docx", [("Teams", 1, [rel]), ("Approvals", 1, [appr])])
    Model.tables = {
        '"Joined"': empty_kv(row_type="Person", name_column="c2", links=[
            {"column": "c3", "link": "MEMBER_OF", "other_type": "Group", "row_is": "from", "split": "none",
             "fields": [{"column": "c1", "field": "joined_at"}, {"column": "c4", "field": "role"},
                        {"column": "c5", "field": "commit_count"}]}]),
        '"Module"': empty_kv(row_type="Service", name_column="c1", links=[
            {"column": "c2", "link": "APPROVER_FOR", "other_type": "Person", "row_is": "to", "split": ",",
             "fields": [{"column": "c3", "field": "valid_from"}]}]),
    }
    model = Model()
    rep, out, tmp = run_fill(g, model, ["table_map"])
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    led = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"]
    mem = {base._name(by_id[lk["a"]]): lk["props"] for lk in links if lk["type"] == "MEMBER_OF"}
    app = {(base._name(by_id[lk["a"]]), base._name(by_id[lk["b"]])): lk["props"] for lk in links if lk["type"] == "APPROVER_FOR"}
    check("relations: a row that is a relation becomes a link with its own fields from the registry (joined_at, role)",
          mem.get("Alice Chen", {}).get("joined_at") == "2025-01-06T00:00:00Z" and mem["Alice Chen"].get("role") == "lead"
          and mem.get("Carla Diaz", {}).get("role") == "member")
    check("relations: a link field value that does not fit its type is counted, and the link still stands",
          "joined_at" not in mem.get("Femi Ade", {"joined_at": 1}) and c.get("cell skipped: link field MEMBER_OF.joined_at value does not fit its type") == 1)
    check("relations: a column the registry has no field for is dropped from the mapping and every cell of it counted",
          c.get("no field for this column: Commits") == 3
          and any(e.get("dropped", {}).get("mapping: commit_count is not a field of link MEMBER_OF in the registry") == 1 for e in led))
    check("relations: a cell with several values ('emma, mahesh') is split, each resolved to the one person (via their login), "
          "each link with the row's date", set(app) == {("Emma Stone", "ledger-sync"), ("Mahesh Rao", "ledger-sync"), ("Mahesh Rao", "pay-auth")}
          and app[("Emma Stone", "ledger-sync")].get("valid_from") == "2026-08-01T00:00:00Z"
          and app[("Mahesh Rao", "pay-auth")].get("valid_from") == "2026-10-08T00:00:00Z")
    check("relations: the people already in the graph are reused, not created again",
          not any(r["type"] == "Person" and r["props"].get("full_name") in ("emma", "mahesh", "Emma Stone", "Mahesh Rao") for r in recs))
    check("relations: every written record and link validates", not base.all_valid(recs, links, by_id))


# ---------------------------------------------------------------- 3. progress lines

def progress_checks(check):
    g = base.base_graph()
    base.handbook(g)
    fill.HEARTBEAT_SECONDS = 0.05
    lines = []
    model = Model(delay=0.4)
    base.llm.bind = lambda qm, m=None: "stand-in test-model"
    llm.window_tokens = lambda override=None: (20_000, "test")
    llm.reply_limit = lambda override=None: (0, "")
    llm.ask = model.ask
    tmp = Path(tempfile.mkdtemp(prefix="ekg-fill-test-"))
    base.TEMP.append(tmp)
    g.write(tmp)
    fill.run(tmp, base.QM, tmp / "out", ["doc_facts"], mode="run", concurrency=2, log=lines.append)
    fill.HEARTBEAT_SECONDS = 30
    check("progress: while calls are slow a heartbeat line is logged (done / in flight), not silence",
          any("calls in flight" in str(x) for x in lines))
    flushed = []

    class Spy(io.StringIO):
        def flush(self):
            flushed.append(True)
    spy = Spy()
    with redirect_stdout(spy):
        fill._say("a line")
    check("progress: the default log flushes every line (a redirected log stays current)", flushed and spy.getvalue() == "a line\n")
    src = (HERE / "fill_graph.py").read_text(encoding="utf-8") + (HERE / "run_pipeline.py").read_text(encoding="utf-8")
    check("progress: fill_graph.py and run_pipeline.py line-buffer stdout", src.count("line_buffering=True") == 2)


# ---------------------------------------------------------------- 4 and 5. thin names, default status, DOCUMENTED_IN

FACTS2 = ("Emma owns the refund runbook. Zara asked about refunds. Priya reviews exports. Yuki Tanaka is the expert on "
          "ledger exports. Omar (omar@acme.example) is the person to ask about payouts. "
          "Refunds are retried once after a timeout. Refund requests must quote the order id. Every refund needs an order. "
          "Sam will draft the refund FAQ. We decided to keep refunds manual.")


def facts_answer(prompt):
    p = base.StandIn.label_of(prompt, "Emma owns the refund runbook")
    if p is None:
        return {"records": [], "links": []}
    rec = lambda ref, t, name, quote, statement="", fields=(): {
        "ref": ref, "type": t, "name": name, "statement": statement, "quote": quote, "source": p,
        "fields": [{"field": f, "value": v} for f, v in fields]}
    lk = lambda t, a, b, quote, fields=(): {"type": t, "from": a, "to": b, "quote": quote, "source": p,
                                             "fields": [{"field": f, "value": v} for f, v in fields]}
    return {"records": [
        rec("r1", "Person", "Emma", "Emma owns the refund runbook"),
        rec("r2", "Person", "Zara", "Zara asked about refunds"),
        rec("r3", "Person", "Priya", "Priya reviews exports"),
        rec("r4", "Person", "Yuki Tanaka", "Yuki Tanaka is the expert on ledger exports"),
        rec("r5", "Person", "Omar", "Omar (omar@acme.example) is the person to ask", fields=[("work_email", "omar@acme.example")]),
        rec("r6", "Topic", "ledger exports", "the expert on ledger exports"),
        rec("r7", "Topic", "payouts", "the person to ask about payouts"),
        rec("r8", "Knowledge", "", "Refunds are retried once after a timeout", "A refund is retried once after a timeout."),
        rec("r9", "Guideline", "", "Refund requests must quote the order id", "Refund requests quote the order id."),
        rec("r10", "Requirement", "", "Every refund needs an order", "Every refund needs an order."),
        rec("r11", "ActionItem", "", "Sam will draft the refund FAQ", "Draft the refund FAQ."),
        rec("r12", "Decision", "Keep refunds manual", "We decided to keep refunds manual", "Refunds stay manual."),
        rec("r13", "Decision", "Keep refunds manual for now", "decided to keep refunds manual", "Refunds stay manual for now.",
            fields=[("status", "proposed")]),
        rec("r14", "Topic", "refunds", "Zara asked about refunds"),
    ], "links": [
        lk("KNOWS_ABOUT", "r4", "r6", "Yuki Tanaka is the expert on ledger exports", [("role", "expert")]),
        lk("KNOWS_ABOUT", "r5", "r7", "is the person to ask about payouts", [("role", "point_of_contact")]),
        lk("KNOWS_ABOUT", "r1", "r14", "Emma owns the refund runbook"),
        lk("KNOWS_ABOUT", "r2", "r14", "Zara asked about refunds"),
    ]}


def thin_and_status_checks(check):
    g = base.base_graph()
    g.rec("Person", "hr", "emma", {"full_name": "Emma Stone", "work_email": "emma@acme.example"}, SCOPE)
    for n, last in enumerate(("Nair", "Shah")):
        g.rec("Person", "hr", f"priya{n}", {"full_name": f"Priya {last}"}, SCOPE)
    doc, pieces = base.add_doc(g, "refunds.docx", [("Refunds", 1, [("text", FACTS2)])])
    model = Model()
    model.facts = facts_answer
    rep, out, tmp = run_fill(g, model, ["doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    new = collections.defaultdict(list)
    for r in recs:
        new[r["type"]].append(r)
    names = {r["props"].get("full_name") for r in new["Person"]}
    check("thin: a bare first name nothing else identifies is never created, and is counted",
          "Zara" not in names and c.get("Person: named too thinly to create (one bare name; no full name, email, login or id)") == 1)
    check("thin: a full name, or a bare name with an email, is created",
          {"Yuki Tanaka", "Omar"} <= names and next(r for r in new["Person"] if r["props"]["full_name"] == "Omar")["props"].get("work_email") == "omar@acme.example")
    emma = next(r for r in by_id.values() if r["props"].get("full_name") == "Emma Stone")
    check("thin: a bare first name with exactly one matching record in the graph resolves to it (Emma -> Emma Stone)",
          "Emma" not in names and any(lk["type"] == "MENTIONS" and lk["b"] == emma["id"] for lk in links)
          and c.get("Person: resolved to the one existing record", 0) >= 1)
    check("thin: a bare first name two records share is asked as a choice, never guessed or created",
          "Priya" not in names and rep["kinds"].get("entity_choice", {}).get("to_ask") == 1)
    status = {r["type"] + ("/" + r["props"]["title"] if r["props"].get("title") else ""): r["props"].get("status") for r in recs
              if r["type"] in ("Knowledge", "Guideline", "Requirement", "ActionItem", "Decision")}
    check("status: new records get the knowledge writer's default status when the text states none",
          status.get("Knowledge") == "active" and status.get("Guideline") == "active" and status.get("Requirement") == "open"
          and status.get("ActionItem") == "open" and status.get("Decision/Keep refunds manual") == "accepted")
    check("status: a status the text states is kept", status.get("Decision/Keep refunds manual for now") == "proposed")
    know = new["Knowledge"][0] if new["Knowledge"] else None
    check("documented: Knowledge found in a document is DOCUMENTED_IN that document, and keeps EXTRACTED_FROM its piece as origin",
          know and any(lk["type"] == "DOCUMENTED_IN" and lk["a"] == know["id"] and lk["b"] == doc["id"] for lk in links)
          and any(lk["type"] == "EXTRACTED_FROM" and lk["a"] == know["id"] and lk["b"] == pieces[0]["id"] for lk in links))
    check("documented: only where the registry allows it (no DOCUMENTED_IN from a Guideline or Requirement)",
          {by_id[lk["a"]]["type"] for lk in links if lk["type"] == "DOCUMENTED_IN"} == {"Knowledge"})
    check("thin/status: every written record and link validates", not base.all_valid(recs, links, by_id))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(base.QM)
    try:
        sizing_checks(check)
        table_checks(check)
        progress_checks(check)
        thin_and_status_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
