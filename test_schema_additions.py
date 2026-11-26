"""Behaviour tests for the four schema additions of 2026-11-26 (stand-in model: no key, no network, no cost).

  1. DEPENDS_ON between requirements: a requirement that waits on another (kind gate) is linked to it;
  2. a group's access a directory states: CAN_ACCESS from the group to the scope the document describes (with the
     level the text states). Never to a tool's own place: a scope a tool made is not matched, patched or linked, and
     what every asker can see is the same with and without the document's grants;
  3. CONTRIBUTED_TO from a contributions table: count from the cell, rank and kind from the column header (`fixed`
     now takes a number field and a link's named values);
  4. an Incident a document recounts: title, summary and timeline, ABOUT the case's Topic (still one Topic);
  plus the table-mapping cache: a mapping that reported a schema gap (or was rejected) is asked again once the
  registry changes; a fully read mapping is kept.

    python -I ekg-eval-data/test_schema_additions.py
"""

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fill_documents as base  # noqa: E402  (fixtures and the stand-in model)
from test_fill_followups import Model, empty_kv  # noqa: E402
from ekg_adapters import access, common, extract_schema, extract_tables, fill, sqlite_store  # noqa: E402

SCOPE = base.SCOPE
ORD07 = ("ORD-07 Do not advance an order for warehouse release until ORD-03 is met and the payments service has "
         "authorized it.")
ORD03 = "ORD-03 An order is allocated in full before release."
GROUPS = "eng-payments / repo-pay-write: payment engineering source write and reviews; financial data access is separate."
ROGUE = "eng-payments also holds github-pay-repo."
INC_HEAD = "O-8201: captured and handed to carrier before the cancellation window ended"
TIMELINE = ("09:00 Order O-8201 placed, card authorization $160.92\n09:03 Card captured in full\n"
            "09:11 Package handed to the carrier\n09:12 Cancellation request arrived")
CONTRIB = ("table", "Module | Most commits | Second most\nPAY-AUTH | Lionel 42 | Suzan 24\nPAY-CAP | Suzan 31 | Emma 19")


class Reader(Model):
    def ekg_doc_facts(self, prompt, schema):
        recs, links = [], []
        rec = lambda ref, t, name, quote, src, statement="", fields=(): recs.append({
            "ref": ref, "type": t, "name": name, "statement": statement, "quote": quote, "source": src,
            "fields": [{"field": f, "value": val} for f, val in fields]})
        lk = lambda t, a, b, quote, src, fields=(): links.append({"type": t, "from": a, "to": b, "quote": quote, "source": src,
                                                                   "fields": [{"field": f, "value": v} for f, v in fields]})
        if (p := self.label_of(prompt, ORD07)) and (q := self.label_of(prompt, ORD03)):
            rec("o7", "Requirement", "ORD-07 release gate", ORD07, p, ORD07.split(" ", 1)[1])
            rec("o3", "Requirement", "ORD-03 full allocation", ORD03, q, ORD03.split(" ", 1)[1])
            rec("ps", "Service", "payments service", ORD07, p)
            lk("DEPENDS_ON", "o7", "o3", "until ORD-03 is met", p, [("kind", "gate")])
            lk("DEPENDS_ON", "o7", "ps", "the payments service has authorized it", p, [("kind", "gate")])
        if (p := self.label_of(prompt, GROUPS)):
            rec("g1", "Group", "eng-payments", GROUPS, p)
            rec("s1", "AccessScope", "repo-pay-write", GROUPS, p, "", [("kind", "repo"), ("history", "since_joined")])
            lk("CAN_ACCESS", "g1", "s1", GROUPS, p, [("level", "write")])
        if (p := self.label_of(prompt, ROGUE)):   # names a tool's own place: must never be granted from a text
            rec("g2", "Group", "eng-payments", ROGUE, p)
            rec("s2", "AccessScope", "github-pay-repo", ROGUE, p, "", [("history", "all")])
            lk("CAN_ACCESS", "g2", "s2", ROGUE, p, [("level", "admin")])
        if (p := self.label_of(prompt, "09:03 Card captured")):
            rec("t1", "Topic", "O-8201", TIMELINE.split("\n")[0], p)
            rec("i1", "Incident", INC_HEAD, TIMELINE.split("\n")[0], p,
                "The card was captured and the package handed to the carrier before the cancellation window ended.",
                [("timeline", TIMELINE.replace("\n", "; "))])
            lk("ABOUT", "i1", "t1", TIMELINE.split("\n")[0], p)
        return {"records": recs, "links": links}


