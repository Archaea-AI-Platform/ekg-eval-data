"""Behaviour tests for the last ACME gaps (stand-in model: no key, no network, no cost). The tables are the real ACME
tables, copied from the PDF's text, and the stand-in answers are the answers Glimmer gave on the ACME builds v9/v10
(ledger and responses of ekg-eval-out/acme-docs-v10).

  1. one document, tables read alike: the directory's page 1 maps the team code in "E003 Lionel / eng-orders" as
     MEMBER_OF a Group; page 2 ("E018 Charolette / pm-orders"), same header "ID / employee", same split, left the code
     unread in every build. Code adds the link to page 2 (share_columns): 27 people, each in exactly one team-code group.
  2. a split that misses a cell or two (the contacts table: "Jonathan; Ben for infrastructure issues" beside
     "Mahesh · mahesh@acme.com") is kept for the rows it parses once the corrected answer still fails, whatever the
     corrected answer failed on; the mapping cache keeps that and reuses it with no call.
  3. step 2's links: only the registry's own end types (a link open to "any" record, ABOUT, is not offered for a
     steward table's Service and Person).
  4. people from prose: a role phrase ("Shipment & Operations Product Manager", "Product Manager") is never created as
     a Person; a personal name still is.

    python -I ekg-eval-data/test_fill_gaps.py
"""

import collections
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402
import test_fill_followups as f1  # noqa: E402
from ekg_adapters import common, extract_facts, extract_tables  # noqa: E402
from test_fill_weakspots import STEWARDS, STEWARDS_MAP, fill_view, link  # noqa: E402

# ---------------------------------------------------------------- the real ACME directory tables (PDF text)
DIR1 = ("ID / employee | Role / team | Manager | GitHub / email\n"
        "E001 Mack / eng-all | Director of Engineering / Engineering Leadership | Cambria | @mack / mack@acme.com\n"
        "E002 Mahesh / eng-all | Engineering Manager, Backend / Backend Engineering | Mack | @mahesh / mahesh@acme.com\n"
        "E003 Lionel / eng-orders | Software Engineer / Order Management | Mahesh | @lionel / lionel@acme.com\n"
        "E004 Suzan / eng-payments | Senior Software Engineer / Payment & Billing | Mahesh | @suzan / suzan@acme.com\n"
        "E005 Emma / eng-payments | Software Engineer / Payment & Billing | Mahesh | @emma / emma@acme.com\n"
        "E006 Alex / eng-payments | Software Engineer / Payment & Billing / Inventory integrations | Mahesh | @alex / alex@acme.com\n"
        "E007 Jonathan / eng-ops | Engineering Manager / Shipment & Operations | Mack | @jonathan / jonathan@acme.com\n"
        "E008 Noah Patel / eng-ops | Software Engineer / Shipment & Operations | Jonathan | @noahp / noah.patel@acme.com\n"
        "E009 Maya Chen / eng-ops | Senior Software Engineer / Shipment & Operations | Jonathan | @mayac / maya.chen@acme.com\n"
        "E010 Priya Nair / eng-ops | Software Engineer / Shipment & Operations | Jonathan | @priyan / priya.nair@acme.com\n"
        "E011 Sophia / eng-customer | Engineering Manager, UI / Customer Experience Engineering | Mack | @sophia / sophia@acme.com\n"
        "E012 Lilian / eng-customer | Software Engineer / Customer Communications | Sophia | @lilian / lilian@acme.com\n"
        "E013 Lisa / eng-customer | Software Engineer / Customer Communications | Sophia | @lisa / lisa@acme.com\n"
        "E014 Batuista / eng-analytics | Software Engineer / Analytics & Reporting | Sophia | @batuista / batuista@acme.com\n"
        "E015 Ben / eng-platform | Senior Engineering Manager, DevOps / Infrastructure | Mack | @ben / ben@acme.com\n"
        "E016 Jerry / eng-platform | DevOps Engineer / Infrastructure | Ben | @jerry / jerry@acme.com\n"
        "E017 Javier / eng-platform | DevOps Engineer / Infrastructure | Ben | @javier / javier@acme.com")
