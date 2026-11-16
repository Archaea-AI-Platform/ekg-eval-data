"""Behaviour tests for complete links from tables and prose (stand-in model: no key, no network, no cost).

  1. schema descriptions: the registry's description of every type, link and field reaches the document and
     table prompts as far as the budget allows; the layers least needed give way first.
  2. tables: one row states several links, also between two of its other columns' records (from_column); a link
     cell may hold the other record's key (other_key_field: a pull request's number, a file's path); a link end
     the graph lacks is made from the table like a row; every table's rows are written before any table's links.
  3. tables: a mapping that leaves columns unread, or names a schema gap, has its table read as text too; a fully
     read table is not read again. A mapping reply too long for a mapping (by its token usage, or cut off at the
     reply limit) is a rejection, and the table is read as text; a mapping that lists more entries than the table
     has columns is rejected.
  4. ids: a requirement whose own words cite another requirement's id, in another document, is linked to it after
     every document is read, in code (no model call); a quote that ran on into the next item is no citation; an id
     several records carry is not linked.
  5. usage: each call's token usage is read off query-master's client per call, kept in responses.jsonl, given back
     with a cached answer and summed in the report.

    python -I ekg-eval-data/test_fill_links.py
"""

import asyncio
import collections
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402  (fixtures and the stand-in model)
import test_fill_followups as f1  # noqa: E402
from ekg_adapters import common, extract_facts, extract_schema, extract_tables, fill, fill_budget, llm  # noqa: E402
from ekg_adapters.fill_tasks import View  # noqa: E402

ORIGINAL_ASK = llm.ask   # base.run_fill replaces llm.ask with the stand-in; the usage checks need the real one
SCOPE = base.SCOPE

MERGES = ("Merged | PR / module | Author | Approved by | Change\n"
          "2026-07-08 | #174 / PAY-AUTH | lionel | suzan | Auth retry audit fields\n"
          "2026-07-23 | #181 / PAY-PROMO | alex | suzan | Coupon reservation guard")
COMMITS = ("Date | SHA | Author | PR | File changed\n"
           "2026-07-08 | 7f3c56331b | @lionel | #174 | src/payments/authorization.py\n"
           "2026-07-23 | 87d5b4b8b2 | @alex | #181 | src/pricing/coupons.py")
MODULES = ("Module | Repository | Primary file\n"
           "PAY-AUTH | payments-platform | src/payments/authorization.py\n"
           "PAY-PROMO | payments-platform | src/pricing/coupons.py")
ACTIVITY = ("Module | Most commits | Second most\n"
            "PAY-AUTH | Lionel 42 | Suzan 24\nPAY-PROMO | Alex 23 | Emma 12")
GROUPS = ("Group | Scope / access\n"
          "eng-payments | Payment engineering source write and reviews.\neng-ops | Shipment and warehouse repositories.")
WIDE = ("Key | Value\nalpha | one\nbeta | two")

link = lambda col, lt, other, row_is="from", **kw: {"column": col, "link": lt, "other_type": other, "row_is": row_is,
                                                    "split": kw.pop("split", "none"), "fields": kw.pop("fields", []),
                                                    "from_column": kw.pop("from_column", "row"),
                                                    "other_key_field": kw.pop("other_key_field", "none")}
MERGE_MAP = f1.empty_kv(
    row_type="PullRequest", name_column="none", key_column="c2.1", key_field="number", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
    fields=[{"column": "c1", "field": "merged_at"}, {"column": "c5", "field": "title"}],
    links=[link("c3", "AUTHORED_BY", "Account"), link("c4", "APPROVED_BY", "Account", split=","),
           link("c2.2", "ABOUT", "Service")], schema_gap="")
COMMIT_MAP = f1.empty_kv(
    row_type="Commit", name_column="none", key_column="c2", key_field="sha", statement_column="none", cell_parts=[],
    fields=[{"column": "c1", "field": "committed_at"}],
    links=[link("c3", "AUTHORED_BY", "Account"),
           link("c4", "CONTAINS", "PullRequest", row_is="to", other_key_field="number"),
           link("c5", "CHANGES", "File", other_key_field="path"),
           link("c5", "CHANGES", "File", from_column="c4", other_key_field="path")], schema_gap="")