def graph():
    g = base.base_graph()
    base.add_doc(g, "orders.pdf", [("Rules", 1, [("text", ORD07), ("text", ORD03)])])
    base.add_doc(g, "ldap.pdf", [("Directory groups", 1, [("text", GROUPS), ("text", ROGUE)])])
    base.add_doc(g, "ship.pdf", [(INC_HEAD, 2, [("text", TIMELINE)]), ("Other cases", 1, [("text", "O-8201 is reviewed weekly.")])])
    base.add_doc(g, "github.pdf", [("Contributions by code area", 1, [CONTRIB])])
    g.rec("Service", "catalog", "payments", {"name": "payments service"}, SCOPE)
    for n in ("PAY-AUTH", "PAY-CAP"):
        g.rec("Service", "catalog", n.lower(), {"name": n}, SCOPE)
    suzan = None
    for n in ("Lionel", "Suzan", "Emma"):
        p = g.rec("Person", "hr", n.lower(), {"full_name": n}, SCOPE)
        suzan = p if n == "Suzan" else suzan
    grp = g.rec("Group", "directory", "eng-payments", {"name": "eng-payments", "kind": "hr_team"}, SCOPE)
    g.link("MEMBER_OF", suzan, grp, "field")
    # a tool's own place (a private repository) that a future adapter names, and a record only its members may read
    scope, place = access.place(g, "github", "repo", "pay-repo", public=False)
    place["props"]["name"] = "github-pay-repo"
    g.rec("Repository", "github", "acme/pay", {"name": "acme/pay"}, scope)
    return g


GATE = "REL-01 Do not release an order until fraud risk clearance passes the configured gate."
RISK = "RISK-04 Fraud risk review must clear before any release; an open review blocks pick release."
LOOSE = "LOG-02 Release notes are written in English."
CASE_PAY = "O-8201 / early capture: the card was captured at 09:03 before packing."
CASE_OPS = "O-8201 was handed to the carrier at 09:11."
LEVEL = "The ops-admins group may write to the deploy console."
LEVEL2 = "The release-managers group can push to the release branch."
THIN = ("table", "Module | Most commits | Second most\nOPS-PACK | Maya 29 | Priya 8\nOPS-CARRIER | Priya 24 | Maya 9")


