"""Behaviour tests for the second round of fixes to the LLM stage (stand-in model: no key, no network, no cost).

  3. one person, one record: composites split before a name is used, every identifying value resolves first,
     the richest table writes first, and Persons that share an email, login or id are reported.

  1. completeness is guaranteed by code: what an answer left uncovered is re-read in smaller follow-up calls, at
     most COVERAGE_ROUNDS times; what still yields nothing is reported as read, nothing kept; per-document
     coverage in the report. Quotes: PDF-text normalisation, and a quote cited to the wrong passage of the
     window is cited to the one passage that holds it.
  2. a table that is an export of another tool (a merge log, a commit log) maps to that tool's record type,
     matched by its key field first; composite cells split by a separator or pattern the mapping states,
     checked on the sample rows; a statement column for stated types.

    python -I ekg-eval-data/test_fill_round2.py
"""

import collections
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402  (fixtures and the stand-in model)
import test_fill_followups as f1  # noqa: E402
from ekg_adapters import common, extract_facts, fill, fill_budget  # noqa: E402
from ekg_adapters.fill_tasks import Item  # noqa: E402

SCOPE = base.SCOPE
FILLER = "This paragraph only explains the background of the product in ordinary words and states no rule. "


def req(n):
    return f"REQ-{n:02d} The system must keep audit record number {n} for every order it changes, and show it on request."


def spec_doc(g):
    """Six pages: page 1 an intro, pages 2-6 three requirements each, one paragraph per requirement; page 6 also
    ends with a boilerplate paragraph."""
    pages = [[FILLER * 2]]
    for p in range(5):
        pages.append([req(p * 3 + k) for k in range(1, 4)])
    pages[-1].append("Page footer: confidential, printed copies are not controlled. " * 2)
    return base.add_doc(g, "spec.pdf", [(f"page {i + 1}", 1, [("text", "\n\n".join(ps))]) for i, ps in enumerate(pages)])


class Lazy(base.StandIn):
    """Answers like a model that stops early: on a first reading, only the first requirement it sees; on a
    follow-up reading, every requirement shown (with the label of the passage BEFORE it, as models often do)."""

    def __init__(self, follow=True, wrong_label=False):
        super().__init__()
        self.follow, self.wrong_label = follow, wrong_label

    def ekg_doc_facts(self, prompt, schema):
        follow_up = "NOT COVERED YET" in prompt
        text = base.shown(prompt)
        blocks = re.split(r"\n(?=\[[A-Z]\d+\] )", text)
        labels = [re.match(r"\s*\[([A-Z]\d+)\]", b).group(1) for b in blocks if re.match(r"\s*\[([A-Z]\d+)\]", b)]
        found = []
        for b in blocks:
            m = re.match(r"\s*\[([A-Z]\d+)\]", b)
            for q in re.findall(r"REQ-\d\d The system must keep audit record number \d+ for every order it changes, and show it on request", b):
                found.append((m.group(1), q))
        if not follow_up:
            found = found[:1]
        elif not self.follow:
            found = []
        recs = []
        for i, (lab, q) in enumerate(found):
            if self.wrong_label and follow_up and labels.index(lab) > 0:
                lab = labels[labels.index(lab) - 1]
            recs.append({"ref": f"r{i}", "type": "Requirement", "name": q[:6], "statement": q, "fields": [],
                         "quote": q, "source": lab})
        return {"records": recs, "links": []}