MODULE_MAP = f1.empty_kv(
    row_type="Service", name_column="c1", key_column="none", key_field="none", statement_column="none", cell_parts=[],
    links=[link("c2", "OWNS_PATH", "Repository", from_column="c2"), link("c3", "IN", "File", row_is="to", from_column="c2", other_key_field="path")],
    schema_gap="")
ACTIVITY_MAP = f1.empty_kv(
    row_type="Service", name_column="c1", key_column="none", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": "", "pattern": r"(.+?)\s+(\d+)"},
                {"column": "c3", "parts": 2, "separator": "", "pattern": r"(.+?)\s+(\d+)"}],
    links=[link("c2.1", "KNOWS_ABOUT", "Person", row_is="to"), link("c3.1", "KNOWS_ABOUT", "Person", row_is="to")],
    schema_gap="the commit counts per person and module (c2.2, c3.2): no link from a Person to a Service has a count field")
GROUP_MAP = f1.empty_kv(row_type="Group", name_column="c1", key_column="none", key_field="none", statement_column="none",
                        cell_parts=[], fields=[{"column": "c2", "field": "text"}], links=[], schema_gap="")
WIDE_MAP = f1.empty_kv(row_type="Group", name_column="c1", key_column="none", key_field="none", statement_column="none",
                       cell_parts=[], fields=[{"column": "c2", "field": "text"}, {"column": "c1", "field": "handle"},
                                              {"column": "c2", "field": "kind"}], links=[], schema_gap="")


def tool_graph():
    g = base.base_graph()
    acct = {}
    for login, full in (("lionel", "Lionel Ray"), ("suzan", "Suzan Kay"), ("alex", "Alex Moe")):
        p = g.rec("Person", "hr", login, {"full_name": full}, SCOPE)
        a = acct[login] = g.rec("Account", "github", login, {"login": login, "display_name": full, "tool": "github"}, SCOPE)
        g.link("HAS_ACCOUNT", p, a, "field", link_method="csv", verified=True)
    return g, acct


# ---------------------------------------------------------------- 1. descriptions in the prompts