class GateReader(Model):
    def ekg_doc_facts(self, prompt, schema):
        recs, links = [], []
        rec = lambda ref, t, name, quote, src, statement="", fields=(): recs.append({
            "ref": ref, "type": t, "name": name, "statement": statement, "quote": quote, "source": src,
            "fields": [{"field": f, "value": val} for f, val in fields]})
        for ref, text in (("g", GATE), ("r", RISK), ("l", LOOSE)):
            if (p := self.label_of(prompt, text)):   # each item alone: no link from the reading itself
                rec(ref, "Requirement", text.split(" ", 1)[0], text, p, text.split(" ", 1)[1])
        if (p := self.label_of(prompt, CASE_OPS)):
            rec("t", "Topic", "O-8201", CASE_OPS, p)
        if (p := self.label_of(prompt, CASE_PAY)):   # the incident only: the reading gives it no ABOUT
            rec("i", "Incident", "O-8201 / early capture", CASE_PAY, p, CASE_PAY)
        if (p := self.label_of(prompt, LEVEL)):
            rec("g1", "Group", "ops-admins", LEVEL, p)
            rec("s1", "AccessScope", "deploy console", LEVEL, p, LEVEL)
            links.append({"type": "CAN_ACCESS", "from": "g1", "to": "s1", "quote": LEVEL, "source": p,
                          "fields": [{"field": "level", "value": "Write"}]})
        if (p := self.label_of(prompt, LEVEL2)):   # the reading leaves the level out
            rec("g2", "Group", "release-managers", LEVEL2, p)
            rec("s2", "AccessScope", "release branch", LEVEL2, p, LEVEL2)
            links.append({"type": "CAN_ACCESS", "from": "g2", "to": "s2", "quote": LEVEL2, "source": p, "fields": []})
        return {"records": recs, "links": links}

    def ekg_grant_level(self, prompt, schema):
        if "release-managers" in prompt:
            return {"verdict": "accept", "choice": "write", "quote": "can push to the release branch", "cue": "", "reason": "t"}
        return {"verdict": "reject", "choice": "none", "quote": "", "cue": "", "reason": "t"}

    def ekg_depends_on(self, prompt, schema):
        out = []
        for block in prompt.split("\n[I")[1:]:
            label = "I" + block.split("]", 1)[0]
            if "." in label:
                continue
            head = block.split("\n", 1)[0]
            cands = re.findall(r"\[(I\d+\.C\d+)\] [^:]+: (.*)", block)
            for c, text in cands:
                if "REL-01" in head and "RISK-04" in text:
                    out.append({"item": label, "candidate": c, "kind": "gate", "quote": "until fraud risk clearance passes"})
                if "REL-01" in head and "LOG-02" in text:   # a guess the item's words do not name: quote not in it
                    out.append({"item": label, "candidate": c, "kind": "gate", "quote": "release notes in English"})
        return {"links": out}


def gate_checks(check):
    g = base.base_graph()
    base.add_doc(g, "orders.pdf", [("Rules", 1, [("text", GATE), ("text", LOOSE), ("text", CASE_PAY)])])
    base.add_doc(g, "risk.pdf", [("Risk", 1, [("text", RISK), ("text", CASE_OPS), ("text", LEVEL), ("text", LEVEL2)])])
    base.add_doc(g, "github.pdf", [("Contributions by code area", 1, [THIN])])
    for n in ("OPS-PACK", "OPS-CARRIER"):
        g.rec("Service", "catalog", n.lower(), {"name": n}, SCOPE)
    maya = g.rec("Person", "hr", "maya", {"full_name": "Maya Chen"}, SCOPE)
    priya = g.rec("Person", "hr", "priya", {"full_name": "Priya Nair"}, SCOPE)
    svc = {r["props"]["name"]: r for r in g.records.values() if r["type"] == "Service"}
    g.link("KNOWS_ABOUT", maya, svc["OPS-PACK"], "field")       # the graph already ties Maya to OPS-PACK only
    g.link("KNOWS_ABOUT", priya, svc["OPS-CARRIER"], "field")   # and Priya to OPS-CARRIER only
    GateReader.tables = {'"Most commits"': _contrib()}
    rep, out, tmp = base.run_fill(g, GateReader(), ["table_map", "doc_facts"], window=80_000)
    recs, links, by = base.written(out, tmp)
    name = base._name
    dep = [(by[lk["a"]]["props"].get("title"), by[lk["b"]]["props"].get("title"), lk["props"].get("kind"))
           for lk in links if lk["type"] == "DEPENDS_ON"]
    check("gates: the gate a requirement names in words links to the requirement elsewhere that states it "
          "(REL-01 DEPENDS_ON RISK-04, kind gate), with the item's own words as the reason",
          ("REL-01", "RISK-04", "gate") in dep)
    check("gates: a candidate the item's words do not name is refused (quote not in the item); no target is created",
          not any(b == "LOG-02" for _, b, _ in dep) and len(dep) == 1
          and rep["kinds"]["depends_on"]["to_ask"] >= 1)
    inc = next(r for r in recs if r["type"] == "Incident")
    topic = next(r for r in recs if r["type"] == "Topic" and name(r) == "O-8201")
    check("own case: an incident whose own id is exactly one Topic's name is ABOUT that Topic (from another document, "
          "with no ABOUT from the reading)",
          any(lk["type"] == "ABOUT" and lk["a"] == inc["id"] and lk["b"] == topic["id"] for lk in links))
    lv = {name(by[lk["a"]]): lk["props"].get("level") for lk in links if lk["type"] == "CAN_ACCESS"}
    check("access: a listed level in another letter case is that level (Write is write)", lv.get("ops-admins") == "write")
    check("access: a grant read without its level is asked its level from its own passage (can push -> write)",
          lv.get("release-managers") == "write" and rep["kinds"]["grant_level"]["to_ask"] == 1)
    con = _contributions(links, by)
    check("thin names: a bare first name the same table settled in another row is that person in its other rows "
          "(Priya 8 on OPS-PACK, Maya 9 on OPS-CARRIER), never a new record",
          con.get(("Priya Nair", "OPS-PACK"), {}).get("count") == 8 and con.get(("Maya Chen", "OPS-CARRIER"), {}).get("count") == 9
          and len(con) == 4 and not any(r["type"] == "Person" for r in recs))


