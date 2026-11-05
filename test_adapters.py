"""Behaviour tests for the adapters, on the synthetic 'acme' fixtures (Linear + Slack + a GitHub/git slice).

Beyond "every item validates", it checks the links the adapters exist to make, and
evaluates access the way the spec's access layer does: a person's verified accounts ->
the groups they are in -> the scopes granted to either, at a point in time; a record is
visible when every scope clause has a tag the viewer holds.

    python -I ekg-eval-data/test_adapters.py
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters.pipeline import run  # noqa: E402

FIXTURES = HERE / "fixtures" / "acme"


class G:
    def __init__(self, out: Path):
        self.recs = [json.loads(l) for l in open(out / "records.jsonl", encoding="utf-8")]
        self.links = [json.loads(l) for l in open(out / "links.jsonl", encoding="utf-8")]
        self.by_id = {r["id"]: r for r in self.recs}

    def one(self, rtype, **props):
        hits = [r for r in self.recs if r["type"] == rtype and all(r["props"].get(k) == v for k, v in props.items())]
        assert len(hits) == 1, f"expected one {rtype} {props}, found {len(hits)}"
        return hits[0]

    def native(self, rtype, tool, native):
        return self.one(rtype, source_tool=tool, native_id=native)

    def linked(self, ltype, a, b, **props):
        return [lk for lk in self.links if lk["type"] == ltype and lk["a"] == a["id"] and lk["b"] == b["id"]
                and all(lk["props"].get(k) == v for k, v in props.items())]

    # ---- the access layer, as the spec describes it
    def scopes_of(self, person, at):
        live = lambda lk: (lk["props"].get("valid_from") or "") <= at and (lk["props"].get("valid_to") or "9999") > at
        accounts = {lk["b"] for lk in self.links if lk["type"] == "HAS_ACCOUNT" and lk["a"] == person["id"]
                    and lk["props"].get("verified") and live(lk)}
        groups = {lk["b"] for lk in self.links if lk["type"] == "MEMBER_OF" and lk["a"] in accounts and live(lk)}
        public = {r["id"] for r in self.recs if r["type"] == "Public"}  # every asker holds the Public principal
        holders = accounts | groups | public
        return {lk["b"] for lk in self.links if lk["type"] == "CAN_ACCESS" and lk["a"] in holders and live(lk)}

    def can_see(self, person, rec, at="2026-10-04T12:00:00Z"):
        held = self.scopes_of(person, at)
        return all(any(tag in held for tag in clause) for clause in rec["scope"])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory() as tmp:
        report = run(FIXTURES, HERE.parent / "query-master", Path(tmp), log=lambda *_: None)
        g = G(Path(tmp))
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    t = report["totals"]
    check("every record and link validates", t["records"] == t["records_valid"] and t["links"] == t["links_valid"])

    eng1, eng2, eng3 = (g.one("Ticket", identifier=k) for k in ("ENG-1", "ENG-2", "ENG-3"))
    sec1 = g.one("Ticket", identifier="SEC-1")
    pr42 = g.one("PullRequest", number=42)
    msgs = {r["props"]["native_id"]: r for r in g.recs if r["type"] == "Message"}
    root = next(m for k, m in msgs.items() if k.startswith("C01ENG:") and "double refunds" in m["props"]["text"])
    reply = next(m for m in msgs.values() if m["props"]["text"].startswith("On it"))
    bot = next(m for m in msgs.values() if m["props"]["kind"] == "bot_message")
    pr_msg = next(m for m in msgs.values() if m["props"]["text"].startswith("PR is up"))
    sec_msg = next(m for m in msgs.values() if "Webhook secret leaked" in m["props"]["text"])
    eve_msg = next(m for m in msgs.values() if "comms drafted" in m["props"]["text"])
    dm = next(m for m in msgs.values() if m["props"]["native_id"].startswith("D01AB:"))
    thread = g.native("Thread", "slack", root["props"]["native_id"])

    # Linear <-> Slack
    check("ENG-1 was created from the Slack message (Linear attachment)", g.linked("CREATED_FROM", eng1, root))
    check("ENG-1 is discussed in that Slack thread", g.linked("DISCUSSED_IN", eng1, thread))
    check("the Linear bot's post ANNOUNCES ENG-1", g.linked("ANNOUNCES", bot, eng1, kind="notify"))
    check("Bob's reply MENTIONS ENG-1 by key", g.linked("MENTIONS", reply, eng1, kind="key"))
    check("Alice's message MENTIONS ENG-2", g.linked("MENTIONS", pr_msg, eng2, kind="key"))
    check("the root message mentions Bob (kind mention)",
          g.linked("MENTIONS", root, g.native("Account", "slack", "U02BOB"), kind="mention"))
    in_channel = [lk for lk in g.links if lk["type"] == "IN" and thread["id"] in (lk["a"], lk["b"])
                  and {g.by_id[lk["a"]]["type"], g.by_id[lk["b"]]["type"]} == {"Thread", "Channel"}]
    check("the Slack thread is IN its channel", in_channel)
    # Linear <-> GitHub
    check("PR 42 IMPLEMENTS ENG-1 through the branch name", g.linked("IMPLEMENTS", pr42, eng1, link_method="branch_name"))
    check("ENG-1 LINKS_TO PR 42 (Linear's GitHub attachment)", g.linked("LINKS_TO", eng1, pr42, method="integration"))
    check("Alice's Slack message LINKS_TO PR 42", g.linked("LINKS_TO", pr_msg, pr42))
    check("PR 42 was merged as the git commit", g.linked("MERGED_AS", pr42, g.native("Commit", "git", "a" * 40)))
    # Linear internals
    check("ENG-2 BLOCKS ENG-1", g.linked("BLOCKS", eng2, eng1))
    check("ENG-3 is a DUPLICATE_OF ENG-1", g.linked("DUPLICATE_OF", eng3, eng1))
    states = sorted((lk["props"]["valid_from"], lk["props"].get("valid_to"), g.by_id[lk["b"]]["props"]["name"], lk["props"].get("cause"))
                    for lk in g.links if lk["type"] == "IN_STATE" and lk["a"] == eng1["id"])
    check("ENG-1 state history is Triage -> In Progress -> Done (closed by the merge)",
          [s[2] for s in states] == ["Triage", "In Progress", "Done"] and states[0][1] == states[1][0]
          and states[1][1] == states[2][0] and states[2][3] == "git_automation")
    check("Bob's Linear comment is a REPLY_TO Alice's",
          g.linked("REPLY_TO", g.native("Comment", "linear", "cm-2"), g.native("Comment", "linear", "cm-1")))
    check("the prompt-injection message is flagged",
          any(m["props"].get("contains_untrusted_instructions") for m in msgs.values()))

    # identity: one person per verified email, across Slack, Linear and GitHub
    bob = g.one("Person", work_email="bob@acme.io")
    has = {g.by_id[lk["b"]]["props"]["source_tool"]: lk["props"]["verified"] for lk in g.links
           if lk["type"] == "HAS_ACCOUNT" and lk["a"] == bob["id"]}
    check("Bob is one Person with Slack, Linear and GitHub accounts, all verified (GitHub through SAML SSO)",
          has == {"slack": True, "linear": True, "github": True})
    alice_p = g.one("Person", work_email="alice@acme.io")
    check("Alice's GitHub account is not linked: no SSO identity, and no commit of hers to infer from",
          not any(lk["type"] == "HAS_ACCOUNT" and lk["a"] == alice_p["id"] and g.by_id[lk["b"]]["props"]["source_tool"] == "github"
                  for lk in g.links))

    # access
    alice, carol, dave, eve = (g.one("Person", work_email=e) for e in
                               ("alice@acme.io", "carol@contractor.dev", "dave@acme.io", "eve@acme.io"))
    check("Bob sees #eng but not the private #sec-incident", g.can_see(bob, root) and not g.can_see(bob, sec_msg))
    check("Dave sees #sec-incident and SEC-1 (private Linear team)", g.can_see(dave, sec_msg) and g.can_see(dave, sec1))
    check("Bob sees ENG-1 and PR 42 (positive control: access is not just empty)", g.can_see(bob, eng1) and g.can_see(bob, pr42))
    check("the private repo's PR is hidden from a non-collaborator (Dave)", not g.can_see(dave, pr42))
    check("...and from Alice, a collaborator whose GitHub account is unverified: fails closed", not g.can_see(alice, pr42))
    check("Bob does not see SEC-1", not g.can_see(bob, sec1))
    check("Alice sees SEC-1 (member of the private Security team)", g.can_see(alice, sec1))
    check("Carol (guest in Slack) sees #eng, which she is in, but not #sec-incident",
          g.can_see(carol, root) and not g.can_see(carol, sec_msg))
    check("Eve saw #sec-incident while she was in it (08:20-09:00), not after",
          g.can_see(eve, eve_msg, at="2026-10-02T08:30:00Z") and not g.can_see(eve, eve_msg))
    check("Eve has no Linear account, so she sees no ENG ticket", not g.can_see(eve, eng1))
    check("the DM is visible to Alice and Bob only", g.can_see(alice, dm) and g.can_see(bob, dm) and not g.can_see(carol, dm))

    width = max(len(n) for n, _ in checks)
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed "
          f"({t['records']} records, {t['links']} links from {', '.join(report['sources'])})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