def description_checks(check):
    g, _ = tool_graph()
    base.add_doc(g, "activity.pdf", [("Merges", 1, [("table", MERGES)])])
    big = View(list(g.records.values()), [lk for lk, _, _ in g.links.values()],
               budget=fill_budget.sizes(131_072, "test", max_reply_tokens=16_384))
    cat, reg = big.catalog(), common._reg
    doc_text, doc_info = cat.schema_text("documents", big.budget.schema_chars)
    tab_text, tab_info = cat.schema_text("tables", big.budget.schema_chars)
    full = {layer: "full" for layer in extract_schema.DETAIL_LAYERS}
    check("descriptions: with room, the document schema carries every layer of the registry's descriptions in full "
          "(what a type is, what a link means and which way it is stored, what a link's and a record's fields hold)",
          doc_info["detail"] == full and reg.link("OWNED_BY").about in doc_text and reg.record("Requirement").about in doc_text
          and reg.record("Guideline").about in doc_text and reg.record("Decision").fields["decided_at"].about in doc_text)
    check("descriptions: the table schema describes the types and links in full (CHANGES, ABOUT, PullRequest), and names "
          "fields by name and value kind only",
          tab_info["detail"] == {"types": "full", "links": "full"} and reg.link("CHANGES").about in tab_text
          and reg.link("ABOUT").about in tab_text and reg.record("PullRequest").about in tab_text
          and reg.record("PullRequest").fields["number"].about not in tab_text and "number (number)" in tab_text)
    check("descriptions: a table's links list every end a table row may use (a PullRequest ABOUT a Service), not only "
          "a document's", any(line.startswith("- ABOUT:") and "PullRequest" in line.split("->")[0] and "Service" in line
                             for line in tab_text.splitlines()))
    check("descriptions: the report gives the schema's size without and with them",
          tab_info["chars"] > tab_info["chars_without_descriptions"] > 0 and doc_info["chars"] == len(doc_text))
    # a smaller budget: the layers least needed give way first, each cut to its first sentence before it goes
    bare = cat.schema_text("documents", 0)
    mid = cat.schema_text("documents", doc_info["chars_without_descriptions"] + 9_000)[1]["detail"]
    check("descriptions: with no room, the schema is the plain one (names and value kinds only)",
          bare[1]["detail"] == {} and bare[0] == cat.describe(detail={}))
    check("descriptions: with some room, record fields give way before link fields, links and types",
          "types" in mid and "fields" not in mid and list(mid) == [x for x in extract_schema.DETAIL_LAYERS if x in mid])
    check("descriptions: a first sentence ends at the first full stop that ends a clause",
          extract_schema.first_sentence("A ABOUT B: item A is about B; interval. Usually found in text.") == "A ABOUT B: item A is about B;")
    # the prompts themselves
    t = extract_tables.build(big)[0][0]
    prompt = extract_tables.prompt(t, cat)
    shape = {"shape": "rows", "row_type": "PullRequest", "name_column": "none", "key_column": "c2.1", "statement_column": "none",
             "cell_parts": [{"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
             "columns": [{"column": "c3", "type": "Account"}, {"column": "c4", "type": "Account"}, {"column": "c2.2", "type": "Service"}]}
    cprompt, cands = extract_tables.columns_prompt(t, cat, shape)
    first = lambda text: extract_schema.first_sentence(text).rstrip(";.")
    check("descriptions: step 1 lists every record type with its description's first sentence, and never lists rows",
          first(reg.record("PullRequest").about) in prompt and "never lists rows" in prompt and "- Commit:" in prompt
          and "LINK TYPES" not in prompt and "columns" in prompt)
    check("descriptions: step 2 lists only the links between the table's types (AUTHORED_BY, APPROVED_BY, ABOUT a "
          "Service), each with its whole description, and the row type's fields with theirs",
          {"AUTHORED_BY", "APPROVED_BY", "ABOUT"} <= set(cands) and "CHANGES" not in cands and "REPORTS_TO" not in cands
          and reg.link("APPROVED_BY").about in cprompt and reg.record("PullRequest").fields["merged_at"].about in cprompt
          and "from_column" in cprompt and "schema_gap" in cprompt and len(cprompt) < len(extract_tables.catalog_text(cat)[0]))
    dp = extract_facts.prompt(type("T", (), {"context": "DOCUMENT: x", "question": "q", "sources": []})(), cat, [])
    check("descriptions: the document prompt tells Requirement from Guideline by their descriptions and a requirement's own id",
          "is a Requirement; a Guideline is a rule for how the team itself works" in dp and "numbers with an id of its own" in dp
          and reg.record("Requirement").about in dp)


# ---------------------------------------------------------------- 2. and 3. tables

class Tables(f1.Model):
    long_reply = '"Scope / access"'

    def ekg_table_map(self, prompt, schema):
        if self.long_reply and self.long_reply in prompt:   # a reply that went round in circles before it ended
            return {**GROUP_MAP, "_usage": {"prompt_tokens": 9_000, "completion_tokens": 15_000}}
        if '"Value"' in prompt:
            return WIDE_MAP
        return super().ekg_table_map(prompt, schema)


def table_checks(check):
    g, acct = tool_graph()
    # the commit log comes first in the document: its pull request cells must still find the merge log's rows
    base.add_doc(g, "activity.pdf", [("Commits", 1, [("table", COMMITS)]), ("Merges", 1, [("table", MERGES)]),
                                     ("Modules", 1, [("table", MODULES)]), ("Activity", 1, [("table", ACTIVITY)]),
                                     ("Groups", 1, [("table", GROUPS)]), ("Wide", 1, [("table", WIDE)])])
    f1.Model.tables = {'"PR / module"': MERGE_MAP, '"SHA"': COMMIT_MAP, '"Primary file"': MODULE_MAP, '"Most commits"': ACTIVITY_MAP}
    model = Tables()
    rep, out, tmp = f1.run_fill(g, model, ["table_map", "doc_facts"], window=131_072, reply_limit=16_384)
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    name = lambda i: base._name(by_id[i]) or by_id[i]["props"].get("path") or by_id[i]["props"].get("number") or by_id[i]["props"].get("sha") or by_id[i]["props"].get("login")
    L = collections.defaultdict(set)
    for lk in links:
        L[lk["type"]].add((name(lk["a"]), name(lk["b"])))
    prs = [r for r in by_id.values() if r["type"] == "PullRequest"]
    files = {r["props"].get("path"): r for r in by_id.values() if r["type"] == "File" and r["props"].get("path", "").startswith("src/")}
    check("tables: a merge log row links its pull request to its module (ABOUT Service), besides author and approver",
          {("Auth retry audit fields", "PAY-AUTH"), ("Coupon reservation guard", "PAY-PROMO")} <= L["ABOUT"]
          and ("Auth retry audit fields", "lionel") in L["AUTHORED_BY"] and ("Auth retry audit fields", "suzan") in L["APPROVED_BY"])
    check("tables: a commit log row links commit -> file (CHANGES), pull request -> commit (CONTAINS) and commit -> author",
          {("7f3c56331b", "src/payments/authorization.py"), ("87d5b4b8b2", "src/pricing/coupons.py")} <= L["CHANGES"]
          and {("Auth retry audit fields", "7f3c56331b"), ("Coupon reservation guard", "87d5b4b8b2")} <= L["CONTAINS"]
          and ("7f3c56331b", "lionel") in L["AUTHORED_BY"])
    check("tables: a link between two other columns' records of one row: the row's pull request CHANGES the row's file",
          {("Auth retry audit fields", "src/payments/authorization.py"), ("Coupon reservation guard", "src/pricing/coupons.py")} <= L["CHANGES"])
    check("tables: the pull request cell '#181' is matched by its number (other_key_field) to the one record the merge "
          "log made, although the commit log comes first: rows of every table are written before any table's links",
          len(prs) == 2 and sorted(p["props"].get("number") for p in prs) == [174, 181]
          and all(p["props"].get("title") for p in prs))
    check("tables: a link end the graph lacks (a File named by its path) is made from the table once, and counted",
          set(files) == {"src/payments/authorization.py", "src/pricing/coupons.py"}
          and c.get("File: created from a table row (no such record from its tool yet)") == 2)
    check("tables: from_column with row_is 'to': the module table's file is IN the row's repository",
          {("src/payments/authorization.py", "payments-platform"), ("src/pricing/coupons.py", "payments-platform")} <= L["IN"]
          and {("PAY-AUTH", "payments-platform")} <= L["OWNS_PATH"])
    mod = next(e for e in base.rows(out / "ledger.jsonl") if (e.get("mapping") or {}).get("row_type") == "Service"
               and "OWNS_PATH" in json.dumps(e["mapping"]))
    check("tables: a link whose from_column names its own column starts at the row (counted), as the model meant",
          mod["dropped"].get("mapping: from_column named the link's own column, read as the row") == 1)
    check("tables: every written record and link validates", not base.all_valid(recs, links, by_id))
    acc = {row["table"].split(": ")[-1]: row for row in rep["kinds"]["table_map"]["acceptance"]}
    shown = [base.shown(p) for n, p in model.calls if n == "ekg_doc_facts"]
    read_as_text = lambda s: any(s in x for x in shown)
    check("tables: a fully read table (every column a name, key, field or link) is not read again as text",
          acc["Modules"]["status"] == "accepted" and not read_as_text("src/pricing/coupons.py | ")
          and acc["Commits"]["status"] == "accepted")
    check("tables: a mapping naming a schema gap (counts no link can hold) is accepted for what it can store, reported, "
          "and its table is read as text as well",
          acc["Activity"]["status"] == "accepted, also read as text" and "no link from a Person" in acc["Activity"]["also_text"]["schema_gap"]
          and set(acc["Activity"]["also_text"]["unread_columns"]) == {"c2.2", "c3.2"} and read_as_text("Lionel 42"))
    check("tables: the merge log, every column part read (its module part now linked), is not read again as text",
          acc["Merges"]["status"] == "accepted" and not read_as_text("#174 / PAY-AUTH"))
    check("tables: a mapping reply over the tokens a mapping may take is a rejection (not an error), and the table is read as text",
          acc["Groups"]["status"] == "rejected" and "mapping reply too long" in acc["Groups"]["reason"]
          and "15,000 tokens" in acc["Groups"]["reason"] and read_as_text("eng-ops | Shipment"))
    check("tables: a mapping that lists more entries than the table has columns is rejected (a mapping names columns)",
          acc["Wide"]["status"] == "rejected" and "more entries than the table has columns" in acc["Wide"]["reason"])
    check("tables: the report has one acceptance row per table, with its row type and links",
          len(acc) == 6 and acc["Commits"]["row_type"] == "Commit" and acc["Commits"]["links"] == ["AUTHORED_BY", "CHANGES", "CONTAINS"])

    # a reply cut off at the reply limit is a rejection too, never asked again as an error
    g, _ = tool_graph()
    base.add_doc(g, "groups.pdf", [("Groups", 1, [("table", GROUPS)])])
    model = Tables()
    model.long_reply = None
    model.ekg_table_map = lambda prompt, schema: {"error": "window full: prompt 9416 + reply 16384 tokens"}
    rep, out, tmp = f1.run_fill(g, model, ["table_map", "doc_facts"], window=131_072, reply_limit=16_384)
    e = [x for x in base.rows(out / "ledger.jsonl") if x["kind"] == "table_map"][0]
    check("tables: a mapping reply cut off at the reply limit is rejected (stands next run) and the table is read as text",
          e["status"] == "rejected" and "ran into the limit" in e["reason"]
          and any("eng-ops | Shipment" in base.shown(p) for n, p in model.calls if n == "ekg_doc_facts"))
    # a mapping call still running after Budget.mapping_seconds is given up: a rejection, read as text
    g, _ = tool_graph()
    base.add_doc(g, "groups.pdf", [("Groups", 1, [("table", GROUPS)])])
    model = Tables()
    model.long_reply = None

    async def slow(prompt, name, schema):
        model.calls.append((name, prompt))
        if name == "ekg_table_map":
            await asyncio.sleep(5)
        return getattr(model, name)(prompt, schema)
    model.ask = slow
    saved = fill_budget.Budget.mapping_seconds
    fill_budget.Budget.mapping_seconds = property(lambda self: 1)
    try:
        rep, out, tmp = f1.run_fill(g, model, ["table_map", "doc_facts"], window=131_072, reply_limit=16_384)
    finally:
        fill_budget.Budget.mapping_seconds = saved
    e = [x for x in base.rows(out / "ledger.jsonl") if x["kind"] == "table_map"][0]
    check("tables: a mapping call past its time (the token guard at a slow pace) is given up: rejected, read as text",
          e["status"] == "rejected" and "no answer after 1 seconds" in e["reason"]
          and any("eng-ops | Shipment" in base.shown(p) for n, p in model.calls if n == "ekg_doc_facts")
          and fill_budget.sizes(131_072, max_reply_tokens=16_384).mapping_seconds == 307)
    check("tables: a pattern that fails a sample cell only by its character classes is read by the text it writes "
          "between its groups ('#(\\d+) / (\\w+)' on '#174 / PAY-AUTH': ' / '); other patterns are not guessed at",
          extract_tables._pattern_separator(r"#(\d+) / (\w+)", 2) == " / "
          and extract_tables._pattern_separator(r"(.+?)\s+(\d+)", 2) is None
          and extract_tables._pattern_separator(r"(\S+) (.+) / (.+)", 3) is None
          and extract_tables.split_cell("#174 / PAY-AUTH", [0, 2, " / ", ""]) == ["#174", "PAY-AUTH"])
    check("tables: a placeholder cell ('—') is no value, not a cell that fails the split",
          extract_tables.split_cell("—", [0, 2, "", r"(.+?)\s+(\d+)"]) == ["", ""]
          and extract_tables.split_cell("Noah", [0, 2, "", r"(.+?)\s+(\d+)"]) is None)
    check("tables: the guard is three quarters of the reply the provider allows",
          fill_budget.sizes(131_072, max_reply_tokens=16_384).mapping_reply_tokens == 12_288
          and extract_tables.too_long({"_usage": {"completion_tokens": 900}}, fill_budget.sizes(131_072, max_reply_tokens=16_384)) is None)


# ---------------------------------------------------------------- 4. ids one requirement cites of another

ORDERS = "ORD-01 Orders are accepted once, even after a double click or a network retry, and keep one placement time."
PAYMENTS = ("PAY-02 Capture happens only after ORD-01 is met for the order, never at warehouse acceptance alone.\n\n"
            "PAY-03 Refunds use the original tender shares. PAY-04 Refunds are reported to Finance in integer cents.")
DUPS = "DUP-1 One duplicated id in this document, for every order line and package that is shipped to a customer."
DUPS2 = "DUP-1 The same id again in another document, so the id is carried by two different records here."
CITES_DUP = "PAY-09 Settlement waits for DUP-1 and keeps the ledger entry open until the processor answers."


def requirement_facts(prompt):
    recs = []
    for sentence, name in ((ORDERS, "ORD-01 Orders accepted once"),
                           ("PAY-02 Capture happens only after ORD-01 is met for the order", "PAY-02 Capture after acceptance"),
                           ("PAY-03 Refunds use the original tender shares. PAY-04 Refunds are reported", "PAY-03 Refund tender shares"),
                           ("PAY-04 Refunds are reported to Finance in integer cents.", "PAY-04 Refunds in cents"),
                           (DUPS, "DUP-1 duplicated id"), (DUPS2, "DUP-1 duplicated id again"),
                           (CITES_DUP, "PAY-09 Settlement waits"),
                           ("Capture happens only after ORD-01 is met for the order, never", "PAY-02 Capture once accepted")):
        p = base.StandIn.label_of(prompt, sentence)
        if p:
            recs.append({"ref": f"r{len(recs) + 1}", "type": "Requirement", "name": name, "statement": name.split(" ", 1)[1] + ".",
                         "quote": sentence, "source": p, "fields": []})
    return {"records": recs, "links": []}


def id_checks(check):
    g = base.base_graph()
    base.add_doc(g, "orders.pdf", [("Rules", 1, [("text", ORDERS)])])
    _, pay = base.add_doc(g, "payments.pdf", [("Rules", 1, [("text", PAYMENTS), ("text", CITES_DUP)])])
    base.add_doc(g, "dups.pdf", [("Rules", 1, [("text", DUPS)])])
    base.add_doc(g, "dups2.pdf", [("Rules", 1, [("text", DUPS2)])])
    model = f1.Model()
    model.facts = requirement_facts
    rep, out, tmp = f1.run_fill(g, model, ["doc_facts"], window=131_072, reply_limit=16_384)
    recs, links, by_id = base.written(out, tmp)
    title = lambda i: by_id[i]["props"].get("title") or by_id[i]["type"]
    refs = [(by_id[lk["a"]], title(lk["b"]), lk) for lk in links if lk["type"] == "LINKS_TO"]
    idr = rep["id_references"]
    check("ids: a requirement citing another document's requirement by id is linked to it: its passage LINKS_TO the cited "
          "Requirement (the registry has no Requirement -> Requirement reference link), method text_rule, after all documents",
          [(a["type"], b) for a, b, _ in refs] == [("Piece", "ORD-01 Orders accepted once")]
          and refs[0][2]["props"]["method"] == "text_rule" and refs[0][0]["id"] in {p["id"] for p in pay}
          and idr.get("Piece LINKS_TO Requirement: written") == 1)
    check("ids: a quote that ran on from one numbered item into the next is no citation (PAY-03 does not cite PAY-04)",
          idr.get("the quote runs on into the cited item's own words (two items quoted together): not a citation") == 1)
    check("ids: an id two records carry (one per document) is not linked, and is counted",
          idr.get("cited ids several records carry: not linked") == 1
          and idr.get("ids that several records carry (one per document, not linked)") == 1)
    check("ids: a record's own id is the one its name starts with",
          idr.get("records with an id of their own") == 7)
    pay02 = [r for r in recs if r["type"] == "Requirement" and (r["props"].get("title") or "").startswith("PAY-02")]
    check("ids: a numbered item read twice in its document (another quote, another wording) is one record, counted",
          len(pay02) == 1 and rep["written"]["documents_and_tables"].get(
              "Requirement: the same numbered item again in its document, the record made for it") == 1)
    cat = View(list(g.records.values()), [lk for lk, _, _ in g.links.values()]).catalog()
    check("ids: a link with an `any` end is offered for the types a document states (a Requirement ABOUT a Service)",
          "Requirement" in cat.links["ABOUT"][0] and "Service" in cat.links["ABOUT"][1] and "Person" not in cat.links["ABOUT"][0])
    check("ids: no model call was made for the references (every call is a document reading)",
          all(n == "ekg_doc_facts" for n, _ in model.calls))
    check("ids: every written record and link validates", not base.all_valid(recs, links, by_id))


# ---------------------------------------------------------------- 5. token usage

class Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def usage_checks(check):
    from app.services.llm import client as qm_client
    llm._count_usage()

    class Fake(qm_client.LLMClient):
        def __init__(self):
            pass

        async def explain(self, prompt, **kw):   # two attempts of one call (a re-ask), as query-master's client may make
            self._record_token_usage(Resp({"usage": {"prompt_tokens": 70, "completion_tokens": 30,
                                                     "completion_tokens_details": {"reasoning_tokens": 20}}}))
            self._record_token_usage(Resp({"usage": {"prompt_tokens": 70, "completion_tokens": 12}}))
            return {"answer": prompt}
    saved = llm._get_client, llm.MODEL, llm.ask
    llm._get_client, llm.MODEL, llm.ask = (lambda model=None: Fake()), None, ORIGINAL_ASK
    tmp = Path(tempfile.mkdtemp(prefix="ekg-usage-test-"))
    try:
        got = asyncio.run(llm.ask("hello", "ekg_test", {}))
        check("usage: llm.ask gives each call's tokens as query-master's client read them off the replies (attempts summed)",
              got == {"answer": "hello", "_usage": {"prompt_tokens": 140, "completion_tokens": 42, "reasoning_tokens": 20, "attempts": 2}})
        r = fill.Responses(tmp / "responses.jsonl", "m")
        first = asyncio.run(r.ask("t1", "hello", "ekg_test", {}))
        r.close()
        line = json.loads(open(tmp / "responses.jsonl", encoding="utf-8").readline())
        check("usage: responses.jsonl keeps the usage beside the answer, never in it",
              line["response"] == {"answer": "hello"} and line["usage"]["completion_tokens"] == 42 and first["_usage"]["prompt_tokens"] == 140)
        check("usage: the run's report sums the tokens by step", r.usage_report()["total"].get("completion_tokens") == 42
              and r.usage_report()["by_step"]["ekg_test"]["calls_with_usage"] == 1)
        again = fill.Responses(tmp / "responses.jsonl", "m")
        cached = asyncio.run(again.ask("t1", "hello", "ekg_test", {}))
        again.close()
        check("usage: a cached answer comes back with the usage of the call that made it (no call, nothing summed)",
              cached["_usage"]["completion_tokens"] == 42 and again.calls == 0 and again.usage_report()["total"] == {})
    finally:
        llm._get_client, llm.MODEL, llm.ask = saved
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(base.QM)
    try:
        description_checks(check)
        table_checks(check)
        id_checks(check)
        usage_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