def visible(gp, asker):
    """The record ids `asker` can see in the saved graph folder `gp`, through query-master's own view."""
    from app.ekg.query.graph import Graph as QGraph
    from app.ekg.query.view import View
    saved = sqlite_store.save(gp, base.QM, log=lambda *_: None)
    g = QGraph.from_sqlite(saved["sqlite"])
    v = View(g, asker)
    return {i for i in g.rec if v.can_see(i)}, g


def checks(check):
    Reader.tables = {'"Most commits"': empty_kv(
        row_type="Service", name_column="c1",
        cell_parts=[{"column": "c2", "parts": 2, "separator": "", "pattern": r"(.+) (\d+)"},
                    {"column": "c3", "parts": 2, "separator": "", "pattern": r"(.+) (\d+)"}],
        links=[{"column": "c2.1", "link": "CONTRIBUTED_TO", "other_type": "Person", "row_is": "to", "split": "none",
                "fields": [{"column": "c2.2", "field": "count"}],
                "fixed": [{"field": "rank", "value": "1"}, {"field": "kind", "value": "commits"}]},
               {"column": "c3.1", "link": "CONTRIBUTED_TO", "other_type": "Person", "row_is": "to", "split": "none",
                "fields": [{"column": "c3.2", "field": "count"}],
                "fixed": [{"field": "rank", "value": "2"}, {"field": "kind", "value": "commits"}]}])}
    g = graph()
    model = Reader()
    rep, out, tmp = base.run_fill(g, model, ["table_map", "doc_facts"], window=80_000)
    recs, links, by_id = base.written(out, tmp)
    name = base._name
    c = rep["written"]["documents_and_tables"]

    # 1. DEPENDS_ON
    dep = {(name(by_id[lk["a"]]).split(" ")[0], name(by_id[lk["b"]]).split(" ")[0], lk["props"].get("kind"))
           for lk in links if lk["type"] == "DEPENDS_ON"}
    check("depends_on: ORD-07 DEPENDS_ON ORD-03 (kind gate), a requirement waiting on another",
          ("ORD-07", "ORD-03", "gate") in dep)
    check("depends_on: ORD-07 DEPENDS_ON the payments service (kind gate)", ("ORD-07", "payments", "gate") in dep)

    # 2. stated group access
    scopes = [r for r in recs if r["type"] == "AccessScope"]
    grants = [lk for lk in links if lk["type"] == "CAN_ACCESS"]
    tool_place = next(r for r in by_id.values() if r["type"] == "AccessScope" and r["props"].get("name") == "github-pay-repo"
                      and r["props"]["source_tool"] == "ekg")
    stated = [lk for lk in grants if by_id[lk["b"]]["props"].get("name") == "repo-pay-write"]
    check("access: the scope the document describes is a record made from it (name and text), granted to the directory's "
          "eng-payments at the level the text states",
          len(stated) == 1 and stated[0]["props"].get("level") == "write"
          and name(by_id[stated[0]["a"]]) == "eng-payments" and by_id[stated[0]["a"]]["props"]["source_tool"] == "directory"
          and by_id[stated[0]["b"]]["props"]["source_tool"] == "derived")
    check("access: a tool's own place named in a text is never matched, patched or granted (a new scope of that name "
          "instead, and the tool's place is left as it was)",
          not any(lk["b"] == tool_place["id"] for lk in grants) and tool_place["id"] not in {r["id"] for r in recs}
          and c.get("AccessScope: a tool's own place of that name left alone (a document describes its own scope)") == 1)
    w = fill.Writer(base.rows(tmp / "records.jsonl"), base.rows(tmp / "links.jsonl"), "2026-11-01T00:00:00Z", "test")
    grp = next(r for r in w.by_id.values() if r["type"] == "Group")
    place = next(r for r in w.by_id.values() if r["id"] == tool_place["id"])
    check("access: the writer refuses any link of this stage to or from a tool's own place",
          (w._link("CAN_ACCESS", grp, place, [], "doc_facts", level="read") or "").startswith("access:"))
    gp_before, gp_after = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    base.TEMP += [gp_before, gp_after]
    for gp in (gp_before, gp_after):
        shutil.copytree(tmp, gp, dirs_exist_ok=True)
        shutil.rmtree(gp / "out", ignore_errors=True)
    shutil.copytree(out, gp_after / "llm")
    before, gb = visible(gp_before, "Suzan")
    after, ga = visible(gp_after, "Suzan")
    repo = next(i for i in gb.rec if gb.rec[i]["type"] == "Repository")
    check("access: what Suzan (a member of eng-payments) can see of the graph is the same with and without the "
          "document's grants, and the private repository stays hidden",
          before == (after & set(gb.rec)) and repo not in after)

    # 3. CONTRIBUTED_TO
    con = {(name(by_id[lk["a"]]), name(by_id[lk["b"]])): lk["props"] for lk in links if lk["type"] == "CONTRIBUTED_TO"}
    check("contributions: Lionel CONTRIBUTED_TO PAY-AUTH, count 42, rank 1, kind commits (rank and kind from the header)",
          {k: con.get(("Lionel", "PAY-AUTH"), {}).get(k) for k in ("count", "rank", "kind")} == {"count": 42, "rank": 1, "kind": "commits"})
    check("contributions: Suzan is second on PAY-AUTH (24) and first on PAY-CAP (31); four links in all",
          con.get(("Suzan", "PAY-AUTH"), {}).get("rank") == 2 and con[("Suzan", "PAY-AUTH")].get("count") == 24
          and con.get(("Suzan", "PAY-CAP"), {}).get("count") == 31 and con[("Suzan", "PAY-CAP")].get("rank") == 1
          and len(con) == 4)
    known = {"kind": ("commits", "reviews")}
    spec = lambda ty, enum=(): type("S", (), {"enum": enum, "type": ty})()
    check("fixed: a number field takes the header's number; a named value only one the link lists; any other refused",
          extract_tables.fixed_value(spec("number"), (), "2") == (2, None)
          and extract_tables.fixed_value(spec("any"), known["kind"], "Commits") == ("commits", None)
          and extract_tables.fixed_value(spec("any"), known["kind"], "lines") is None
          and extract_tables.fixed_value(spec("number"), (), "second") is None)

    # 4. Incident
    inc = [r for r in recs if r["type"] == "Incident"]
    about = [lk for lk in links if lk["type"] == "ABOUT" and inc and lk["a"] == inc[0]["id"]]
    topics = [r for r in recs if r["type"] == "Topic" and name(r) == "O-8201"]
    check("incident: one Incident with its title, summary and the timeline as written (four steps, times kept)",
          len(inc) == 1 and inc[0]["props"].get("title") == INC_HEAD and inc[0]["props"].get("summary")
          and inc[0]["props"].get("timeline") == TIMELINE.split("\n"))
    check("incident: it is ABOUT the case's Topic O-8201, which stays one Topic; it is EXTRACTED_FROM its passage",
          len(topics) == 1 and len(about) == 1 and about[0]["b"] == topics[0]["id"]
          and any(lk["type"] == "EXTRACTED_FROM" and lk["a"] == inc[0]["id"] for lk in links))
    check("valid: every record and link written validates", not base.all_valid(recs, links, by_id))