DIR2 = ("ID / employee | Responsibility | Manager | Email\n"
        "E018 Charolette / pm-orders | Lead Program Manager / Order Management | Mack | charolette@acme.com\n"
        "E019 Kim / pm-payments | Product Manager / Payment & Billing | Charolette | kim@acme.com\n"
        "E020 Amir / pm-ops | Product Manager / Shipment & Operations | Charolette | amir@acme.com\n"
        "E021 Emily / pm-inventory | Product Manager / Inventory & Availability | Charolette | emily@acme.com\n"
        "E022 Suzie / finance-ledger | Director of Finance / Finance | Cambria | suzie@acme.com\n"
        "E023 Evelyn / sales | Director of Sales / Sales | Cambria | evelyn@acme.com\n"
        "E024 Bob / support | Director of Customer Support / Customer Support | Cambria | bob@acme.com\n"
        "E025 Kox / support | Support Lead, America / Customer Support | Bob | kox@acme.com\n"
        "E026 Peter / hr-people-admin | Director of HR / People Operations | Cambria | peter@acme.com\n"
        "E027 Cambria / executive | CEO / Executive | - | cambria@acme.com")
AREAS = ("Area | Product contact | Engineering / operating contact\n"
         "Payment & Billing | Kim · kim@acme.com | Mahesh · mahesh@acme.com\n"
         "Shipment & Operations | Amir · amir@acme.com | Jonathan · jonathan@acme.com\n"
         "Order Management | Charolette · charolette@acme.com | Mahesh · mahesh@acme.com\n"
         "Inventory & Availability | Emily · emily@acme.com | Mahesh · mahesh@acme.com\n"
         "Customer Communications | Charolette · charolette@acme.com | Sophia · sophia@acme.com\n"
         "Analytics & Reporting | Charolette · charolette@acme.com | Sophia · sophia@acme.com\n"
         "Payment reconciliation | Suzie · suzie@acme.com | Kim / Mahesh for engineering defects\n"
         "Carrier / infrastructure | Amir · amir@acme.com | Jonathan; Ben for infrastructure issues")
SLUGS = {"eng-all", "eng-orders", "eng-payments", "eng-ops", "eng-customer", "eng-analytics", "eng-platform", "pm-orders",
         "pm-payments", "pm-ops", "pm-inventory", "finance-ledger", "sales", "support", "hr-people-admin", "executive"}

# ---------------------------------------------------------------- Glimmer's answers on v10 (both steps, merged)
DIR1_V10 = f1.empty_kv(
    row_type="Person", name_column="c1.2", key_column="c1.1", key_field="hr_worker_id", statement_column="none",
    cell_parts=[{"column": "c1", "parts": 3, "separator": "", "pattern": r"^(E\d+)\s+(.+?)\s*/\s*(.+)$"},
                {"column": "c2", "parts": 2, "separator": "", "pattern": r"^(.+?)\s*/\s*(.+)$"},
                {"column": "c4", "parts": 2, "separator": " / ", "pattern": ""}],
    fields=[{"column": "c1.1", "field": "hr_worker_id"}, {"column": "c1.2", "field": "preferred_name"},
            {"column": "c2.1", "field": "title"}],
    links=[link("c1.3", "MEMBER_OF", "Group"), link("c2.2", "MEMBER_OF", "Group"), link("c3", "REPORTS_TO", "Person"),
           link("c4.1", "HAS_ACCOUNT", "Account"), link("c4.2", "HAS_ALIAS", "EmailAddress")], schema_gap="")
