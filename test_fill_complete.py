"""Behaviour tests for the graph-completeness fixes to the document reading (stand-in model: no key, no network, no cost).

  1. a stated record keeps the source's whole statement: a statement the model worded that drops a word of its quote
     (a clause, an exact value) is replaced by the quote, word for word from the source;
  2. a thing named by an id of its own in several documents (a case, an order) is one Topic, mentioned from each;
     never a heading of the document, and an existing record's name is that record;
  3. the document itself, listed by its title as shown, is the document's own record (who maintains it);
  4. a value a text states for an existing record's field (a person's status) is given to it when it has none;
  5. two dated owners of one thing give no computed Handover: the graph holds what the sources state.

    python -I ekg-eval-data/test_fill_complete.py
"""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402  (fixtures and the stand-in model)
from ekg_adapters import common, extract_facts  # noqa: E402

SCOPE = base.SCOPE
SHIP = ("SHIP-06 Do not authorize carrier handoff earlier than the 30-minute cancellation boundary, regardless of when "
        "payment clears or pack completes.")
CASE_OPS = "Case O-8201 was handed to the carrier at 09:11 before the cancellation window ended."
CASE_PAY = "For case O-8201 / early capture the card was captured for $160.92 at 09:03; the Platform team reconciles it."
OWNER = "This document is maintained by the Platform team."
STATUS = "Bob Kumar has accountStatus=active in the directory, employee number E999."
SECRET = "Dana Holt has accountStatus=suspended."
LATER = "Carrier release is described in its own section below."
PROGRAMME = "The 2026 Programme starts in spring."


def statement_checks(check):
    ws = extract_facts.whole_statement
    short = "Do not authorize carrier handoff earlier than the 30-minute cancellation boundary."
    check("statement: a worded statement that drops a clause is replaced by its quote (the item's own id left out)",
          ws(short, SHIP) == SHIP.split(" ", 1)[1])
    check("statement: one that keeps every word of the quote stays as the model wrote it",
          ws(SHIP.split(" ", 1)[1] + " (shipping team)", SHIP) == SHIP.split(" ", 1)[1] + " (shipping team)")
    check("statement: a quote shorter than the statement is only part of it: the statement stays",
          ws("The reconciliation job runs at 04:00 UTC and a failure is flagged FIN_RECON_DELAYED.", "runs at 04:00 UTC")
          == "The reconciliation job runs at 04:00 UTC and a failure is flagged FIN_RECON_DELAYED.")
    check("statement: words are compared as letters and digits in any script, case apart",
          ws("Платёж до 04:00", "платёж до 04:00 UTC") == "платёж до 04:00 UTC" and ws("Ab 1", "ab 1") == "Ab 1")


class Reader(base.StandIn):
    """Answers each passage it is shown with the records a careful reader lists."""

    def ekg_doc_facts(self, prompt, schema):
        recs, links = [], []
        rec = lambda ref, t, name, quote, src, statement="", fields=(): recs.append({
            "ref": ref, "type": t, "name": name, "statement": statement, "quote": quote, "source": src,
            "fields": [{"field": f, "value": val} for f, val in fields]})
        if (p := self.label_of(prompt, "SHIP-06")):
            rec("r1", "Requirement", "SHIP-06 carrier handoff", SHIP, p,
                "Do not authorize carrier handoff earlier than the 30-minute cancellation boundary.")
        for n, sentence in enumerate((CASE_OPS, CASE_PAY)):
            if (p := self.label_of(prompt, sentence)):
                rec(f"c{n}", "Topic", "O-8201" if n == 0 else "O-8201 / early capture", sentence, p)
                if n == 1:
                    rec("h1", "Topic", "Cases", sentence, p)       # the passage's heading: a place, not a thing
                    rec("h2", "Topic", "Platform", sentence, p)    # an existing team's name: that team
                rec(f"k{n}", "Knowledge", "", sentence, p, sentence, [("kind", "fact")])
                links.append({"type": "ABOUT", "from": f"k{n}", "to": f"c{n}", "quote": sentence, "source": p, "fields": []})
        if (p := self.label_of(prompt, PROGRAMME)):
            rec("y1", "Topic", "2026 Programme", PROGRAMME, p)   # starts with a number; a heading "Programme" exists
        if (p := self.label_of(prompt, LATER)):
            rec("h0", "Topic", "Carrier release", LATER, p)   # another section's heading, without its number
        if (p := self.label_of(prompt, OWNER)):
            title = prompt.split("DOCUMENT: ", 1)[1].split("\n", 1)[0]
            rec("d1", "File", title, OWNER, p)
            rec("d2", "Topic", title, OWNER, p)   # the title is the document's name, not a topic's: must be in the text
            rec("g1", "Group", "Platform", "the Platform team", p)
            links.append({"type": "MAINTAINED_BY", "from": "d1", "to": "g1", "quote": OWNER, "source": p, "fields": []})
        if (p := self.label_of(prompt, STATUS)):
            rec("b1", "Person", "Bob Kumar", STATUS, p, "", [("status", "active"), ("person_id", "E999"),
                                                               ("employment_type", "contractor")])
        if (p := self.label_of(prompt, SECRET)):
            rec("s1", "Person", "Dana Holt", SECRET, p, "", [("status", "suspended")])
        return {"records": recs, "links": links}


