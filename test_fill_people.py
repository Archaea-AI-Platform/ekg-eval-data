"""One person, one record: the ACME directory shapes, with the stand-in model (no key, no network, no cost).

The table texts below are the ACME LDAP directory PDF's tables exactly as the PDF reader now writes them: a cell
written on two lines in the PDF ("E003 Lionel" over "eng-orders") keeps its line break, rendered " / ". The
stand-in gives the mappings a model gives for those shapes; code does the rest.

    python -I ekg-eval-data/test_fill_people.py
"""

import collections
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402
import test_fill_followups as f1  # noqa: E402
from ekg_adapters import common  # noqa: E402

SCOPE = base.SCOPE
DIRECTORY_1 = """ID / employee | Role / team | Manager | GitHub / email
E001 Mack / eng-all | Director of Engineering / Engineering Leadership | Cambria | @mack / mack@acme.com
E002 Mahesh / eng-all | Engineering Manager, Backend / Backend Engineering | Mack | @mahesh / mahesh@acme.com
E003 Lionel / eng-orders | Software Engineer / Order Management | Mahesh | @lionel / lionel@acme.com
E004 Suzan / eng-payments | Senior Software Engineer / Payment & Billing | Mahesh | @suzan / suzan@acme.com
E005 Emma / eng-payments | Software Engineer / Payment & Billing | Mahesh | @emma / emma@acme.com
E006 Alex / eng-payments | Software Engineer / Payment & Billing / Inventory integrations | Mahesh | @alex / alex@acme.com
E007 Jonathan / eng-ops | Engineering Manager / Shipment & Operations | Mack | @jonathan / jonathan@acme.com
E008 Noah Patel / eng-ops | Software Engineer / Shipment & Operations | Jonathan | @noahp / noah.patel@acme.com
E009 Maya Chen / eng-ops | Senior Software Engineer / Shipment & Operations | Jonathan | @mayac / maya.chen@acme.com
E010 Priya Nair / eng-ops | Software Engineer / Shipment & Operations | Jonathan | @priyan / priya.nair@acme.com
E011 Sophia / eng-customer | Engineering Manager, UI / Customer Experience Engineering | Mack | @sophia / sophia@acme.com
E012 Lilian / eng-customer | Software Engineer / Customer Communications | Sophia | @lilian / lilian@acme.com
E013 Lisa / eng-customer | Software Engineer / Customer Communications | Sophia | @lisa / lisa@acme.com
E014 Batuista / eng-analytics | Software Engineer / Analytics & Reporting | Sophia | @batuista / batuista@acme.com
E015 Ben / eng-platform | Senior Engineering Manager, DevOps / Infrastructure | Mack | @ben / ben@acme.com
E016 Jerry / eng-platform | DevOps Engineer / Infrastructure | Ben | @jerry / jerry@acme.com
E017 Javier / eng-platform | DevOps Engineer / Infrastructure | Ben | @javier / javier@acme.com"""
DIRECTORY_2 = """ID / employee | Responsibility | Manager | Email
E018 Charolette / pm-orders | Lead Program Manager / Order Management | Mack | charolette@acme.com
E019 Kim / pm-payments | Product Manager / Payment & Billing | Charolette | kim@acme.com
E020 Amir / pm-ops | Product Manager / Shipment & Operations | Charolette | amir@acme.com
E021 Emily / pm-inventory | Product Manager / Inventory & Availability | Charolette | emily@acme.com
E022 Suzie / finance-ledger | Director of Finance / Finance | Cambria | suzie@acme.com
E023 Evelyn / sales | Director of Sales / Sales | Cambria | evelyn@acme.com
E024 Bob / support | Director of Customer Support / Customer Support | Cambria | bob@acme.com
E025 Kox / support | Support Lead, America / Customer Support | Bob | kox@acme.com
E026 Peter / hr-people-admin | Director of HR / People Operations | Cambria | peter@acme.com
E027 Cambria / executive | CEO / Executive | - | cambria@acme.com"""
STEWARDS = """Module | Current steward | Manager | Effective
PAY-AUTH | Suzan / @suzan | Mahesh | 2026-10-08
PAY-CAP | Suzan / @suzan | Mahesh | 2026-09-01
PAY-REF | Emma / @emma | Mahesh | 2026-08-01
PAY-PROMO | Alex / @alex | Mahesh | 2026-07-01
PAY-POINTS | Emma / @emma | Mahesh | 2026-08-01
ORD-CORE | Lionel / @lionel | Mahesh | 2026-10-03
INV-RES | Alex / @alex | Mahesh | 2026-07-01
OPS-FULF | Noah Patel / @noahp | Jonathan | 2026-01-01
OPS-PACK | Maya Chen / @mayac | Jonathan | 2026-01-01
OPS-LABEL | Priya Nair / @priyan | Jonathan | 2026-10-14
OPS-CARRIER | Priya Nair / @priyan | Jonathan | 2026-09-01
COM-NOTIFY | Lilian / @lilian | Sophia | 2026-01-01
ANA-REPORT | Batuista / @batuista | Sophia | 2026-01-01"""
PERIODS = """Period | Person / module | Record
Before Oct 03 | Lionel � PAY-AUTH | Primary implementation contributor; assigned to Payments.
Oct 03-07 | Mahesh � PAY-AUTH | Interim escalation while Lionel transferred to Order Management.
From Oct 08 | Suzan � PAY-AUTH | Current accountable engineer; approved/implemented recent auth work.
Before Oct 14 | Noah Patel � OPS-LABEL | Original shipping-label steward and largest contributor.
From Oct 14 | Priya Nair � OPS-LABEL | Current accountable engineer; Noah remains on fulfillment tasks."""
EMPLOYEES = [line.split(" | ")[0].split(" / ")[0].split(" ", 1)[1] for t in (DIRECTORY_1, DIRECTORY_2) for line in t.split("\n")[1:]]