def cache_checks(check):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "table_mappings.json"
        t = lambda sig: type("T", (), {"write": {"signature": sig}, "question": sig})()
        old = extract_tables.MappingCache(path, "sha-old")
        old.put(t("gap"), {"shape": "rows", "schema_gap": "a count per person and module has no link"})
        old.put(t("full"), {"shape": "rows", "schema_gap": ""})
        old.put(t("rej"), {"shape": "rows", "_verdict": "rejected"})
        same, new = extract_tables.MappingCache(path, "sha-old"), extract_tables.MappingCache(path, "sha-new")
        check("cache: under the same registry every kept mapping answers",
              all(same.get(t(s)) is not None for s in ("gap", "full", "rej")))
        check("cache: under a new registry a mapping with a schema gap or a rejection is asked again; a full one is kept",
              new.get(t("gap")) is None and new.get(t("rej")) is None and new.get(t("full")) is not None and new.stale == 2)
        legacy = json.loads(path.read_text(encoding="utf-8"))
        for row in legacy.values():
            row.pop("registry")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        check("cache: a mapping kept before the cache knew registries counts as another registry's",
              extract_tables.MappingCache(path, "sha-old").get(t("gap")) is None)


def _contrib(rank1="1", rank2="2", second=True, gap=""):
    cols = [{"column": "c2.1", "link": "CONTRIBUTED_TO", "other_type": "Person", "row_is": "to", "split": "none",
             "fields": [{"column": "c2.2", "field": "count"}],
             "fixed": [{"field": "rank", "value": rank1}, {"field": "kind", "value": "commits"}]}]
    if second:
        cols.append({"column": "c3.1", "link": "CONTRIBUTED_TO", "other_type": "Person", "row_is": "to", "split": "none",
                     "fields": [{"column": "c3.2", "field": "count"}],
                     "fixed": [{"field": "rank", "value": rank2}, {"field": "kind", "value": "commits"}]})
    return empty_kv(row_type="Service", name_column="c1", schema_gap=gap,
                    cell_parts=[{"column": "c2", "parts": 2, "separator": "", "pattern": r"(.+) (\d+)"},
                                {"column": "c3", "parts": 2, "separator": "", "pattern": r"(.+) (\d+)"}], links=cols)


