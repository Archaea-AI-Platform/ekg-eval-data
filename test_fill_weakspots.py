"""Behaviour tests for the weak spots found on the ACME builds v4-v7 and the Kafka sample (stand-in model: no key,
no network, no cost). The tables are the real ACME tables, copied from the PDFs' text.

  1. tables in two small steps: step 1 (shape, row type, name and key columns, what each column names) sees the
     record types only; step 2 sees the row type's fields and ONLY the links code found between the table's types.
     A step the size or time guard refuses is asked once more with the shorter tier before the table is read as
     text. An accepted mapping is kept by table signature and reused by a later run (no call) unless the table changed.
  2. identity: a login column that gives one value to several rows' records (two people "support") is no account
     column: the mapping is rejected with that reason, asked again once, and the corrected one written.
  3. people: a bare first name in a table ("Noah 26") is never made a Person; it is the one person whose name starts
     with it only when the row's record is already linked to that person (corroborated), else unlinked and counted.
     A short repository name is the one qualified repository it ends ("payments-platform").
  4. dated stewardship: a period column ("Before Oct 03", "Oct 03-07", "From Oct 08") gives valid_from / valid_to by
     the table's own words, a date without its year takes the document's; a month-name date parses. The reviewers
     table writes the registry's approver link per path (aspect) with the kind its header states (fixed).
  5. quotes: each sentence of a quote that joins sentences must be word for word in one passage; hyperlink targets and
     the prompt's own label are not part of the text; a paraphrase still fails.
  6. reading: a window whose reply is cut at the provider's limit is halved, a single passage's text too; the halves
     answered are kept, the rest is recorded as unanswered and asked again next run. Prose is read for what the
     registry says is worth keeping (stated in the prompt from the registry); a follow-up that keeps nothing is not
     repeated.

    python -I ekg-eval-data/test_fill_weakspots.py
"""

import asyncio
import collections
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402
import test_fill_followups as f1  # noqa: E402
from ekg_adapters import common, extract_facts, extract_schema, extract_tables, fill  # noqa: E402
from ekg_adapters.fill_tasks import Item, View  # noqa: E402

SCOPE = base.SCOPE

# ---------------------------------------------------------------- the ACME tables, as the PDFs' text gives them
DIR1 = ("ID / employee | Role / team | Manager | GitHub / email\n"
        "E001 Mack / eng-all | Director of Engineering / Engineering Leadership | Cambria | @mack / mack@acme.com\n"
        "E002 Mahesh / eng-all | Engineering Manager, Backend / Backend Engineering | Mack | @mahesh / mahesh@acme.com\n"
        "E003 Lionel / eng-orders | Software Engineer / Order Management | Mahesh | @lionel / lionel@acme.com\n"
        "E004 Suzan / eng-payments | Senior Software Engineer / Payment & Billing | Mahesh | @suzan / suzan@acme.com\n"
        "E005 Emma / eng-payments | Software Engineer / Payment & Billing | Mahesh | @emma / emma@acme.com\n"
        "E007 Jonathan / eng-ops | Engineering Manager / Shipment & Operations | Mack | @jonathan / jonathan@acme.com\n"
        "E008 Noah Patel / eng-ops | Software Engineer / Shipment & Operations | Jonathan | @noahp / noah.patel@acme.com\n"
        "E009 Maya Chen / eng-ops | Senior Software Engineer / Shipment & Operations | Jonathan | @mayac / maya.chen@acme.com\n"
        "E010 Priya Nair / eng-ops | Software Engineer / Shipment & Operations | Jonathan | @priyan / priya.nair@acme.com")
DIR2 = ("ID / employee | Responsibility | Manager | Email\n"
        "E022 Suzie / finance-ledger | Director of Finance / Finance | Cambria | suzie@acme.com\n"
        "E024 Bob / support | Director of Customer Support / Customer Support | Cambria | bob@acme.com\n"
        "E025 Kox / support | Support Lead, America / Customer Support | Bob | kox@acme.com\n"
        "E027 Cambria / executive | CEO / Executive | - | cambria@acme.com")
