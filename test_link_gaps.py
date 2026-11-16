"""Tests for four links the loaders left out, each on a small synthetic dataset or graph (no model, no network).

  1. mail -> Group: a person's message whose subject or own text names a Group by its whole name MENTIONS it;
     a name only in the quoted reply, a one-word group name and a name two groups share are not linked
  2. Knowledge -> code: Type#member, Type.member, Type::member and file paths in a knowledge item's own text
     EXPLAIN the CodeSymbol and its File; an unknown or ambiguous name is counted, never guessed; code_refs.derive
     writes them for a built graph folder and sqlite_store.save stores them as their own stage
  3. (query side: the thread a decision was made in, see query-master tests/ekg/test_query_hops.py)
  4. address -> Domain: each address on an owned domain or a sub-domain of it, and each mailing list's own address
     there, is MEMBER_OF that exact domain's Domain; addresses elsewhere are not

    python -I ekg-eval-data/test_link_gaps.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters import code_refs, common, sqlite_store  # noqa: E402
from ekg_adapters.common import Graph  # noqa: E402
from ekg_adapters.pipeline import run  # noqa: E402
from test_adapters import G  # noqa: E402

QM = HERE.parent / "query-master"

MANIFEST = {"tenant": "northwind", "project": None, "fetched_at": "2026-05-01T00:00:00Z",
            "window": {"since": "2026-03-01", "until": "2026-04-30"},
            "settings": {"directory_tool": "hris", "directory_name": "northwind-hr", "owned_email_domains": ["northwind.test"],
                         "decision_prefix": None, "notifiers": {}}}
DIRECTORY = {"people": [
    {"id": "p1", "name": "Rosa Vint", "email": "rosa@northwind.test", "teams": ["Release Council", "Ops"]},
    {"id": "p2", "name": "Tomas Ek", "email": "tomas@eu.northwind.test", "teams": ["Ops"]},
    {"id": "p3", "name": "Ines Moll", "email": "ines@northwind.test",
     "teams": [{"id": "g-a", "name": "Data Guild"}, {"id": "g-b", "name": "data guild"}]},
]}
MBOX = """From a@x Mon Mar  2 09:00:00 2026
From: Rosa Vint <rosa@northwind.test>
To: forum@lists.northwind.test
Subject: [ANNOUNCE] New Release Council member: Tomas Ek
Date: Mon, 02 Mar 2026 09:00:00 +0000
Message-ID: <a1@northwind.test>
List-Id: <forum.lists.northwind.test>

Please welcome Tomas.

From a@x Mon Mar  2 10:00:00 2026
From: Guest Person <guest@elsewhere.test>
To: forum@lists.northwind.test
Subject: Re: something else
Date: Mon, 02 Mar 2026 10:00:00 +0000
Message-ID: <a2@elsewhere.test>
In-Reply-To: <x0@northwind.test>
List-Id: <forum.lists.northwind.test>

Thanks, the release council already agreed, and Ops will follow up with the Data Guild.

> Earlier: the Release Council met on Friday.

From a@x Mon Mar  2 11:00:00 2026
From: Tomas Ek <tomas@eu.northwind.test>
To: forum@lists.northwind.test
Subject: Re: plans
Date: Mon, 02 Mar 2026 11:00:00 +0000
Message-ID: <a3@northwind.test>
List-Id: <forum.lists.northwind.test>

> the Release Council met on Friday.
Noted.