def graph():
    g = base.base_graph()
    base.add_doc(g, "ops.pdf", [("Rules", 1, [("text", SHIP), ("text", CASE_OPS), ("text", OWNER), ("text", LATER)]),
                                ("6. Carrier release", 1, [("text", "Release needs a carrier scan.")])])
    base.add_doc(g, "pay.pdf", [("Cases", 1, [("text", CASE_PAY), ("text", STATUS), ("text", PROGRAMME)]),
                                ("Platform", 1, [("text", "The team section.")]), ("Programme", 1, [("text", "Plans.")])])
    g.rec("Person", "hr", "dana", {"full_name": "Dana Holt"}, SCOPE)
    _, secret = base.add_doc(g, "hr.pdf", [("Status", 1, [("text", SECRET)])])
    for r in [*secret, *(r for r in g.records.values() if r["props"].get("native_id", "").startswith("hr.pdf"))]:
        r["scope"] = [*SCOPE, ["hr-only"]]   # a passage fewer people may read than may see Dana's record
    # two dated owners of one service from a directory: Lionel until 2026-10-08, Suzan from 2026-10-08
    svc = g.rec("Service", "directory", "pay-auth", {"name": "PAY-AUTH"}, SCOPE)
    lionel = g.rec("Person", "hr", "lionel", {"full_name": "Lionel Ortiz"}, SCOPE)
    suzan = g.rec("Person", "hr", "suzan", {"full_name": "Suzan Kay"}, SCOPE)
    g.link("OWNED_BY", svc, lionel, "field", valid_from="2026-07-01T00:00:00Z", valid_to="2026-10-08T00:00:00Z")
    g.link("OWNED_BY", svc, suzan, "field", valid_from="2026-10-08T00:00:00Z")
    g.link("OWNED_BY", svc, suzan, "field")   # undated: says nothing about when
    return g


def reading_checks(check):
    model = Reader()
    rep, out, tmp = base.run_fill(graph(), model, ["doc_facts"], window=80_000)
    recs, links, by_id = base.written(out, tmp)
    name = lambda r: r["props"].get("name") or r["props"].get("full_name") or r["props"].get("title") or ""
    req = [r for r in recs if r["type"] == "Requirement"]
    check("statement: SHIP-06 keeps its second clause, word for word from the PDF",
          len(req) == 1 and req[0]["props"]["statement"].endswith("regardless of when payment clears or pack completes."))
    topics = [r for r in recs if r["type"] == "Topic" and name(r) == "O-8201"]
    mentions = {lk["a"] for lk in links if lk["type"] == "MENTIONS" and topics and lk["b"] == topics[0]["id"]}
    pieces = {by_id[i]["props"]["text"] for i in mentions}
    about = [lk for lk in links if lk["type"] == "ABOUT" and topics and lk["b"] == topics[0]["id"]]
    check("case id: O-8201 named in two documents is ONE Topic, mentioned from both documents' passages, with the facts ABOUT it",
          len(topics) == 1 and pieces == {CASE_OPS, CASE_PAY} and len(about) == 2)
    ops = next(r for r in by_id.values() if r["type"] == "File" and r["props"].get("name") == "ops.pdf")
    owner = [lk for lk in links if lk["type"] == "MAINTAINED_BY" and lk["a"] == ops["id"]]
    check("document: the document listed by its title as shown is its own record: ops.pdf MAINTAINED_BY the Platform team",
          len(owner) == 1 and by_id[owner[0]["b"]]["props"].get("name") == "Platform"
          and not any(r["type"] == "File" and r["id"] != ops["id"] and name(r) in ("ops", "ops.pdf") for r in recs))
    bob = [r for r in recs if r["type"] == "Person" and r["props"].get("full_name") == "Bob Kumar"]
    check("fields: a status the text states is given to the existing Person (patched, cited), nothing else changed",
          len(bob) == 1 and bob[0]["props"].get("status") == "active" and "status" in bob[0]["props"].get("_stated", {})
          and bob[0]["props"].get("work_email") == "bob@acme.example")
    check("fields: an identifying field (person_id) and a value not word for word in the passage are never given",
          bob and "person_id" not in bob[0]["props"] and "employment_type" not in bob[0]["props"])
    dana = [r for r in by_id.values() if r["type"] == "Person" and r["props"].get("full_name") == "Dana Holt"]
    check("fields: a passage visible to fewer people than the record gives it nothing (its scope is not the record's)",
          dana and "status" not in dana[0]["props"])
    check("document: the title exemption holds only for the document's own type: no Topic named after the document",
          not any(r["type"] == "Topic" and name(r) == "ops" for r in recs))
    owners = [lk for lk in base.rows(tmp / "links.jsonl") if lk["type"] == "OWNED_BY" and lk["props"].get("valid_from")]
    check("hand-over: none is computed from two dated owners (no Handover, no SUCCEEDS); the dated OWNED_BY links stay "
          "for the reader to reason over",
          not any(r["type"] == "Handover" for r in recs) and not any(lk["type"] == "SUCCEEDS" for lk in links)
          and len(owners) == 2 and "handovers" not in rep)
    check("topic: a case named with words after its id is named by the id alone (one O-8201, no 'O-8201 / early capture')",
          not any(r["type"] == "Topic" and "early capture" in name(r) for r in recs))
    check("topic: a heading of the document is never a Topic; an existing team's name is that team, not a Topic beside it",
          not any(r["type"] == "Topic" and name(r) in ("Cases", "Platform", "Carrier release") for r in recs)
          and rep["written"]["documents_and_tables"].get("Topic: the name is another record's: that record") == 1)
    check("topic: a name starting with a number is never cut to match a heading ('2026 Programme' beside 'Programme')",
          any(r["type"] == "Topic" and name(r) == "2026 Programme" for r in recs))
    check("topic: an existing record's name resolves first, even when a section has that heading (Platform)",
          not any(r["type"] == "Topic" and name(r) == "Platform" for r in recs)
          and rep["written"]["documents_and_tables"].get("Topic: the name is another record's: that record") == 1)
    check("valid: every record and link written validates", not base.all_valid(recs, links, by_id))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}", flush=True)

    common.bind(base.QM)
    try:
        statement_checks(check)
        reading_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