REPOS = ("Repository | Product area | Includes\n"
         "acme-commerce/payments-platform | Payment & Billing | Authorizations, captures, refunds, coupons, loyalty / points\n"
         "acme-commerce/fulfillment-platform | Shipment & Operations | Pick tasks, pack verification, shipping labels and carrier / acceptance")
MODULES = ("Module | Repository | Primary file\n"
           "PAY-AUTH | payments-platform | src/payments/authorization.py\n"
           "OPS-FULF | fulfillment-platform | src/warehouse/task_dispatch.py\n"
           "OPS-PACK | fulfillment-platform | src/warehouse/pack_checks.py")
STEWARDS = ("Module | Current steward | Manager | Effective\n"
            "PAY-AUTH | Suzan / @suzan | Mahesh | 2026-10-08\n"
            "OPS-FULF | Noah Patel / @noahp | Jonathan | 2026-01-01\n"
            "OPS-PACK | Maya Chen / @mayac | Jonathan | 2026-01-01")
HISTORY = ("Period | Person / module | Record\n"
           "Before Oct 03 | Lionel – PAY-AUTH | Primary implementation contributor; assigned to Payments.\n"
           "Oct 03-07 | Mahesh – PAY-AUTH | Interim escalation while Lionel transferred to Order Management.\n"
           "From Oct 08 | Suzan – PAY-AUTH | Current accountable engineer; approved/implemented recent auth work.")
ACTIVITY = ("Module | Most commits | Second most | Latest merged PR\n"
            "OPS-FULF | Noah 26 | Maya 11 | —\n"
            "OPS-PACK | Maya 29 | Priya 8 | #241")
REVIEWERS = ("Repository | Path scope | Requested reviewers\n"
             "payments-platform | /src/payments/authorization.py | @suzan @mahesh\n"
             "fulfillment-platform | /src/warehouse/pack_checks.py | @mayac @jonathan")
AREAS = ("Area | Product contact | Engineering / operating contact\n"
         "Payment & Billing | Kim | Mahesh\n"
         "Carrier / infrastructure | Amir | Jonathan; Ben for infrastructure issues")
CHANGES = ("Effective | Change | Supporting GitHub record\n"
           "Oct 03, 2026 | Lionel moved from Payments to Order Management. | Order-state PR #221 and corporate directory history\n"
           "Oct 08, 2026 | Suzan became the payment-authorization service steward. | Authorization roster PR #225 and directory assignment")


def link(col, lt, other, row_is="from", **kw):
    return {"column": col, "link": lt, "other_type": other, "row_is": row_is, "split": kw.pop("split", "none"),
            "fields": [{"column": c, "field": f} for c, f in kw.pop("fields", [])], "from_column": kw.pop("from_column", "row"),
            "other_key_field": kw.pop("other_key_field", "none"), "fixed": kw.pop("fixed", []),
            "period": kw.pop("period", {"column": "none", "from_words": [], "to_words": []})}


DIR1_MAP = f1.empty_kv(
    row_type="Person", name_column="c1.2", key_column="c1.1", key_field="hr_worker_id", statement_column="none",
    cell_parts=[{"column": "c1", "parts": 3, "separator": "", "pattern": r"(\S+)\s+(.+?)\s*/\s*(\S+)"},
                {"column": "c2", "parts": 2, "separator": " / ", "pattern": ""},
                {"column": "c4", "parts": 2, "separator": " / ", "pattern": ""}],
    fields=[{"column": "c2.1", "field": "title"}, {"column": "c4.2", "field": "work_email"}],
    links=[link("c3", "REPORTS_TO", "Person"), link("c1.3", "MEMBER_OF", "Group"), link("c2.2", "MEMBER_OF", "Group"),
           link("c4.1", "HAS_ACCOUNT", "Account")], schema_gap="")