"""


def build_dataset(tmp: Path) -> Path:
    d = tmp / "northwind"
    (d / "people").mkdir(parents=True)
    (d / "mail" / "forum").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (d / "people" / "directory.json").write_text(json.dumps(DIRECTORY), encoding="utf-8")
    (d / "mail" / "forum" / "2026-03.mbox").write_bytes(MBOX.encode("utf-8"))
    return d


def code_graph():
    """A tiny code graph: two files, a type with a member and an overload pair, a second type sharing a simple name
    in another package, and knowledge items naming them."""
    g = Graph(observed_at="2026-05-01T00:00:00Z")
    pub = [["public"]]
    f1 = g.rec("File", "git", "r:src/main/org/ex/Pump.java", {"path": "src/main/org/ex/Pump.java", "name": "Pump.java"}, pub)
    f2 = g.rec("File", "git", "r:src/util/org/ex/util/Valve.java", {"path": "src/util/org/ex/util/Valve.java", "name": "Valve.java"}, pub)
    f3 = g.rec("File", "git", "r:src/other/org/ex/other/Valve.java", {"path": "src/other/org/ex/other/Valve.java", "name": "Valve.java"}, pub)
    f4 = g.rec("File", "git", "r:conf/pump.yaml", {"path": "conf/pump.yaml", "name": "pump.yaml"}, pub)
    syms = {}
    for native, qn, kind, f in [("Pump", "org.ex.Pump", "class", f1), ("Pump.drain", "org.ex.Pump.drain", "method", f1),
                                ("Pump.stop()", "org.ex.Pump.stop", "method", f1), ("Pump.stop(t)", "org.ex.Pump.stop", "method", f1),
                                ("util.Valve", "org.ex.util.Valve", "class", f2), ("util.Valve.open", "org.ex.util.Valve.open", "method", f2),
                                ("other.Valve", "org.ex.other.Valve", "class", f3), ("other.Valve.open", "org.ex.other.Valve.open", "method", f3)]:
        s = g.rec("CodeSymbol", "git", native, {"qualified_name": qn, "kind": kind}, pub)
        g.link("DEFINED_IN", s, f, "field")
        syms[native] = s
    src = g.rec("EmailMessage", "mail", "<k@x>", {"message_id": "<k@x>", "subject": "s", "sent_at": "2026-03-01T00:00:00Z"}, pub)

    def know(native, statement, kind="fact"):
        return g.rec("Knowledge", "derived", native, {"statement": statement, "kind": kind, "status": "active",
                     "_stated": {"statement": {"value": statement, "by": {src["id"]: {"value": statement}}}}}, pub)

    ks = {"member": know("k1", "Pump#drain() must run before Pump.stop() or the queue is lost.", "rationale"),
          "fallback": know("k2", "Pump.MAX_RATE caps the rate."),
          "ambiguous": know("k3", "Valve.open() retries twice."),
          "qualified": know("k4", "org.ex.util.Valve::open is safe to call twice."),
          "path": know("k5", "The rate lives in conf/pump.yaml, not in Valve.java."),
          "none": know("k6", "Restart the service when in doubt; see Missing.thing and docs/none.md.")}
    return g, syms, (f1, f2, f3, f4), ks, src


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # ---------------------------------------------------------------- 1 and 4: through the whole pipeline
    with tempfile.TemporaryDirectory() as tmp:
        data = build_dataset(Path(tmp))
        out = Path(tmp) / "out"
        report = run(data, QM, out, log=lambda *_: None)
        g = G(out)
    t = report["totals"]
    check("every record and link validates", t["records"] == t["records_valid"] and t["links"] == t["links_valid"])
    council = g.one("Group", name="Release Council")
    ops = g.one("Group", name="Ops")
    announce = g.one("EmailMessage", message_id="<a1@northwind.test>")
    reply = g.one("EmailMessage", message_id="<a2@elsewhere.test>")
    quoted_only = g.one("EmailMessage", message_id="<a3@northwind.test>")
    hit = g.linked("MENTIONS", announce, council)
    check("1: an announcement naming a group in its subject MENTIONS that group",
          hit and hit[0]["props"]["kind"] == "name" and hit[0]["props"]["method"] == "text_rule")
    check("1: a name written in another letter case in the body is the same group", g.linked("MENTIONS", reply, council))
    check("1: a one-word group name is not read as a mention", not any(lk["b"] == ops["id"] for lk in g.links
                                                                     if lk["type"] == "MENTIONS"))
    check("1: a name only in the quoted reply is not a mention", not g.linked("MENTIONS", quoted_only, council))
    guilds = [r for r in g.recs if r["type"] == "Group" and r["props"]["name"].lower() == "data guild"]
    check("1: a name two groups share links neither", len(guilds) == 2 and not any(
        g.linked("MENTIONS", reply, x) for x in guilds) and report["notes"].get("mail: group names several groups share (not linked)"))
    check("1: no mail LINKS_TO a group (a name is not a hyperlink)",
          not any(lk["type"] == "LINKS_TO" and g.by_id[lk["b"]]["type"] == "Group" for lk in g.links))

    root = g.one("Domain", domain="northwind.test")
    sub = g.one("Domain", domain="eu.northwind.test")
    lists_dom = g.one("Domain", domain="lists.northwind.test")
    rosa = g.one("EmailAddress", address="rosa@northwind.test")
    tomas = g.one("EmailAddress", address="tomas@eu.northwind.test")
    guest = g.one("EmailAddress", address="guest@elsewhere.test")
    check("4: an address on an owned domain is MEMBER_OF its Domain", g.linked("MEMBER_OF", rosa, root, role="member"))
    check("4: an address on a sub-domain is MEMBER_OF that sub-domain's own Domain",
          g.linked("MEMBER_OF", tomas, sub) and not g.linked("MEMBER_OF", tomas, root))
    lst = g.one("MailingList", address="forum@lists.northwind.test")
    list_addr = g.one("EmailAddress", address=lst["props"]["address"])
    check("4: a list's own address is an EmailAddress MEMBER_OF its Domain", g.linked("MEMBER_OF", list_addr, lists_dom))
    check("4: an address elsewhere has no Domain, and no Domain is made for it",
          not any(lk["a"] == guest["id"] and lk["type"] == "MEMBER_OF" for lk in g.links)
          and not any(r["type"] == "Domain" and "elsewhere" in r["props"]["domain"] for r in g.recs))

    # ---------------------------------------------------------------- 2: code named in knowledge
    common.bind(QM)
    common.set_tenant("northwind")
    cg, syms, (f1, f2, f3, f4), ks, src = code_graph()
    index = code_refs.CodeIndex()
    for r in list(cg.records.values()):
        index.add(r)
    for lk, _, _ in cg.links.values():
        if lk["type"] == "DEFINED_IN":
            index.defined_in(lk["a"], lk["b"])
    before = set(cg.links)
    made = code_refs.explains(cg, ks.values(), index)
    new = [cg.links[c][0] for c in cg.links if c not in before]
    ex = lambda k, t: [lk for lk in new if lk["a"] == k["id"] and lk["b"] == t["id"]]
    check("2: every link made is an EXPLAINS that validates", made == len(new) and all(
        lk["type"] == "EXPLAINS" and common._vlink(lk, "Knowledge", cg.records[cg.links[lk["props"]["claim_id"]][2]]["type"]).ok
        for lk in new))
    check("2: Type#member EXPLAINS that member and its file",
          ex(ks["member"], syms["Pump.drain"]) and ex(ks["member"], f1))
    check("2: an overloaded member names its file, not one overload",
          not ex(ks["member"], syms["Pump.stop()"]) and not ex(ks["member"], syms["Pump.stop(t)"]))
    check("2: a rationale item's link is kind rationale and cites the item's source, never the item",
          all(lk["props"].get("kind") == "rationale" and lk["props"]["evidence"] == [src["id"]] for lk in ex(ks["member"], f1)))
    check("2: a member the parse has no symbol for falls back to its type",
          ex(ks["fallback"], syms["Pump"]) and ex(ks["fallback"], f1))
    check("2: a simple name two types share is not guessed", not [lk for lk in new if lk["a"] == ks["ambiguous"]["id"]])
    check("2: a qualified name (any separator) settles it",
          ex(ks["qualified"], syms["util.Valve.open"]) and ex(ks["qualified"], f2) and not ex(ks["qualified"], f3))
    check("2: a path EXPLAINS its file; a file name two files share does not",
          ex(ks["path"], f4) and not ex(ks["path"], f2) and not ex(ks["path"], f3))
    check("2: names of nothing in the code make no link", not [lk for lk in new if lk["a"] == ks["none"]["id"]])

    with tempfile.TemporaryDirectory() as tmp:   # derive over a graph folder, then the stored stage
        gp = Path(tmp) / "graph"
        knowledge = [r for r in cg.records.values() if r["type"] == "Knowledge"]
        adapters = Graph(observed_at=cg.observed_at)
        adapters.records = {k: r for k, r in cg.records.items() if r["type"] != "Knowledge"}
        adapters.links = {c: v for c, v in cg.links.items() if c in before}
        adapters.write(gp)
        (gp / "llm").mkdir()
        with open(gp / "llm" / "records.jsonl", "w", encoding="utf-8") as f:
            for r in knowledge:
                f.write(json.dumps(r) + "\n")
        rep = code_refs.derive(gp, QM, log=lambda *_: None)
        saved = sqlite_store.save(gp, QM, log=lambda *_: None)
        con = sqlite3.connect(saved["sqlite"])
        stored = con.execute("select count(*) from ekg_links where type='EXPLAINS'").fetchone()[0]
        con.close()
    check("2: derive writes the same links for a graph folder", rep["links"] == made)
    check("2: the saved graph holds them as a stage of their own",
          stored == made and saved["links_by_stage"].get("derived") == made)

    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(ok for _, ok in checks)
    print(f"\n{passed} of {len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