def _contributions(links, by):
    return {(base._name(by[lk["a"]]), base._name(by[lk["b"]])): lk["props"] for lk in links if lk["type"] == "CONTRIBUTED_TO"}


def remap_checks(check):
    """A table asked again for a new schema: the new mapping is used only when it reads all the earlier one read; a
    fixed value that is neither listed nor a number is asked again, and left out if it comes back the same."""
    g = graph()

    def run(answer, out=None, stale=False):
        Reader.tables = {'"Most commits"': answer}
        if out is not None:
            for f in ("ledger.jsonl", "responses.jsonl"):
                (out / f).unlink(missing_ok=True)
            if stale:
                path = out / "table_mappings.json"
                rows = json.loads(path.read_text(encoding="utf-8"))
                for r in rows.values():
                    r["registry"] = "an older registry"
                path.write_text(json.dumps(rows), encoding="utf-8")
        return base.run_fill(g, Reader(), ["table_map"], window=80_000, **({"out": out} if out else {}))

    rep, out, tmp = run(_contrib(gap="a column no link holds"))
    first = _contributions(*base.written(out, tmp)[1:])
    rep2, out, _ = run(_contrib(second=False), out, stale=True)
    kept = rep2["kinds"]["table_map"]
    check("remap: asked again for a new schema, an answer that reads less (one contributor column, not two) is not "
          "used: the earlier mapping stays and is counted",
          kept.get("mappings_asked_again_for_a_new_schema") == 1 and kept.get("earlier_mappings_kept_new_read_less") == 1
          and _contributions(*base.written(out, tmp)[1:]) == first and len(first) == 4)
    rep3, out, _ = run(_contrib(gap="still a gap"), out, stale=True)
    check("remap: an answer that reads all the earlier one read is used",
          rep3["kinds"]["table_map"].get("mappings_asked_again_for_a_new_schema") == 1
          and rep3["kinds"]["table_map"].get("earlier_mappings_kept_new_read_less") == 0)
    rep4, out4, tmp4 = run(_contrib(rank1="}, ", rank2="}, "))
    c4 = _contributions(*base.written(out4, tmp4)[1:])
    led = [e for e in base.rows(out4 / "ledger.jsonl") if e["kind"] == "table_map"]
    check("fixed: an unreadable fixed value is asked again; given again, it is left out and the rest of the mapping "
          "stands (counts kept, no rank)",
          len(c4) == 4 and c4.get(("Lionel", "PAY-AUTH"), {}).get("count") == 42
          and "rank" not in c4.get(("Lionel", "PAY-AUTH"), {})
          and any("fixed value" in json.dumps(e) for e in led))
    check("remap: earlier mappings are checked to the same reads() a fresh answer is",
          extract_tables.reads({"shape": "rows", "name": 0, "fields": [[1, "count"]], "links": []})
          < extract_tables.reads({"shape": "rows", "name": 0, "fields": [[1, "count"]],
                                  "links": [{"column": 2, "link": "ABOUT", "fields": [], "fixed": {}}]}))