DIR2_PARTS = [{"column": "c1", "parts": 3, "separator": "", "pattern": r"(\S+)\s+(.+?)\s*/\s*(\S+)"},
              {"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}]
DIR2_BAD = f1.empty_kv(   # v7's mapping: the team slug read as the person's account
    row_type="Person", name_column="c1.2", key_column="c1.1", key_field="hr_worker_id", statement_column="none",
    cell_parts=DIR2_PARTS, fields=[{"column": "c2.1", "field": "title"}, {"column": "c4", "field": "work_email"}],
    links=[link("c3", "REPORTS_TO", "Person"), link("c1.3", "HAS_ACCOUNT", "Account")], schema_gap="")
DIR2_GOOD = {**DIR2_BAD, "links": [link("c3", "REPORTS_TO", "Person"), link("c1.3", "MEMBER_OF", "Group"),
                                   link("c2.2", "MEMBER_OF", "Group")]}
REPOS_MAP = f1.empty_kv(row_type="Repository", name_column="c1", key_column="none", key_field="none", statement_column="none",
                        cell_parts=[], links=[link("c2", "ABOUT", "Topic", row_is="to")], schema_gap="")
MODULES_MAP = f1.empty_kv(row_type="Service", name_column="c1", key_column="none", key_field="none", statement_column="none",
                          cell_parts=[], links=[link("c2", "OWNS_PATH", "Repository", fields=[("c3", "glob")])], schema_gap="")
STEWARDS_MAP = f1.empty_kv(
    row_type="Service", name_column="c1", key_column="none", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
    links=[link("c2.1", "OWNED_BY", "Person", fields=[("c4", "valid_from")]),
           link("c2.2", "HAS_ACCOUNT", "Account", from_column="c2.1")], schema_gap="")
HISTORY_MAP = f1.empty_kv(
    row_type="Service", name_column="c2.2", key_column="none", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": " – ", "pattern": ""}],
    links=[link("c2.1", "OWNED_BY", "Person", period={"column": "c1", "from_words": ["From"], "to_words": ["Before"]})],
    schema_gap="")
ACTIVITY_MAP = f1.empty_kv(
    row_type="Service", name_column="c1", key_column="none", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": "", "pattern": r"(.+?)\s+(\d+)"},
                {"column": "c3", "parts": 2, "separator": "", "pattern": r"(.+?)\s+(\d+)"}],
    links=[link("c2.1", "KNOWS_ABOUT", "Person", row_is="to"), link("c3.1", "KNOWS_ABOUT", "Person", row_is="to")],
    schema_gap="the commit counts (c2.2, c3.2): no link from a Person to a Service has a count field")
AREAS_MAP = f1.empty_kv(row_type="Topic", name_column="c1", key_column="none", key_field="none", statement_column="none",
                        cell_parts=[], links=[link("c3", "KNOWS_ABOUT", "Person", row_is="to")], schema_gap="")
REVIEWERS_MAP = f1.empty_kv(
    row_type="Repository", name_column="c1", key_column="none", key_field="none", statement_column="none", cell_parts=[],
    links=[link("c3", "APPROVER_FOR", "Account", row_is="to", split="space", fields=[("c2", "aspect")],
                fixed=[{"field": "kind", "value": "default_reviewer"}])], schema_gap="")


class Acme(f1.Model):
    """The stand-in for the ACME tables: answers by the table's header."""
    refuse_full = False   # step 1 at the full tier goes round in circles (over the guard)

    def ekg_table_shape(self, prompt, schema):
        if self.refuse_full and "(named by" not in prompt:
            return {"_usage": {"completion_tokens": 15_000}, "shape": "rows"}
        return super().ekg_table_shape(prompt, schema)

    def ekg_table_map(self, prompt, schema):
        if '"Responsibility"' in prompt:
            return DIR2_GOOD if "AN EARLIER MAPPING" in prompt else DIR2_BAD
        return super().ekg_table_map(prompt, schema)


def acme_graph():
    g = base.new_graph()
    base.add_doc(g, "ACME_LDAP_Employee_Directory.pdf",
                 [("page 1", 1, [("table", DIR1)]), ("page 2", 1, [("table", DIR2)]), ("page 3", 1, [("table", STEWARDS)]),
                  ("page 3b", 1, [("table", HISTORY)]),
                  ("page 2b", 1, [("table", AREAS)])])
    base.add_doc(g, "ACME_GitHub_Engineering_Activity.pdf",
                 [("page 1", 1, [("table", REPOS)]), ("page 1b", 1, [("table", MODULES)]), ("page 2", 1, [("table", ACTIVITY)]),
                  ("page 5", 1, [("table", REVIEWERS)]), ("page 5b", 1, [("table", CHANGES)])])
    return g


TABLES = {'"GitHub / email"': DIR1_MAP, '"Product area"': REPOS_MAP, '"Primary file"': MODULES_MAP,
          '"Current steward"': STEWARDS_MAP, '"Person / module"': HISTORY_MAP, '"Most commits"': ACTIVITY_MAP,
          '"Requested reviewers"': REVIEWERS_MAP, '"Engineering / operating contact"': AREAS_MAP}


def run(model, out=None):
    f1.Model.tables = TABLES
    return f1.run_fill(acme_graph(), model, ["table_map"], window=131_072, reply_limit=16_384, out=out)


# ---------------------------------------------------------------- 1.-4. tables

def table_checks(check):
    model = Acme()
    rep, out, tmp = run(model)
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    name = lambda i: base._name(by_id[i]) or by_id[i]["props"].get("login") or by_id[i]["props"].get("path")
    L = collections.defaultdict(list)
    for lk in links:
        L[lk["type"]].append((name(lk["a"]), name(lk["b"]), lk["props"]))
    led = {e["task"]: e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"}
    shape_calls = [p for n, p in model.calls if n == "ekg_table_shape"]
    map_calls = [p for n, p in model.calls if n == "ekg_table_map"]

    # 1. two steps, short lists
    dir1_map = next(p for p in map_calls if '"GitHub / email"' in p)
    check("two steps: step 1 lists record types (no links); step 2 lists only the links between the table's types "
          "(REPORTS_TO, MEMBER_OF, HAS_ACCOUNT for a directory), never the whole catalog",
          all("LINK TYPES" not in p and "- Person:" in p for p in shape_calls)
          and all(f"- {x}:" in dir1_map for x in ("REPORTS_TO", "MEMBER_OF", "HAS_ACCOUNT"))
          and "- CHANGES:" not in dir1_map and "- MERGED_AS:" not in dir1_map
          and max(map(len, map_calls)) < len(extract_tables.catalog_text(fill_view().catalog())[0]))
    sizes = [e.get("steps", {}).get("prompt_chars", {}) for e in led.values()]
    check("two steps: every table's ledger entry says how large each step's prompt was",
          all({"ekg_table_shape", "ekg_table_map"} <= set(s) for s in sizes if s))

    # 2. identity: one login value for several rows is no account
    dir2 = next(e for e in led.values() if "support" in e.get("retried_after", ""))
    check("identity: the team slug mapped as HAS_ACCOUNT gives 'support' to Bob and Kox: rejected with that reason, "
          "asked again once, and the corrected mapping (MEMBER_OF) accepted",
          "gives one value (\"support\")" in dir2["retried_after"] and dir2["status"] == "accepted"
          and any("AN EARLIER MAPPING" in p and "is one identity and belongs to one record" in p for p in map_calls))
    members = {(a, b) for a, b, _ in L["MEMBER_OF"]}
    accounts = {(a, b) for a, b, _ in L["HAS_ACCOUNT"]}
    check("identity: the 4 page-2 people are MEMBER_OF their slug and their area; no 'support' or 'executive' account",
          {("Bob", "support"), ("Kox", "support"), ("Cambria", "executive"), ("Suzie", "Finance")} <= members
          and not any(b in ("support", "executive", "finance-ledger") for _, b in accounts))
    check("directory: every page-1 employee is MEMBER_OF both the slug and the team, and has their one GitHub account",
          {("Suzan", "eng-payments"), ("Suzan", "Payment & Billing"), ("Maya Chen", "eng-ops")} <= members
          and {("Noah Patel", "noahp"), ("Maya Chen", "mayac"), ("Mack", "mack")} <= accounts
          and sum(1 for a, _ in accounts if a in ("Noah Patel",)) == 1)

    # 3. people: bare first names
    persons = collections.Counter(base._name(r) for r in by_id.values() if r["type"] == "Person")
    know = {(a, b) for a, b, _ in L["KNOWS_ABOUT"]}
    check("people: no Person is made from a bare first name ('Noah', 'Maya', 'Priya' in the commit-count table)",
          not ({"Noah", "Maya", "Priya"} & set(persons)) and persons["Noah Patel"] == 1 and persons["Maya Chen"] == 1)
    check("people: a cell that holds two names ('Jonathan; Ben for infrastructure issues', the ACME contacts table) is "
          "never made a Person; it is counted",
          not any(";" in str(p) for p in persons)
          and c.get("Person: a cell that holds more than a name (\"Jonathan; Ben for infrastructu\"), not created and not linked") == 1)
    check("people: 'Noah 26' on OPS-FULF is Noah Patel (OPS-FULF is already OWNED_BY him), 'Maya 29' on OPS-PACK is "
          "Maya Chen; 'Maya 11' on OPS-FULF and 'Priya 8' on OPS-PACK have no corroborating link: not linked, counted",
          {("Noah Patel", "OPS-FULF"), ("Maya Chen", "OPS-PACK")} <= know
          and ("Maya Chen", "OPS-FULF") not in know and ("Priya Nair", "OPS-PACK") not in know
          and c.get("Person: a bare first name, the one record whose name starts with it, already linked to the row's record") == 2
          and c.get("Person: a bare first name, one record could be it but nothing links it to the row's record yet: not "
                    "linked (not created)") == 2)
    mentions = {(a, b) for a, b, _ in L["MENTIONS"]}
    check("people: the commit-count piece MENTIONS the module and the people it names (a record result can point to it)",
          any(b == "Noah Patel" for _, b in mentions) and any(b == "OPS-FULF" for _, b in mentions))
    repos = sorted(base._name(r) for r in by_id.values() if r["type"] == "Repository")
    owns = {(a, b) for a, b, _ in L["OWNS_PATH"]}
    check("repositories: 'payments-platform' is the one 'acme-commerce/payments-platform' (no second repository)",
          repos == ["acme-commerce/fulfillment-platform", "acme-commerce/payments-platform"]
          and ("PAY-AUTH", "acme-commerce/payments-platform") in owns)
    check("logins: '@suzan' / '@mahesh' cells resolve to the directory's accounts (no account made twice)",
          sum(1 for r in by_id.values() if r["type"] == "Account" and r["props"].get("login") == "suzan") == 1)

    # 4. dated stewardship and reviewers
    owned = collections.defaultdict(list)
    for a, b, p in L["OWNED_BY"]:
        owned[a].append((b, (p.get("valid_from") or "")[:10], (p.get("valid_to") or "")[:10]))
    auth = sorted(set(owned["PAY-AUTH"]))
    current = {b for b, f, t in auth if not t}
    check("stewardship: PAY-AUTH's history gives Lionel an end (Before Oct 03 -> valid_to 2026-10-03), Mahesh a range "
          "(Oct 03-07), Suzan a start (From Oct 08), the year taken from the document's own dates; one current owner",
          ("Lionel", "", "2026-10-03") in auth and ("Mahesh", "2026-10-03", "2026-10-07") in auth
          and ("Suzan", "2026-10-08", "") in auth and current == {"Suzan"})
    check("stewardship: no OWNED_BY without a date", all(f or t for xs in owned.values() for _, f, t in xs))
    appr = {(a, b, p.get("aspect"), p.get("kind")) for a, b, p in L["APPROVER_FOR"]}
    check("reviewers: each requested reviewer is APPROVER_FOR the repository, for that path (aspect), kind default_reviewer "
          "(the header's word, a listed value); never an owner",
          ("suzan", "acme-commerce/payments-platform", "/src/payments/authorization.py", "default_reviewer") in appr
          and ("jonathan", "acme-commerce/fulfillment-platform", "/src/warehouse/pack_checks.py", "default_reviewer") in appr
          and len(appr) == 4 and not any(a == "acme-commerce/payments-platform" for a, _, _ in L["OWNED_BY"]))
    check("tables: every written record and link validates", not base.all_valid(recs, links, by_id))

    # 1. the mapping cache: a later run in a new folder with the kept mappings asks nothing for the same tables
    new_out = tmp / "out2"
    new_out.mkdir()
    shutil.copy(out / "table_mappings.json", new_out / "table_mappings.json")
    again = Acme()
    rep2, out2, tmp2 = run(again, out=new_out)
    check("cache: a rebuild reuses each table's accepted mapping by its signature (no call; only the table that was not "
          "accepted is asked again), and writes the same links",
          all('"Supporting GitHub record"' in p for n, p in again.calls if n.startswith("ekg_table"))
          and rep2["kinds"]["table_map"]["mappings_from_cache"] == rep["kinds"]["table_map"].get("accepted", 0)
          and rep2["written"]["links_by_type"] == rep["written"]["links_by_type"])
    sig = lambda header: extract_tables.signature({"id": "d", "props": {"_key": "k"}}, type("T", (), {"heading": "h", "header": header})())
    check("cache: the signature changes with the header row, not with the rows",
          sig(["Module", "Steward"]) != sig(["Module", "Owner"]) and sig(["a", "b"]) == sig(["a", "b"]))

    # 1. the guard: step 1 refused at the full tier is asked with the short list, and accepted
    model = Acme()
    model.refuse_full = True
    rep3, out3, _ = run(model)
    e = [x for x in base.rows(out3 / "ledger.jsonl") if x["kind"] == "table_map"]
    check("guard: a step over the size guard at the full tier is asked once more with the short list before the table "
          "falls to text; the tables are then accepted as before",
          rep3["kinds"]["table_map"].get("accepted") == rep["kinds"]["table_map"].get("accepted")
          and all(x["steps"]["tier"]["ekg_table_shape"] == "short" and x["steps"]["refused"] for x in e if x["status"] == "accepted"))


def fill_view():
    g = acme_graph()
    return View(list(g.records.values()), [lk for lk, _, _ in g.links.values()])


def date_checks(check):
    p = extract_schema.period
    check("dates: a month-name date parses ('Oct 03, 2026', '3 October 2026'); a word that only starts like a month does not",
          extract_schema.dates_in("Oct 03, 2026") and extract_schema.dates_in("Oct 03, 2026")[0][2] == "2026-10-03"
          and extract_schema.dates_in("3 October 2026")[0][2] == "2026-10-03" and not extract_schema.dates_in("Decision 12", "2026"))
    spec = common._reg.record("Decision").fields["effective_from"]
    check("dates: coerce reads a whole month-name date as a day; a period or a sentence is no date",
          extract_schema.coerce(spec, "Oct 08, 2026") == ("2026-10-08T00:00:00Z", "day")
          and extract_schema.coerce(spec, "Before Oct 03", "2026") is None and extract_schema.coerce(spec, "Oct 03", "2026")[0] == "2026-10-03T00:00:00Z")
    check("dates: periods by the table's own words: a range, an end word, a start word, a bare date (a start)",
          p("Oct 03-07", [], [], "2026") == ("2026-10-03", "2026-10-07") and p("Before Oct 03", ["From"], ["Before"], "2026") == (None, "2026-10-03")
          and p("From Oct 08", ["From"], ["Before"], "2026") == ("2026-10-08", None) and p("2026-01-01") == ("2026-01-01", None)
          and p("Before Oct 03", [], [], None) == (None, None))
    check("dates: the year of a table or document is the latest full year it writes", extract_schema.year_of(["x 2025-01-02", "2026-10-08"]) == "2026")


# ---------------------------------------------------------------- 5. quotes

def quote_checks(check):
    it = Item("P2", "x", "Motivation", "The DescribeQuorum API as defined in KIP-595 <https://cwiki.apache.org/x#y> is "
              "intended to allow the admin client to query the status of the Kraft quorum, including information about "
              "voter lag.\n\nAt this moment the API is not used. A majority of the controllers must be alive in order "
              "to maintain availability.")
    fq = fill.find_quote
    check("quotes: a hyperlink target written after the link's words is not part of the text a quote must match",
          fq("The DescribeQuorum API as defined in KIP-595 is intended to allow the admin client to query the status of the Kraft quorum", [it]))
    check("quotes: two sentences joined with '...' or a line break pass when each is word for word in the one passage",
          fq("At this moment the API is not used. ... A majority of the controllers must be alive in order to maintain availability.", [it])
          and fq("including information about voter lag.\nA majority of the controllers must be alive", [it]))
    check("quotes: sentences stitched from two different passages, or a paraphrased sentence, still fail",
          not fq("At this moment the API is not used. A majority of the brokers must be alive.", [it])
          and not fq("At this moment the API is not used. ... Another passage says this.", [it, Item("P3", "y", "t", "Another passage says this.")]))
    check("quotes: the prompt's own passage label before a quote is not part of it", fq("[P2] Motivation", [it]))
    bridge = Item("P5", "z", "Bridge Release", 'The overall plan for compatibility is to create a "bridge release" of Kafka.')
    check("quotes: a quotation mark the model changed (the text's \"bridge release\" written 'bridge release' inside its "
          "JSON string) is the same mark; the words must still be the text's",
          fq("The overall plan for compatibility is to create a 'bridge release' of Kafka.", [bridge])
          and not fq("The overall plan for compatibility is to create a 'bridge build' of Kafka.", [bridge]))
    cov = extract_facts._covered(["At this moment the API is not used.", "Something else entirely here.",
                                  "A majority of the controllers must be alive in order to maintain availability."],
                                 ["At this moment the API is not used. ... A majority of the controllers must be alive"])
    check("quotes: a joined quote covers the paragraph of each of its sentences, and not the one between", cov == {0, 2})


# ---------------------------------------------------------------- 6. reading

class Cut(f1.Model):
    """Answers a doc_facts window only when it holds at most `most` characters of text; else the reply is cut."""
    most = 1_500

    def ekg_doc_facts(self, prompt, schema):
        text = base.shown(prompt)
        if len(text) > self.most:
            return {"error": "window full: prompt 11007 + reply 16384 tokens", "window_full": True}
        p = base.StandIn.label_of(prompt, "Rule") or "P1"
        quotes = [ln for ln in text.splitlines() if ln.startswith("Rule ")]
        return {"records": [{"ref": f"r{i}", "type": "Guideline", "name": "", "statement": q, "fields": [], "quote": q,
                             "source": p} for i, q in enumerate(quotes)], "links": []}


def reading_checks(check):
    g = base.new_graph()
    long = "\n\n".join(f"Rule {i:02d}: deploys wait for the release gate and are recorded in the ledger every time." for i in range(40))
    base.add_doc(g, "runbook.md", [("Deploying", 1, [("text", long)])])
    model = Cut()
    rep, out, tmp = f1.run_fill(g, model, ["doc_facts"], window=131_072, reply_limit=16_384)
    e = [x for x in base.rows(out / "ledger.jsonl") if x["kind"] == "doc_facts"][0]
    halves = [p for n, p in model.calls if n == "ekg_doc_facts" and "(part 1 of 2 of this passage)" in p]
    recs = [r for r in base.written(out, tmp)[0] if r["type"] == "Guideline"]
    check("reading: one passage whose reply is cut at the provider's limit is asked again as two halves of its text "
          "(same passage, so every quote still cites it), down to parts the reply can hold",
          halves and e["status"] == "accepted" and not e.get("unanswered") and len(recs) == 40)
    check("ask_split: a cut reply is told from other failures; a short passage is not cut", fill.reply_cut({"error": "window full: x"})
          and not fill.reply_cut({"error": "retries exhausted"}) and fill.halve_text(Item("P1", "x", "t", "short")) is None)

    async def partial():
        calls = []

        async def ask(prompt, name, schema):
            calls.append(prompt)
            return {"error": "retries exhausted"} if "BAD" in prompt else {"records": [], "links": []}
        items = [Item("P1", "a", "t", "good one"), Item("P2", "b", "t", "BAD two")]
        return await fill.ask_split(ask, items, lambda ss: "\n".join(s.text for s in ss), "ekg_doc_facts", lambda labels: {}), calls
    (got, failed), calls = asyncio.run(partial())
    check("ask_split: when one half fails, the half that was answered is kept; the failed passages are named for the ledger",
          len(got) == 1 and got[0][0][0].label == "P1" and fill.unanswered(failed)["unanswered"] == ["P2"])

    cat = fill_view().catalog()
    prompt = extract_facts.prompt(type("T", (), {"context": "DOCUMENT: x", "question": "q", "sources": []})(), cat, [])
    know = common._reg.record("Knowledge")
    check("reading: the document prompt says what is worth keeping in prose, from the registry's own descriptions "
          "(Knowledge and its kinds, Decision, Guideline, Requirement), and reads 'we' as the document's team",
          "WORTH KEEPING" in prompt and extract_schema.first_sentence(know.about).rstrip(";.") in prompt
          and know.fields["kind"].about in prompt
          and extract_schema.first_sentence(common._reg.record("Guideline").about).rstrip(";.") in prompt
          and "speaks for a team" in prompt)


def guard_checks(check):
    saved = list(fill.PACE)
    try:
        fill.PACE.clear()
        idle = fill.slow_pace(40)
        fill.PACE.extend([60, 55, 12, 10, 58, 9, 61, 11])
        loaded = fill.slow_pace(40)
    finally:
        fill.PACE.clear()
        fill.PACE.extend(saved)
    check("guard: with no calls seen the time guard keeps its floor pace (40 tokens/s); when this run's calls were slower "
          "(several runs at once), it follows the slowest quarter of them", idle == 40 and loaded == 11)

    async def gated():
        import tempfile
        orig = fill.llm.ask

        async def slow(prompt, name, schema):
            await asyncio.sleep(0.6)
            return {"ok": prompt}
        fill.llm.ask = slow
        try:
            with tempfile.TemporaryDirectory() as d:
                r = fill.Responses(Path(d) / "responses.jsonl", "m", concurrency=1)
                got = await asyncio.gather(*[r.ask("t", f"p{i}", "n", {}, timeout=lambda: 1.0) for i in range(3)])
                r.close()
        finally:
            fill.llm.ask = orig
        return got
    got = asyncio.run(gated())
    check("guard: the time limit starts when a call is sent, not while it waits for a free slot (three 0.6 s calls, one "
          "at a time, each within a 1 s limit)", all("error" not in g for g in got))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(base.QM)
    try:
        table_checks(check)
        date_checks(check)
        quote_checks(check)
        reading_checks(check)
        guard_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