DIR2_V10 = f1.empty_kv(   # the code part (c1.3) left unread, as in v8, v9 and v10
    row_type="Person", name_column="c1.2", key_column="c1.1", key_field="hr_worker_id", statement_column="none",
    cell_parts=[{"column": "c1", "parts": 3, "separator": "", "pattern": r"^(\S+)\s+(.+?)\s*/\s*(.+)$"},
                {"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
    fields=[{"column": "c1.2", "field": "preferred_name"}, {"column": "c2.1", "field": "title"}],
    links=[link("c3", "REPORTS_TO", "Person"), link("c4", "HAS_ALIAS", "EmailAddress")],
    schema_gap="Columns c1.3 and c2.2 contain an employee code/position identifier and a responsibility area")
AREAS_V10 = f1.empty_kv(   # the first answer: " · " splits six of the eight engineering-contact cells
    row_type="Topic", name_column="c1", key_column="none", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": " · ", "pattern": ""},
                {"column": "c3", "parts": 2, "separator": " · ", "pattern": ""}],
    links=[link("c2.1", "KNOWS_ABOUT", "Person", row_is="to"), link("c2.2", "HAS_ALIAS", "EmailAddress", from_column="c2.1"),
           link("c3.1", "KNOWS_ABOUT", "Person", row_is="to")], schema_gap="")
AREAS_RETRY = {**AREAS_V10, "cell_parts": [   # the corrected answer of v10: the pattern still misses "Jonathan; Ben ..."
    {"column": "c2", "parts": 2, "separator": " · ", "pattern": ""},
    {"column": "c3", "parts": 2, "separator": "", "pattern": r"^(.*?)\s[·/;]\s(.*)$"}]}
OVER_GUARD = {"_usage": {"completion_tokens": 15_000}, "shape": "rows"}


class Directory(f1.Model):
    """Answers each table as Glimmer did; `retry` is the answer to a corrected ask of the contacts table."""
    retry = AREAS_RETRY

    def ekg_table_map(self, prompt, schema):
        if '"Engineering / operating contact"' in prompt:
            return self.retry if "AN EARLIER MAPPING" in prompt else AREAS_V10
        if '"Responsibility"' in prompt:
            return DIR2_V10
        if '"GitHub / email"' in prompt:
            return DIR1_V10
        return f1.empty_kv(shape="none", reason="not a table of records")


def graph(dir2=DIR2, prose=None):
    g = base.new_graph()
    pages = [("page 1", 1, [("table", DIR1)]), ("page 2", 1, [("table", dir2)]), ("page 2b", 1, [("table", AREAS)])]
    if prose:
        pages.append(("page 5", 1, [("text", prose)]))
    base.add_doc(g, "ACME_LDAP_Employee_Directory.pdf", pages)
    return g


def run(model, g=None, kinds=("table_map",), out=None):
    return f1.run_fill(g or graph(), model, list(kinds), window=131_072, reply_limit=16_384, out=out)


def graph_of(out, tmp):
    recs, links, by_id = base.written(out, tmp)
    name = lambda i: base._name(by_id[i]) or by_id[i]["props"].get("login") or by_id[i]["props"].get("address")
    L = collections.defaultdict(list)
    for lk in links:
        L[lk["type"]].append((name(lk["a"]), name(lk["b"])))
    persons = [r for r in by_id.values() if r["type"] == "Person"]
    return recs, links, by_id, L, persons


# ---------------------------------------------------------------- 1. the directory's two pages read alike

def directory_checks(check):
    model = Directory()
    rep, out, tmp = run(model)
    recs, links, by_id, L, persons = graph_of(out, tmp)
    led = {e["task"]: e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"}
    teams = collections.defaultdict(set)
    for a, b in L["MEMBER_OF"]:
        if b in SLUGS:
            teams[a].add(b)
    names = sorted(base._name(p) for p in persons)
    check("directory: 27 people from the two pages, each named once", len(names) == 27 and len(set(names)) == 27)
    check("directory: page 2 gains the team code page 1 reads (same header 'ID / employee', same split): all 27 people "
          "are MEMBER_OF exactly one team-code group, Charolette pm-orders, Bob and Kox the one 'support', Cambria executive",
          len(teams) == 27 and all(len(v) == 1 for v in teams.values())
          and teams["Charolette"] == {"pm-orders"} and teams["Kox"] == teams["Bob"] == {"support"}
          and teams["Cambria"] == {"executive"} and teams["Lionel"] == {"eng-orders"}
          and sum(1 for r in by_id.values() if r["type"] == "Group" and base._name(r) == "support") == 1)
    acc = [(a, b) for a, b in L["HAS_ACCOUNT"]]
    check("directory: 17 GitHub accounts, all page 1's, one each; no team code became an account",
          len(acc) == 17 and len({a for a, _ in acc}) == 17 and not any(b in SLUGS for _, b in acc))
    check("directory: 26 REPORTS_TO (Cambria's '-' is no manager), 27 job titles",
          len(L["REPORTS_TO"]) == 26 and sum(1 for p in persons if p["props"].get("title")) == 27)
    page2 = next(e for e in led.values() if "c1.3 and c2.2" in str(e.get("schema_gap")))
    shared = [x for x in rep["kinds"]["table_map"]["acceptance"] if x.get("columns_from_other_tables")]
    check("directory: the ledger keeps Glimmer's own answer; the report says which column came from which table",
          "c1.3" not in str(page2["answer"]["links"]) and len(shared) == 1 and shared[0]["rows"] == 10
          and any("c1.3 MEMBER_OF Group" in n and "ID / employee" in n for n in shared[0]["columns_from_other_tables"]))
    check("directory: the tables still validate", not base.all_valid(recs, links, by_id))

    # not shared: another header, or a split that cuts the column otherwise
    other = DIR2.replace("ID / employee |", "Employee |", 1)
    rep2, out2, tmp2 = run(Directory(), graph(dir2=other))
    _, _, _, L2, _ = graph_of(out2, tmp2)
    check("directory: a column of another header is not read as the other table's (no team for page 2 then)",
          not any(a == "Charolette" for a, _ in L2["MEMBER_OF"]))
    tb_rows = [ln.split(" | ") for ln in DIR2.split("\n")[1:]]
    cut = extract_tables.split_cell(tb_rows[0][0], [0, 3, "", r"^(E\d+)\s+(.+?)\s*/\s*(.+)$"])
    check("directory: page 1's split cuts page 2's cells into the same three values (the condition for sharing)",
          cut == ["E018", "Charolette", "pm-orders"])


# ---------------------------------------------------------------- 2. a split that misses a cell or two

DIR2_V12 = {**DIR2_V10, "cell_parts": [   # v12's first answer: the pattern escaped twice (matches a backslash, no cell)
    {"column": "c1", "parts": 3, "separator": "", "pattern": r"(\\w+)\\s+([^/]+)\\s*/\\s*(.+)"}],
    "fields": [{"column": "c1.2", "field": "preferred_name"}, {"column": "c2", "field": "title"}]}
DIR2_V12_RETRY = {**DIR2_V12, "cell_parts": [{"column": "c1", "parts": 3, "separator": "", "pattern": r"^(\S+)\s+(\S+)\s*/\s*(.+)$"}],
                  "links": DIR2_V10["links"] + [link("c1.3", "HAS_ACCOUNT", "Account")]}   # v12's corrected answer


def directory_v12_checks(check):
    class V12(Directory):
        first = DIR2_V12

        def ekg_table_map(self, prompt, schema):
            if '"Responsibility"' in prompt:
                return DIR2_V12_RETRY if "AN EARLIER MAPPING" in prompt else self.first
            return super().ekg_table_map(prompt, schema)
    for label, first, how in (("escaped twice", DIR2_V12, "the pattern read once unescaped splits every row"),
                              ("team code as account", DIR2_V12_RETRY, "asked again, the same fault: the column is left out")):
        model = V12()
        model.first = first
        rep, out, tmp = run(model)
        _, _, _, L, persons = graph_of(out, tmp)
        teams = collections.defaultdict(set)
        for a, b in L["MEMBER_OF"]:
            if b in SLUGS:
                teams[a].add(b)
        page2 = next(x for x in rep["kinds"]["table_map"]["acceptance"] if x["rows"] == 10)
        check(f"directory (v12, {label}): page 2 accepted ({how}); all 27 people in exactly one team-code group, and no "
              "team code is anyone's account",
              page2["status"].startswith("accepted") and len(persons) == 27 and len(teams) == 27
              and all(len(v) == 1 for v in teams.values()) and not any(b in SLUGS for _, b in L["HAS_ACCOUNT"])
              and len(L["HAS_ACCOUNT"]) == 17)


HISTORY_REAL = ("Period | Person / module | Record\n"
                "Before Oct 03 | Lionel · PAY-AUTH | Primary implementation contributor; assigned to Payments.\n"
                "Oct 03-07 | Mahesh · PAY-AUTH | Interim escalation while Lionel transferred to Order Management.\n"
                "From Oct 08 | Suzan · PAY-AUTH | Current accountable engineer; approved/implemented recent auth work.\n"
                "Before Oct 14 | Noah Patel · OPS-LABEL | Original shipping-label steward and largest contributor.\n"
                "From Oct 14 | Priya Nair · OPS-LABEL | Current accountable engineer; Noah remains on fulfillment tasks.")
HISTORY_SHAPE_V13 = [   # v13's step 1, asked and asked again: a row type no column names
    {"shape": "rows", "row_type": rt, "cell_parts": [{"column": "c2", "parts": 2, "separator": " · ", "pattern": ""}],
     "name_column": "none", "key_column": "none", "statement_column": "none", "entity_name": "", "reason": "",
     "columns": [{"column": "c2.1", "type": "Person"}, {"column": "c2.2", "type": "Service"}]} for rt in ("Handover", "PropertyClaim")]
HISTORY_MAP_V14 = {"key_field": "none", "fields": [], "schema_gap": "", "label_fields": [], "label_links": [], "reason": "",
                   "links": [link("c2.2", "KNOWS_ABOUT", "Service", fields=[("c3", "aspect")],
                                  period={"column": "c1", "from_words": ["From"], "to_words": ["Before"]})]}


def history_checks(check):
    class History(Directory):
        def ekg_table_shape(self, prompt, schema):
            if '"Person / module"' in prompt:
                return HISTORY_SHAPE_V13[1 if "AN EARLIER MAPPING" in prompt else 0]
            return super().ekg_table_shape(prompt, schema)

        def ekg_table_map(self, prompt, schema):
            return HISTORY_MAP_V14 if '"Person / module"' in prompt else super().ekg_table_map(prompt, schema)
    g = base.new_graph()
    base.add_doc(g, "ACME_LDAP_Employee_Directory.pdf", [("page 1", 1, [("table", DIR1)]), ("page 3", 1, [("table", HISTORY_REAL)])])
    f1.Model.tables = {}
    model = History()
    rep, out, tmp = f1.run_fill(g, model, ["table_map"], window=131_072, reply_limit=16_384)
    e = next(x for x in base.rows(out / "ledger.jsonl") if x.get("row_type_from_columns"))
    _, _, _, L, _ = graph_of(out, tmp)
    shapes = [p for n, p in model.calls if n == "ekg_table_shape" and '"Person / module"' in p]
    check("history: step 1 names no column for its row type twice (v13: Handover, then PropertyClaim): code takes the "
          "first typed column's record as the row (Person, c2.1), asks step 2 once, and the table is accepted",
          e["status"] == "accepted" and e["mapping"]["row_type"] == "Person" and len(shapes) == 2
          and "PropertyClaim -> Person" in e["row_type_from_columns"]
          and ("Suzan", "PAY-AUTH") in set(L["KNOWS_ABOUT"]))
    check("history: a table whose only typed column is a tool's record (a pull request reference) gets no such row "
          "type: it stays rejected and is read as text",
          extract_tables._row_from_columns({"row_type": "Handover", "columns": [{"column": "c3.1", "type": "PullRequest"}]},
                                           fill_view().catalog()) is None)


def patience_checks(check):
    import asyncio
    seen = []

    async def ask(prompt, name, schema, timeout=None):
        seen.append(timeout())
        return {"error": "reply ran away: no answer after 307 seconds"} if len(seen) == 1 else {"shape": "rows"}
    info = {}
    ans, why = asyncio.run(extract_tables._step(ask, lambda tier: tier, "ekg_table_shape", {}, fill_view().catalog(), {}, info))
    from ekg_adapters import fill_budget
    check("guard: a step whose fuller tier ran past the time guard asks the shorter tier with the longest time a mapping "
          "may take (a provider slow at the start of a run, before any pace is known, is not a verdict on the table)",
          seen[0] < fill_budget.MAPPING_SECONDS_MAX and seen[1] == fill_budget.MAPPING_SECONDS_MAX and why is None)


def contacts_checks(check):
    model = Directory()
    rep, out, tmp = run(model)
    _, _, _, L, persons = graph_of(out, tmp)
    row = next(x for x in rep["kinds"]["table_map"]["acceptance"] if x["rows"] == 8)
    check("contacts: the first split misses two cells, the corrected one still misses one: the table is accepted for "
          "the rows its split parses, and read as text too (verdict: accepted, also read as text)",
          row["status"] == "accepted, also read as text" and row.get("split_kept_for_the_rows_it_parses")
          and row["also_text"].get("cells_that_do_not_split"))
    know = {(a, b) for a, b in L["KNOWS_ABOUT"]}
    check("contacts: each area's product contact KNOWS_ABOUT it; no Person made of 'Jonathan; Ben for infrastructure issues'",
          {("Kim", "Payment & Billing"), ("Amir", "Shipment & Operations"), ("Suzie", "Payment reconciliation")} <= know
          and not any(";" in base._name(p) or " for " in base._name(p) for p in persons))

    # the corrected answer fails otherwise (over the guard at both tiers): the first answer's split is kept
    model = Directory()
    model.retry = OVER_GUARD
    rep2, out2, tmp2 = run(model)
    row2 = next(x for x in rep2["kinds"]["table_map"]["acceptance"] if x["rows"] == 8)
    check("contacts: when the corrected answer fails on something else (over the size guard), the first answer's split "
          "is kept for the rows it parses: the same verdict",
          row2["status"] == "accepted, also read as text" and row2.get("split_kept_for_the_rows_it_parses"))

    # the mapping cache keeps the lenient mapping: a rebuild makes no table call
    new_out = tmp / "out2"
    new_out.mkdir()
    shutil.copy(out / "table_mappings.json", new_out / "table_mappings.json")
    again = Directory()
    rep3, out3, tmp3 = run(again, out=new_out)
    _, _, _, L3, _ = graph_of(out3, tmp3)
    check("cache: a rebuild with the kept mappings makes no table call (the contacts table's kept split included), "
          "shares page 1's team code again and writes the same links",
          not [n for n, _ in again.calls if n.startswith("ekg_table")]
          and rep3["kinds"]["table_map"]["mappings_from_cache"] == 3
          and rep3["written"]["links_by_type"] == rep["written"]["links_by_type"]
          and len({a for a, b in L3["MEMBER_OF"] if b in SLUGS}) == 27)


# ---------------------------------------------------------------- 3. step 2's links

def candidate_checks(check):
    cat = fill_view().catalog()
    steward = extract_tables.candidate_links(cat, "Service", {"c2.1": "Person", "c2.2": "Account", "c3": "Person"})
    pr = extract_tables.candidate_links(cat, "PullRequest", {"c3": "Service", "c4": "Account"})
    check("step 2: a steward table (Service, Person, Account) is offered OWNED_BY, HAS_ACCOUNT, REPORTS_TO, and not "
          "ABOUT (open to 'any' record at one end, it names neither Service nor Person as its subject)",
          {"OWNED_BY", "HAS_ACCOUNT", "REPORTS_TO"} <= set(steward) and "ABOUT" not in steward)
    check("step 2: a pull request table is still offered ABOUT (the registry names PullRequest as its subject)",
          "ABOUT" in pr and pr["ABOUT"][0] == ["PullRequest"])
    t = type("T", (), {"write": {"rows": 3}, "context": "DOCUMENT: d", "question": "q",
                       "sources": [type("S", (), {"label": "T1", "title": "t", "text": STEWARDS})()]})()
    shape = {"shape": "rows", "row_type": "Service", "name_column": "c1", "columns": [
        {"column": "c2.1", "type": "Person"}, {"column": "c2.2", "type": "Account"}, {"column": "c3", "type": "Person"}],
        "cell_parts": [{"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}]}
    text, cands = extract_tables.columns_prompt(t, cat, shape)
    check("step 2: the steward prompt lists only those links", "- ABOUT:" not in text and "- OWNED_BY:" in text and cands == steward)

    # a whole answer over the token guard at both tiers (v9: 15,575 then 13,004 tokens, the same sound mapping)
    class Long(f1.Model):
        reply = None

        def ekg_table_map(self, prompt, schema):
            return self.reply if '"Current steward"' in prompt else f1.empty_kv(shape="none")
    g = base.new_graph()
    base.add_doc(g, "ACME_LDAP_Employee_Directory.pdf", [("page 3", 1, [("table", STEWARDS)])])
    got = {}
    for label, reply in (("whole", {**STEWARDS_MAP, "_usage": {"completion_tokens": 13_004}}),
                         ("ran away", {"error": "reply ran away: no answer after 307 seconds"})):
        m = Long()
        m.reply = reply
        rep, out, _ = f1.run_fill(g, m, ["table_map"], window=131_072, reply_limit=16_384)
        got[label] = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"][0]
    check("guard: a step whose answers at both tiers were whole but over the token guard keeps the shorter one, held to "
          "the checks like any other (the steward table is accepted, the ledger says so); a reply that ran past the time "
          "guard is never kept",
          got["whole"]["status"] == "accepted" and got["whole"]["steps"].get("kept_over_the_guard")
          and got["ran away"]["status"] == "rejected" and "too long" in got["ran away"]["reason"])


# ---------------------------------------------------------------- 4. people from prose

PROSE = ("Owned by: Shipment & Operations Product Manager. Escalations go to the Product Manager on call, then to the "
         "Order Management Lead. The label printer runbook is kept by Dana Whitfield.")


def people_checks(check):
    def facts(prompt):
        p = base.StandIn.label_of(prompt, "Owned by:")
        if p is None:
            return {"records": [], "links": []}
        rec = lambda ref, name, quote: {"ref": ref, "type": "Person", "name": name, "statement": "", "fields": [],
                                        "quote": quote, "source": p}
        return {"records": [rec("r1", "Shipment & Operations Product Manager", "Owned by: Shipment & Operations Product Manager"),
                            rec("r2", "Product Manager", "the Product Manager on call"),
                            rec("r3", "Dana Whitfield", "kept by Dana Whitfield"),
                            rec("r4", "Order Management Lead", "the Order Management Lead")], "links": []}
    model = Directory()
    model.facts = facts
    rep, out, tmp = run(model, graph(prose=PROSE), kinds=("table_map", "doc_facts"))
    _, _, _, _, persons = graph_of(out, tmp)
    names = {base._name(p) for p in persons}
    c = rep["written"]["documents_and_tables"]
    check("people: a role phrase from prose is not made a Person ('Shipment & Operations Product Manager' holds the "
          "group 'Shipment & Operations' and the title 'Product Manager'; 'Product Manager' is a title people hold); "
          "and neither is one that holds a group's name ('Order Management Lead'); a personal name is ('Dana Whitfield')",
          "Shipment & Operations Product Manager" not in names and "Product Manager" not in names
          and "Order Management Lead" not in names
          and "Dana Whitfield" in names and len(names) == 28
          and any(k.startswith("Person: not created, the name") for k in c))
    v = fill_view()
    v._index()
    person = {"Person"} | extract_facts.family(common._reg, "Person", v._identity)
    check("people: the rule is the graph's own records and the name's marks, not a word list: with nothing in the "
          "graph, 'Ops Lead' could be a name; '&' or ';' never is",
          v.named_otherwise("Ops Lead", person) is None
          and extract_facts.not_a_personal_name(v, v.catalog(), "Person", "Jonathan; Ben") is not None
          and extract_facts.not_a_personal_name(v, v.catalog(), "Person", "Noah Patel") is None)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(base.QM)
    try:
        directory_checks(check)
        directory_v12_checks(check)
        patience_checks(check)
        history_checks(check)
        contacts_checks(check)
        candidate_checks(check)
        people_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