link = lambda col, ltype, other, row_is="from", split="none", fields=(): {
    "column": col, "link": ltype, "other_type": other, "row_is": row_is, "split": split,
    "fields": [{"column": c, "field": f} for c, f in fields]}
ID_NAME_TEAM = {"column": "c1", "parts": 3, "separator": "", "pattern": r"(\S+)\s+(.+?) / (.+)"}
DIRECTORY_1_MAP = f1.empty_kv(
    row_type="Person", name_column="c1.2", key_column="c1.1", key_field="hr_worker_id", statement_column="none",
    cell_parts=[ID_NAME_TEAM, {"column": "c2", "parts": 2, "separator": " / ", "pattern": ""},
                {"column": "c4", "parts": 2, "separator": " / ", "pattern": ""}],
    fields=[{"column": "c2.1", "field": "title"}, {"column": "c4.2", "field": "work_email"}],
    links=[link("c1.3", "MEMBER_OF", "Group"), link("c2.1", "HOLDS_POSITION", "Position"), link("c3", "REPORTS_TO", "Person"),
           link("c4.1", "HAS_ACCOUNT", "Account")])
DIRECTORY_2_MAP = f1.empty_kv(
    row_type="Person", name_column="c1.2", key_column="c1.1", key_field="hr_worker_id", statement_column="none",
    cell_parts=[ID_NAME_TEAM, {"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
    fields=[{"column": "c2.1", "field": "title"}, {"column": "c4", "field": "work_email"}],
    links=[link("c1.3", "MEMBER_OF", "Group"), link("c2.1", "HOLDS_POSITION", "Position"), link("c3", "REPORTS_TO", "Person")])
STEWARDS_MAP = f1.empty_kv(
    row_type="Person", name_column="c2.1", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": " / ", "pattern": ""}],
    links=[link("c1", "KNOWS_ABOUT", "Service", fields=[("c4", "valid_from")]), link("c3", "REPORTS_TO", "Person"),
           link("c2.2", "HAS_ACCOUNT", "Account")])
PERIODS_MAP = f1.empty_kv(
    row_type="Person", name_column="c2.1", key_field="none", statement_column="none",
    cell_parts=[{"column": "c2", "parts": 2, "separator": " � ", "pattern": ""}],
    links=[link("c2.2", "KNOWS_ABOUT", "Service")])
FUSED_MAP = {**DIRECTORY_1_MAP, "name_column": "c1.2", "cell_parts": [{"column": "c1", "parts": 2, "separator": "", "pattern": r"(\S+)\s+(.+)"}],
             "fields": [], "links": [link("c3", "REPORTS_TO", "Person")]}


class Model(f1.Model):
    def ekg_table_map(self, prompt, schema):
        if '"Current steward"' in prompt:
            return STEWARDS_MAP
        if '"Person / module"' in prompt:
            return PERIODS_MAP
        return DIRECTORY_1_MAP if '"GitHub / email"' in prompt else DIRECTORY_2_MAP

    def ekg_doc_facts(self, prompt, schema):
        p = base.StandIn.label_of(prompt, "Lionel moved to Order Management")
        if p is None:
            return {"records": [], "links": []}
        return {"records": [{"ref": "r1", "type": "Person", "name": "Lionel", "statement": "", "source": p, "fields": [],
                             "quote": "Lionel moved to Order Management"}], "links": []}


def directory_checks(check):
    g = base.base_graph()
    # the poorer tables come first in the document: the directory (most identifying columns) still writes first
    doc, pieces = base.add_doc(g, "ACME_LDAP_Employee_Directory.pdf", [
        ("page 3", 1, [("table", STEWARDS), ("table", PERIODS)]),
        ("page 1", 1, [("table", DIRECTORY_1)]), ("page 2", 1, [("table", DIRECTORY_2)]),
        ("page 4", 1, [("text", "On October 3 Lionel moved to Order Management. " * 3)])])
    rep, out, tmp = f1.run_fill(g, Model(), ["table_map", "doc_facts"])
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    people = collections.Counter(r["props"].get("full_name") for r in by_id.values() if r["type"] == "Person")
    L = collections.defaultdict(set)
    for lk in links:
        L[lk["type"]].add((base._name(by_id[lk["a"]]), base._name(by_id[lk["b"]])))
    check(f"directory: four tables and a paragraph about 27 employees make exactly 27 Person records, one each "
          f"(got {sum(people[n] for n in people if n != 'Bob Kumar')})",
          {n: k for n, k in people.items() if n != "Bob Kumar"} == {n: 1 for n in EMPLOYEES})
    check("directory: the id / name / team cell is split into its three values: name 'Lionel', id E003, team eng-orders",
          any(r["props"].get("full_name") == "Lionel" and r["props"].get("hr_worker_id") == "E003" for r in recs)
          and ("Lionel", "eng-orders") in L["MEMBER_OF"] and not any(" / " in n or "eng-" in n for n in people))
    check("directory: every employee is MEMBER_OF their team and HOLDS_POSITION their title (27 each)",
          len({a for a, _ in L["MEMBER_OF"] if a in EMPLOYEES}) == 27 and len({a for a, _ in L["HOLDS_POSITION"]}) == 27
          and ("Noah Patel", "Software Engineer") in L["HOLDS_POSITION"])
    check("directory: REPORTS_TO for the 26 with a manager; Cambria's '-' is a placeholder, never a person, and counted",
          len({a for a, _ in L["REPORTS_TO"]}) == 26 and "-" not in people
          and c.get("placeholder cell (-, n/a ...): no link end, left out") == 1)
    accounts = collections.Counter(r["props"].get("login") for r in by_id.values() if r["type"] == "Account" and r["props"].get("tool") != "slack")
    check("directory: a login column linked by HAS_ACCOUNT makes the person's Account once; the steward table's "
          "'@suzan' resolves to it", accounts.get("suzan") == 1
          and any(by_id[lk["a"]]["props"].get("full_name") == "Suzan" and by_id[lk["b"]]["props"].get("login") == "suzan"
                  for lk in links if lk["type"] == "HAS_ACCOUNT")
          and all(k == 1 for login, k in accounts.items() if login))
    check("directory: the steward and period tables link the directory's records (Suzan -> PAY-AUTH, Priya Nair -> OPS-LABEL)",
          ("Suzan", "PAY-AUTH") in L["KNOWS_ABOUT"] and ("Priya Nair", "OPS-LABEL") in L["KNOWS_ABOUT"])
    check("directory: the prose 'Lionel' resolves to the directory's Lionel", c.get("Person: resolved to the one existing record", 0) >= 1)
    check("directory: no possible duplicates are reported", rep["written"]["possible_duplicates"]["records"] == 0)
    check("directory: every written record and link validates", not base.all_valid(recs, links, by_id))
    check("report: coverage_by_document is at the top of the report too", "coverage_by_document" in rep)


def prefix_checks(check):
    """The v3 shape: the directory's name came out as 'Lionel eng-orders' and other rows say 'Lionel'."""
    g = base.base_graph()
    team = g.rec("Group", "hr", "eng-orders", {"name": "eng-orders"}, SCOPE)
    lio = g.rec("Person", "hr", "e003", {"full_name": "Lionel eng-orders", "hr_worker_id": "E003"}, SCOPE)
    g.link("MEMBER_OF", lio, team, "field")
    g.rec("Person", "hr", "e099", {"full_name": "Alex E006", "hr_worker_id": "E006"}, SCOPE)
    g.rec("Person", "hr", "e008", {"full_name": "Noah Patel"}, SCOPE)
    g.rec("Person", "hr", "alex", {"full_name": "Alex"}, SCOPE)       # a pair already in the graph: reported
    base.add_doc(g, "stewards.pdf", [("Stewards", 1, [("table", "Module | Current steward | Manager | Effective\n"
                                                                 "ORD-CORE | Lionel / @lionel | Mahesh Rao | 2026-10-03\n"
                                                                 "OPS-FULF | Noah / @noahp | n/a | 2026-01-01")])])
    m = {**STEWARDS_MAP, "links": [link("c1", "KNOWS_ABOUT", "Service"), link("c3", "REPORTS_TO", "Person")]}
    f1.Model.tables = {'"Current steward"': m}
    rep, out, tmp = f1.run_fill(g, f1.Model(), ["table_map"])
    recs, links, by_id = base.written(out, tmp)
    c = rep["written"]["documents_and_tables"]
    names = collections.Counter(r["props"].get("full_name") for r in by_id.values() if r["type"] == "Person")
    know = {(base._name(by_id[lk["a"]]), base._name(by_id[lk["b"]])) for lk in links if lk["type"] == "KNOWS_ABOUT"}
    check("prefix: 'Lionel' is the one 'Lionel eng-orders' whose own team is eng-orders: the same person, no new record",
          ("Lionel eng-orders", "ORD-CORE") in know and "Lionel" not in names)
    check("prefix: 'Noah' is not 'Noah Patel' (the rest is no value of that record): a name alone never joins two",
          names.get("Noah") == 1 and names.get("Noah Patel") == 1)
    check("prefix: 'n/a' is a placeholder, not a manager", c.get("placeholder cell (-, n/a ...): no link end, left out") == 1
          and "n/a" not in names)
    dup = rep["written"]["possible_duplicates"]
    check("duplicates: one Person's name being another's name plus one of its own values (Alex + 'Alex E006') is reported; "
          "Noah / Noah Patel is not", any(set(e["records"]) == {"Alex", "Alex E006"} and e["kind"] == "name plus a value of the record"
                                          for e in dup["examples"])
          and not any("Noah Patel" in e["records"] for e in dup["examples"]))

    # the composite check catches the fused v3 cell shape: an id beside the name
    g = base.base_graph()
    base.add_doc(g, "dir.pdf", [("Directory", 1, [("table", DIRECTORY_1)])])
    f1.Model.tables = {'"GitHub / email"': {**DIRECTORY_1_MAP, "name_column": "c1", "cell_parts": [], "key_column": "none",
                                             "key_field": "none", "fields": [], "links": []}}
    rep, out, tmp = f1.run_fill(g, f1.Model(), ["table_map"])
    led = [e for e in base.rows(out / "ledger.jsonl") if e["kind"] == "table_map"]
    check("composite: 'E003 Lionel / eng-orders' as a person's name is rejected (asked again once, then left to be read as text)",
          led[0]["status"] == "rejected" and "more than a name" in led[0]["reason"] and led[0].get("retried_after"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(base.QM)
    try:
        directory_checks(check)
        prefix_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