def coverage_checks(check):
    g = base.base_graph()
    doc, pieces = spec_doc(g)
    model = Lazy()
    rep, out, tmp = f1.run_fill(g, model, ["doc_facts"], window=80_000)   # one window: the reply reads 12,000 characters
    recs, links, by_id = base.written(out, tmp)
    got = sorted(r["props"].get("title") for r in recs if r["type"] == "Requirement")
    calls = [p for n, p in model.calls if n == "ekg_doc_facts"]
    first, follow = [p for p in calls if "NOT COVERED YET" not in p], [p for p in calls if "NOT COVERED YET" in p]
    led = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "doc_facts"]
    cov = led[0]["coverage"]
    check("coverage: a model that stops after one item still yields every requirement: code re-reads what it left uncovered",
          got == [f"REQ-{n:02d}" for n in range(1, 16)] and len(first) == 1 and follow)
    check("coverage: the follow-up shows only the paragraphs that yielded nothing (REQ-01's paragraph is not read again)",
          "REQ-01" not in base.shown(follow[0]) and "REQ-02" in base.shown(follow[0]) and "REQ-15" in base.shown(follow[0]))
    check("coverage: each round is recorded in the ledger (passages, paragraphs, characters, calls, items kept)",
          cov["rounds"] and cov["rounds"][0]["items_kept"] == 14 and {"passages", "paragraphs", "chars", "calls"} <= set(cov["rounds"][0]))
    check("coverage: what still yields nothing after the follow-ups is counted as read, nothing kept (the intro, the footer)",
          cov["paragraphs_read_nothing_kept"] >= 1 and "P1" in cov["passages_read_nothing_kept"] and len(cov["rounds"]) <= extract_facts.COVERAGE_ROUNDS)
    row = rep["kinds"]["doc_facts"]["coverage_by_document"]["spec"]
    check("coverage: the report gives per document the passages with items / passages read and items per 1,000 characters",
          row["passages"] == 6 and row["passages_with_items"] == 5 and row["items_per_1000_chars"] > 0
          and row["items_from_follow_ups"] == 14)

    # a window larger than half the room: the follow-ups are smaller calls, each at most half the room
    g = base.base_graph()
    doc, pieces = spec_doc(g)
    model = Lazy()
    rep, out, tmp = f1.run_fill(g, model, ["doc_facts"], window=18_000)
    e = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "doc_facts"]
    room = fill_budget.sizes(18_000).room(rep["kinds"]["doc_facts"]["prompt_chars_max"] - rep["kinds"]["doc_facts"]["text_chars_per_window_max"])
    follow = [p for n, p in model.calls if "NOT COVERED YET" in p]
    check("coverage: follow-up readings are smaller windows than the first (several calls when the uncovered text is large)",
          any(r["calls"] > 1 for x in e for r in x["coverage"]["rounds"])
          and sorted(r["props"]["title"] for r in base.written(out, tmp)[0] if r["type"] == "Requirement") == [f"REQ-{n:02d}" for n in range(1, 16)]
          and room > 0 and follow)

    # a model that never adds anything on a follow-up: one round (a second would send the very same passages), counted
    g = base.base_graph()
    spec_doc(g)
    model = Lazy(follow=False)
    rep, out, tmp = f1.run_fill(g, model, ["doc_facts"])
    cov = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "doc_facts"][0]["coverage"]
    check("coverage: a follow-up round that keeps nothing ends the follow-ups (the next would ask the very same passages "
          "again); what is left is counted, never looped on",
          len(cov["rounds"]) == 1 and all(r["items_kept"] == 0 for r in cov["rounds"])
          and cov["passages_with_items"] == 1 and cov["paragraphs_read_nothing_kept"] >= 14)

    # follow-up answers citing the passage next to the right one: cited where the quote really is
    g = base.base_graph()
    doc, pieces = spec_doc(g)
    model = Lazy(wrong_label=True)
    rep, out, tmp = f1.run_fill(g, model, ["doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    e = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "doc_facts"][0]
    piece_of = {r["props"]["title"]: lk["b"] for lk in links for r in [by_id[lk["a"]]] if lk["type"] == "EXTRACTED_FROM"}
    check("quotes: a quote cited to the wrong passage is kept, cited to the one passage that holds it, and counted",
          len(piece_of) == 15 and e.get("fixed", {}).get("quote found in another passage of the window: cited there", 0) >= 10
          and piece_of["REQ-04"] == pieces[2]["id"] and piece_of["REQ-15"] == pieces[5]["id"])

    # the normalisation PDF text needs: ligatures, a word hyphenated at a line end, soft hyphens, odd spacing
    it = Item("P1", None, "page 3", "Refunds are re-\nserved for the ﬁnal  settle­ment of an\norder.")
    other = Item("P2", None, "page 4", "Refunds are reserved for the final settlement of an order.")
    q = lambda s, items=(it,): fill.find_quote(s, list(items)) is not None
    check("quotes: PDF text matches word for word after NFKC, joined line-end hyphens, soft hyphens and spacing",
          q("refunds are reserved for the final settlement of an order") and q("re-served for the final"))
    check("quotes: a changed or added word still fails", not q("refunds are reserved for the first settlement")
          and not q("refunds are always reserved"))
    check("quotes: a quote two passages hold is not moved to either (ambiguous), the named one keeps it",
          fill.locate("refunds are reserved for the final settlement", None, [it, other]) == (None, False)
          and fill.locate("refunds are reserved for the final settlement", other, [it, other]) == (other, False))


# ---------------------------------------------------------------- 2. tables of another tool's records

MERGES = ("Merged | PR / module | Author | Approved by | Change\n"
          "2026-07-08 | #174 / PAY-AUTH | lionel | suzan | Auth retry audit fields\n"
          "2026-07-23 | #181 / PAY-PROMO | alex | emma, suzan | Coupon reservation guard\n"
          "2026-08-02 | #187 / OPS-LABEL | noahp | mayac | Label lifecycle callbacks")
COMMITS = ("Date | SHA | Author | PR | File changed\n"
           "2026-07-08 | 7f3c56331b | @lionel | #174 | src/payments/authorization.py\n"
           "2026-07-23 | 87d5b4b8b2 | @alex | #181 | src/pricing/coupons.py")
ACTIVITY = ("Module | Most commits | Second most\n"
            "PAY-AUTH | Lionel Ray 42 | Suzan Kay 24\nPAY-CAP | Suzan Kay 31 | Emma Stone 19")
CHANGES = ("Effective | Change | Supporting record\n"
           "2026-10-03 | Lionel moved from Payments to Order Management. | PR #221\n"
           "2026-10-08 | Suzan became the payment-authorization steward. | PR #225")
MERGE_MAP = f1.empty_kv(
    row_type="PullRequest", name_column="none", key_column="c2.1", key_field="number", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
    fields=[{"column": "c1", "field": "merged_at"}, {"column": "c5", "field": "title"}],
    links=[{"column": "c3", "link": "AUTHORED_BY", "other_type": "Account", "row_is": "from", "split": "none", "fields": []},
           {"column": "c4", "link": "APPROVED_BY", "other_type": "Person", "row_is": "from", "split": ",", "fields": []}])
COMMIT_MAP = f1.empty_kv(
    row_type="Commit", name_column="none", key_column="c2", key_field="sha", statement_column="none", cell_parts=[],
    fields=[{"column": "c1", "field": "committed_at"}],
    links=[{"column": "c3", "link": "AUTHORED_BY", "other_type": "Account", "row_is": "from", "split": "none", "fields": []}])
ACTIVITY_MAP = f1.empty_kv(
    row_type="Service", name_column="c1", key_column="none", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": "", "pattern": r"(.+?)\s+(\d+)"},
                {"column": "c3", "parts": 2, "separator": "", "pattern": r"(.+?)\s+(\d+)"}],
    links=[{"column": "c2.1", "link": "KNOWS_ABOUT", "other_type": "Person", "row_is": "to", "split": "none", "fields": []},
           {"column": "c3.1", "link": "KNOWS_ABOUT", "other_type": "Person", "row_is": "to", "split": "none", "fields": []}])
CHANGES_MAP = f1.empty_kv(
    row_type="Knowledge", name_column="none", key_column="none", key_field="none", statement_column="c2", cell_parts=[],
    fields=[{"column": "c1", "field": "effective_from"}])


def tool_graph():
    g = base.base_graph()
    acct = {}
    for login, full in (("lionel", "Lionel Ray"), ("suzan", "Suzan Kay"), ("emma", "Emma Stone"), ("alex", "Alex Moe")):
        p = g.rec("Person", "hr", login, {"full_name": full}, SCOPE)
        a = acct[login] = g.rec("Account", "github", login, {"login": login, "display_name": full, "tool": "github"}, SCOPE)
        g.link("HAS_ACCOUNT", p, a, "field", link_method="csv", verified=True)
    g.rec("Person", "hr", "mayac", {"full_name": "Maya Chen"}, SCOPE)   # a person with no account
    pr = g.rec("PullRequest", "github", "PR_node_174", {"number": 174, "title": "Auth retry audit fields", "state": "merged"}, SCOPE)
    cm = g.rec("Commit", "git", "7f3c56331b2d9a0e44f1c3b7aa0d5e6f71234567", {"sha": "7f3c56331b2d9a0e44f1c3b7aa0d5e6f71234567",
                                                                          "message": "Auth retry audit fields"}, SCOPE)
    return g, pr, cm, acct


def tool_table_checks(check):
    g, pr174, commit, acct = tool_graph()
    base.add_doc(g, "activity.pdf", [("Merges", 1, [("table", MERGES)]), ("Commits", 1, [("table", COMMITS)]),
                                     ("Activity", 1, [("table", ACTIVITY)]), ("Changes", 1, [("table", CHANGES)])])
    f1.Model.tables = {'"PR / module"': MERGE_MAP, '"SHA"': COMMIT_MAP, '"Most commits"': ACTIVITY_MAP, '"Supporting record"': CHANGES_MAP}
    model = f1.Model()
    rep, out, tmp = f1.run_fill(g, model, ["table_map", "doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    led = {e["status"]: e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"}
    tprompt = next(p for n, p in model.calls if n == "ekg_table_shape")
    cprompt = next(p for n, p in model.calls if n == "ekg_table_map" and '"PR / module"' in p)
    prs = {r["props"].get("number"): r for r in by_id.values() if r["type"] == "PullRequest"}
    L = collections.defaultdict(set)
    for lk in links:
        L[lk["type"]].add((lk["a"], lk["b"]))
    check("tool tables: step 1 lists the other tools' record types (PullRequest); step 2 the links between the table's "
          "types (AUTHORED_BY, APPROVED_BY)",
          "export of another tool" in tprompt and "- PullRequest:" in tprompt and "cell_parts" in tprompt
          and "- AUTHORED_BY:" in cprompt and "- APPROVED_BY:" in cprompt)
    check("tool tables: a merge log maps to PullRequest rows; the composite 'PR / module' cell is split by the stated separator",
          set(prs) == {174, 181, 187} and rep["kinds"]["table_map"].get("accepted") == 4)
    check("tool tables: a row matches the tool's own record by its key field first (#174 is the existing GitHub PR, not a copy)",
          prs[174]["id"] == pr174["id"] and sum(1 for r in by_id.values() if r["type"] == "PullRequest" and r["props"].get("number") == 174) == 1
          and prs[174]["props"].get("merged_at") == "2026-07-08T00:00:00Z")
    check("tool tables: a PR the graph does not hold yet is created from the row, and counted as such",
          prs[181]["props"].get("title") == "Coupon reservation guard"
          and c.get("PullRequest: created from a table row (no such record from its tool yet)") == 2)
    check("tool tables: AUTHORED_BY goes to the author's Account; APPROVED_BY needs an Account, so a Person is followed to their one account",
          (pr174["id"], acct["lionel"]["id"]) in L["AUTHORED_BY"] and (pr174["id"], acct["suzan"]["id"]) in L["APPROVED_BY"]
          and (prs[181]["id"], acct["emma"]["id"]) in L["APPROVED_BY"] and (prs[181]["id"], acct["suzan"]["id"]) in L["APPROVED_BY"])
    check("tool tables: a link end the graph lacks is made from the table like a row (Account 'noahp'), counted; "
          "'mayac' read as a Person is one bare word: never made a Person of its own, counted, not dropped",
          c.get("Account: created from a table row (no such record from its tool yet)") == 1
          and any(by_id[b]["props"].get("login") == "noahp" for _, b in L["AUTHORED_BY"])
          and c.get("Person: a bare first name that no record could be, not linked (not created)") == 1
          and not any(r["type"] == "Person" and r["props"].get("full_name") == "mayac" for r in recs))
    check("tool tables: the module half of the split cell, with no field, is counted",
          c.get("no field for this column: PR / module (part 2)") == 3)
    commits = [r for r in by_id.values() if r["type"] == "Commit"]
    check("tool tables: a commit log row finds the existing commit by its short sha (the one stored sha that starts with it)",
          len([r for r in commits if r["props"]["sha"].startswith("7f3c56331b")]) == 1
          and any(r["props"].get("sha") == "87d5b4b8b2" for r in commits) and (commit["id"], acct["lionel"]["id"]) in L["AUTHORED_BY"])
    svc = {r["props"].get("name"): r for r in by_id.values() if r["type"] == "Service"}
    know = {(base._name(by_id[a]), base._name(by_id[b])) for a, b in L["KNOWS_ABOUT"]}
    check("tool tables: composite 'Lionel Ray 42' cells split by the stated pattern: the person is linked, the count counted",
          {("Lionel Ray", "PAY-AUTH"), ("Suzan Kay", "PAY-AUTH"), ("Suzan Kay", "PAY-CAP"), ("Emma Stone", "PAY-CAP")} <= know
          and c.get("no field for this column: Most commits (part 2)") == 2 and "PAY-AUTH" in svc)
    kn = [r for r in recs if r["type"] == "Knowledge"]
    check("tool tables: a statement column makes stated records (Knowledge) with their origin in the table piece and the default status",
          len(kn) == 2 and all(r["props"].get("status") == "active" for r in kn)
          and kn[0]["props"].get("effective_from") in ("2026-10-03T00:00:00Z", "2026-10-08T00:00:00Z")
          and sum(1 for lk in links if lk["type"] == "EXTRACTED_FROM" and by_id[lk["a"]]["type"] == "Knowledge") == 2)
    check("tool tables: every written record and link validates", not base.all_valid(recs, links, by_id))
    merge_entry = next(e for e in base.rows(out / "ledger.jsonl")
                       if e["kind"] == "table_map" and (e.get("mapping") or {}).get("row_type") == "PullRequest")
    check("tool tables: a mapping that leaves a column unread (the merge log's module part) has its table read as text too",
          merge_entry["mapping"].get("also_text", {}).get("unread_columns") == ["c2.2"]
          and any("Auth retry audit fields" in base.shown(p) for n, p in model.calls if n == "ekg_doc_facts"))

    # a split that does not parse every sample row rejects the mapping, and the table is read as text
    g, *_ = tool_graph()
    base.add_doc(g, "activity.pdf", [("Activity", 1, [("table", ACTIVITY + "\nOPS-FULF | Noah | Maya 11")])])
    f1.Model.tables = {'"Most commits"': ACTIVITY_MAP}
    model = f1.Model()
    seen = []
    model.facts = lambda p: (seen.append(base.shown(p)), {"records": [], "links": []})[1]
    rep, out, tmp = f1.run_fill(g, model, ["table_map", "doc_facts"])
    led = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"]
    check("tool tables: a cell split the sample rows do not all follow rejects the mapping and asks again with the reason; "
          "when the corrected one still misses that cell, the split is kept for the rows it parses, the cell that does "
          "not split is listed and counted, and the table is read as text too",
          led and led[0]["status"] == "accepted" and "does not parse the sample row cell \"Noah\"" in led[0]["retried_after"]
          and led[0]["mapping"]["also_text"]["cells_that_do_not_split"] == ["c2: Noah"]
          and rep["written"]["documents_and_tables"].get("cell skipped: c2 does not split as the mapping states") == 1
          and any("Lionel Ray 42" in s for s in seen))

    # a tool type with neither its name nor a key field cannot be matched: rejected
    g, *_ = tool_graph()
    base.add_doc(g, "activity.pdf", [("Merges", 1, [("table", MERGES)])])
    f1.Model.tables = {'"PR / module"': {**MERGE_MAP, "key_field": "merged_at", "fields": [{"column": "c1", "field": "merged_at"}]}}
    rep, out, tmp = f1.run_fill(g, f1.Model(), ["table_map"])
    led = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"]
    check("tool tables: a date is no key; without its name or a key field a tool row is rejected",
          led[0]["status"] == "rejected" and "needs its name or its key field" in led[0]["reason"])


# ---------------------------------------------------------------- one person, one record

STEWARDS = ("Module | Current steward | Manager | Effective\n"
            "PAY-AUTH | Suzan / @suzan | Mahesh | 2026-10-08\nPAY-REF | Emma / @emma | Mahesh | 2026-08-01")
PERIODS = ("Period | Person / module | Record\n"
           "From Oct 08 | Suzan · PAY-AUTH | Current accountable engineer.\nOct 03-07 | Mahesh · PAY-AUTH | Interim escalation.")
LDAP = ("ID / employee | Role / team | Manager | GitHub / email\n"
        "E002 Mahesh eng-all | Engineering Manager | Mack | @mahesh mahesh@acme.com\n"
        "E004 Suzan eng-payments | Senior Software Engineer | Mahesh | @suzan suzan@acme.com\n"
        "E005 Emma eng-payments | Software Engineer | Mahesh | @emma emma@acme.com")
STEWARDS_BAD = f1.empty_kv(row_type="Person", name_column="c2", key_field="none", statement_column="none", cell_parts=[],
                           links=[{"column": "c1", "link": "KNOWS_ABOUT", "other_type": "Service", "row_is": "from", "split": "none", "fields": []}])
STEWARDS_GOOD = {**STEWARDS_BAD, "name_column": "c2.1", "cell_parts": [{"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
                 "links": STEWARDS_BAD["links"] + [{"column": "c3", "link": "REPORTS_TO", "other_type": "Person", "row_is": "from",
                                                     "split": "none", "fields": [{"column": "c4", "field": "valid_from"}]}]}
PERIODS_MAP = f1.empty_kv(row_type="Person", name_column="c2.1", key_field="none", statement_column="none",
                          cell_parts=[{"column": "c2", "parts": 2, "separator": "", "pattern": r"(.+?)\s*·\s*(.+)"}],
                          links=[{"column": "c2.2", "link": "KNOWS_ABOUT", "other_type": "Service", "row_is": "from", "split": "none", "fields": []}])
LDAP_MAP = f1.empty_kv(row_type="Person", name_column="c1.2", key_column="c1.1", key_field="hr_worker_id", statement_column="none",
                       cell_parts=[{"column": "c1", "parts": 3, "separator": "", "pattern": r"(\S+)\s+(.+)\s+(\S+)"},
                                   {"column": "c4", "parts": 2, "separator": " ", "pattern": ""}],
                       fields=[{"column": "c2", "field": "title"}, {"column": "c4.2", "field": "work_email"}],
                       links=[{"column": "c3", "link": "REPORTS_TO", "other_type": "Person", "row_is": "from", "split": "none", "fields": []}])


class People(f1.Model):
    def ekg_table_map(self, prompt, schema):
        if '"Current steward"' in prompt:
            return STEWARDS_GOOD if "AN EARLIER MAPPING" in prompt else STEWARDS_BAD
        return PERIODS_MAP if '"Person / module"' in prompt else LDAP_MAP

    def ekg_doc_facts(self, prompt, schema):
        p = base.StandIn.label_of(prompt, "Suzan (suzan@acme.com) approved")
        if p is None:
            return {"records": [], "links": []}
        return {"records": [{"ref": "r1", "type": "Person", "name": "Suzan", "statement": "", "source": p,
                             "quote": "Suzan (suzan@acme.com) approved", "fields": [{"field": "work_email", "value": "suzan@acme.com"}]}],
                "links": []}


def people_checks(check):
    g = base.base_graph()
    # the poorest table first in the document: the richest one must still make the records
    base.add_doc(g, "directory.pdf", [("Stewards", 1, [("table", STEWARDS)]), ("Periods", 1, [("table", PERIODS)]),
                                      ("Directory", 1, [("table", LDAP)]),
                                      ("Notes", 1, [("text", "Suzan (suzan@acme.com) approved the refund change. " * 3)])])
    model = People()
    rep, out, tmp = f1.run_fill(g, model, ["table_map", "doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    people = collections.Counter(r["props"].get("full_name") for r in recs if r["type"] == "Person")
    led = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"]
    check("people: a composite cell used as a person's name is rejected by code and asked again once, with the reason",
          any("AN EARLIER MAPPING" in p and "every sample cell holds more than a name" in p for n, p in model.calls)
          and any(e.get("retried_after", "").startswith("check: column c2 names a Person") and e["status"] == "accepted" for e in led))
    check("people: three tables and a paragraph naming the same people make ONE Person each (Suzan, Mahesh, Emma); 'Mack', "
          "only ever a bare first name in a manager cell, is never made a Person (counted)",
          people == {"Suzan": 1, "Mahesh": 1, "Emma": 1}
          and rep["written"]["documents_and_tables"].get("Person: a bare first name that no record could be, not linked (not created)", 0) >= 1)
    suzan = next(r for r in recs if r["props"].get("full_name") == "Suzan")
    check("people: the richest table wrote first: Suzan carries the directory's id, email and title, the composite parts in their fields",
          suzan["props"].get("hr_worker_id") == "E004" and suzan["props"].get("work_email") == "suzan@acme.com"
          and suzan["props"].get("title") == "Senior Software Engineer")
    know = {(base._name(by_id[lk["a"]]), base._name(by_id[lk["b"]])) for lk in links if lk["type"] == "KNOWS_ABOUT"}
    check("people: the other tables' rows link the same record (steward and period tables -> PAY-AUTH)",
          ("Suzan", "PAY-AUTH") in know and ("Mahesh", "PAY-AUTH") in know and ("Emma", "PAY-REF") in know)
    check("people: the prose mention with an email resolves to the directory's Suzan (no new Person)",
          rep["written"]["documents_and_tables"].get("Person: resolved to the one existing record", 0) >= 1)
    check("people: no possible duplicates are left", rep["written"]["possible_duplicates"]["records"] == 0)
    check("people: every written record and link validates", not base.all_valid(recs, links, by_id))

    # an existing record with the row's email is reused, whatever its name; shared ids are reported, not merged
    g = base.base_graph()
    g.rec("Person", "hr", "suzan-k", {"full_name": "Suzan Kay", "work_email": "suzan@acme.com"}, SCOPE)
    g.rec("Person", "hr", "ana", {"full_name": "Ana Ruiz", "work_email": "ana@acme.example"}, SCOPE)
    g.rec("Person", "crm", "ana2", {"full_name": "A. Ruiz", "work_email": "ana@acme.example"}, SCOPE)
    g.rec("Person", "crm", "ana3", {"full_name": "Ana Ruiz", "work_email": "ana.r@other.example"}, SCOPE)
    g.rec("Person", "hr", "suzan-l", {"full_name": "Suzan Lee", "work_email": "slee@acme.com"}, SCOPE)
    doc, pieces = base.add_doc(g, "directory.pdf", [("Directory", 1, [("table", LDAP)]),
                                                     ("Notes", 1, [("text", "Suzan (suzan@acme.com) approved the refund change. " * 3)])])
    rep, out, tmp = f1.run_fill(g, People(), ["table_map", "doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    dup = rep["written"]["possible_duplicates"]
    kay = next(r for r in by_id.values() if r["props"].get("full_name") == "Suzan Kay")
    check("people: a bare first name two people share is settled by the email the text gives (word for word), not asked or created",
          "entity_choice" not in rep["kinds"] and any(lk["type"] == "MENTIONS" and lk["a"] == pieces[1]["id"] and lk["b"] == kay["id"] for lk in links))
    check("people: a row whose email an existing Person has reuses that Person (Suzan Kay), not a new 'Suzan'",
          not any(r["type"] == "Person" and r["props"].get("full_name") == "Suzan" for r in recs)
          and any(r["props"].get("full_name") == "Suzan Kay" and r["props"].get("hr_worker_id") == "E004" for r in recs))
    check("people: Persons sharing an email are reported as possible duplicates; the same name alone is not",
          dup["records"] == 2 and dup["examples"][0]["records"] == ["A. Ruiz", "Ana Ruiz"] and dup["examples"][0]["value"] == "ana@acme.example")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(base.QM)
    try:
        coverage_checks(check)
        tool_table_checks(check)
        people_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