def catalog_checks(check):
    cat = extract_schema.Catalog(common._reg, ["Group", "AccessScope", "Repository", "Person", "Service", "Topic", "Account"])
    check("catalog: AccessScope may be created from a document (a scope a directory describes) and is the far end of "
          "CAN_ACCESS only; no other link offers it (GOVERNED_BY, INHERITS_FROM, UNTIL ...)",
          "AccessScope" in cat.creatable and cat.links.get("CAN_ACCESS", ([], []))[1] == ["AccessScope"]
          and not any("AccessScope" in a + b for n, (a, b) in cat.links.items() if n != "CAN_ACCESS")
          and not any("AccessScope" in a for a, _ in cat.links.values())
          and not any("AccessScope" in a + b for n, (a, b) in cat.table_links.items() if n != "CAN_ACCESS"))
    check("catalog: Incident is a stated type (title, summary); DEPENDS_ON joins requirements; CONTRIBUTED_TO is offered",
          "Incident" in cat.stated and cat.name_field("Incident") == "title" and cat.text_field("Incident") == "summary"
          and "Requirement" in cat.links["DEPENDS_ON"][0] and "Requirement" in cat.links["DEPENDS_ON"][1]
          and "CONTRIBUTED_TO" in cat.table_links)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    results = []

    def check(n, cond):
        results.append((n, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {n}", flush=True)

    common.bind(base.QM)
    try:
        catalog_checks(check)
        cache_checks(check)
        checks(check)
        remap_checks(check)
        gate_checks(check)
    finally:
        for d in base.TEMP:
            shutil.rmtree(d, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
