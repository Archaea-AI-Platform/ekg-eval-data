"""Tests that nothing in the loaders is tied to one project or organisation.

The dataset is fixtures/acme (Linear + Slack + a GitHub/git slice) with fixtures/acme_directory laid over it:
a generic people directory (people/directory.json), a mailing list, and settings that name no open-source
foundation (tenant "acme", the directory's tool is "workday", the sources are private). It checks

  - the generic directory: Person, the directory Account, EmailAddress, Group + dated MEMBER_OF, Position +
    HOLDS_POSITION, REPORTS_TO, and the accounts in other tools the directory names
  - the same shapes from a CSV file
  - that no record carries another organisation's tenant, tool or address
  - that a tool name the kit has never seen does not break validation
  - that the proposal / vote logic does nothing when its settings are off, and that notifiers are settings
  - the settings themselves: families, presets, release-branch names

    python -I ekg-eval-data/test_directory.py
"""

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters import common, people, settings  # noqa: E402
from ekg_adapters.pipeline import run  # noqa: E402
from test_adapters import G  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as tmp:
        data, out = Path(tmp) / "acme-co", Path(tmp) / "out"
        shutil.copytree(HERE / "fixtures" / "acme", data)
        shutil.copytree(HERE / "fixtures" / "acme_directory", data, dirs_exist_ok=True)
        report = run(data, HERE.parent / "query-master", out, log=lambda *_: None)
        g = G(out)

        # ---- the same rows from a CSV file
        csv_dir = Path(tmp) / "csv" / "people"
        csv_dir.mkdir(parents=True)
        (csv_dir / "directory.csv").write_text(
            "id,name,email,emails,title,teams,manager,start_date,account_github,accounts.slack\n"
            "7,Ada Byron,ada@corp.example,,Analyst,Maths; Engines,,2020-01-02,ada-gh,U0ADA\n"
            "8,Bo Li,,bo@corp.example|bo.li@corp.example,,Maths,7,,,\n", encoding="utf-8")
        from_csv = people.read_directory(csv_dir)

    t = report["totals"]
    check("every record and link validates", t["records"] == t["records_valid"] and t["links"] == t["links_valid"])
    check("the people adapter ran on the generic directory", report["adapters"]["people"]["status"] == "ran")

    # ---- the directory's records
    alice, bob, dave, frank = (g.native("Person", "workday", i) for i in ("e-100", "e-101", "e-102", "e-090"))
    check("a Person per row, with name, title, work email, start date and source",
          alice["props"]["full_name"] == "Alice Ng" and alice["props"]["title"] == "Engineering Manager"
          and alice["props"]["work_email"] == "alice@acme.io" and alice["props"]["start_date"] == "2024-03-01T00:00:00Z"
          and alice["props"]["person_source"] == "directory" and frank["props"]["status"] == "inactive")
    check("the row with no id is skipped and counted", report["notes"].get("people: directory row with no id skipped") == 1)
    acct = g.native("Account", "workday", "e-101")
    has = g.linked("HAS_ACCOUNT", bob, acct, verified=True, link_method="idp")
    check("each person HAS_ACCOUNT in the directory itself: verified, with the file's link method, from the start date",
          has and has[0]["props"]["valid_from"] == "2025-02-01T00:00:00Z")
    left = g.linked("HAS_ACCOUNT", frank, g.native("Account", "workday", "e-090"))
    check("a last day is stored as the exclusive end (the next day)", left and left[0]["props"]["valid_to"] == "2026-07-01T00:00:00Z")
    primary, second = g.native("EmailAddress", "email", "bob@acme.io"), g.native("EmailAddress", "email", "bob.ruiz@acme.io")
    check("every address is an EmailAddress: the first is the primary alias, the rest secondary, each ADDRESS_OF the account",
          g.linked("HAS_ALIAS", bob, primary, role="primary", verified=True) and g.linked("HAS_ALIAS", bob, second, role="secondary")
          and g.linked("ADDRESS_OF", second, acct) and g.native("EmailAddress", "email", "frank@acme.io"))
    payments, security = g.native("Group", "workday", "team:Payments"), g.native("Group", "workday", "grp-sec")
    member = g.linked("MEMBER_OF", acct, payments, role="member")
    ended = g.linked("MEMBER_OF", g.native("Account", "workday", "e-090"), payments)
    check("a Group per team, and dated MEMBER_OF (since / until, the end exclusive)",
          payments["props"]["kind"] == "hr_team" and security["props"] == {**security["props"], "name": "Security", "kind": "department"}
          and member and member[0]["props"]["valid_from"] == "2025-02-01T00:00:00Z"
          and ended and ended[0]["props"]["valid_to"] == "2026-07-01T00:00:00Z"
          and g.linked("MEMBER_OF", g.native("Account", "workday", "e-102"), security, role="lead"))
    seat, own_seat = g.native("Position", "workday", "pos-sec-1"), g.native("Position", "workday", "seat:e-101")
    holds = g.linked("HOLDS_POSITION", dave, seat, kind="primary", title="Security Engineer")
    check("a Position per seat: the one the row names, else the person's own seat named after the title",
          seat["props"]["name"] == "Security Engineer II" and holds and holds[0]["props"]["valid_from"] == "2025-06-01T00:00:00Z"
          and own_seat["props"]["name"] == "Backend Engineer" and g.linked("HOLDS_POSITION", bob, own_seat, title="Backend Engineer")
          and g.linked("PART_OF", seat, security))
    check("REPORTS_TO the manager (kind line); a manager id that is not in the directory is counted, not invented",
          g.linked("REPORTS_TO", bob, alice, kind="line") and g.linked("REPORTS_TO", dave, alice)
          and not any(lk["type"] == "REPORTS_TO" and lk["a"] == frank["id"] for lk in g.links)
          and report["notes"].get("people: manager id not in the directory (no REPORTS_TO)") == 1)

    # ---- identity: the directory's Person is the one every tool's account joins
    def tools_of(person):
        return {g.by_id[lk["b"]]["props"]["source_tool"]: (lk["props"]["verified"], lk["props"]["link_method"])
                for lk in g.links if lk["type"] == "HAS_ACCOUNT" and lk["a"] == person["id"]}

    check("Bob is one Person: the directory's, joined by his Slack, Linear and GitHub accounts",
          len([r for r in g.recs if r["type"] == "Person" and r["props"].get("work_email") == "bob@acme.io"]) == 1
          and set(tools_of(bob)) == {"workday", "slack", "linear", "github"} and all(v for v, _ in tools_of(bob).values()))
    check("the directory names Alice's GitHub account, so it is hers and verified (it was unlinked without a directory)",
          tools_of(alice).get("github") == (True, "admin"))
    check("...which lets her see the private repo's PR", g.can_see(alice, g.one("PullRequest", number=42)))
    check("people the directory does not list still get a Person from their verified email (Carol, Eve)",
          g.one("Person", work_email="carol@contractor.dev")["props"]["source_tool"] == "email")
    check("with a directory, no person is inferred from commit authorship",
          not any(r["props"].get("person_source") == "inferred_from_activity" for r in g.recs if r["type"] == "Person"))
    carol = g.one("Person", work_email="carol@contractor.dev")
    check("a private directory is visible to its members (Bob sees Alice's record) and not to others (Carol)",
          g.can_see(bob, alice) and not g.can_see(carol, alice))

    # ---- nothing of another organisation
    blob = json.dumps(g.recs) + json.dumps(g.links)
    check("every record is the manifest's tenant", {r["props"]["tenant_id"] for r in g.recs} == {"acme"} and report["tenant"] == "acme")
    check("no record or link names another organisation (tool, address or key)", not re.search(r"apache|\basf\b|\bkip\b", blob, re.I))
    check("a tool name the kit has never seen (workday) validates, and its records count under their type's pillar",
          report["records"]["Person (workday)"]["valid"] == 4
          and any("People" in k for k in report["cross_pillar"]) and common.pillar_of(alice) == "People"
          and common.pillar_of({"type": "NoSuchType", "props": {"source_tool": "x"}}) == "Other")

    # ---- mail: notifiers are settings; the proposal and vote logic is off unless the settings turn it on
    eng1 = g.one("Ticket", identifier="ENG-1")
    mails = {r["props"]["message_id"]: r for r in g.recs if r["type"] == "EmailMessage"}
    check("a sender the settings call a tracker notifier ANNOUNCES the ticket its subject names",
          g.linked("ANNOUNCES", mails["<t1@acme.io>"], eng1, kind="notify"))
    check("a sender the settings call CI is counted and not linked",
          report["notes"].get("mail: CI notifications (noise, not linked)") == 1
          and not any(lk["a"] == mails["<c1@acme.io>"]["id"] and lk["type"] in ("MENTIONS", "DISCUSSES") for lk in g.links))
    check("a person's message still MENTIONS the ticket in its body", g.linked("MENTIONS", mails["<v1@acme.io>"], g.one("Ticket", identifier="ENG-2")))
    check("with no decision_prefix and vote_threads off, a [VOTE] thread makes no decision, vote or tally",
          not any(lk["type"] in ("DECIDED_BY", "MADE_IN", "REVERSES") for lk in g.links)
          and not any(r["type"] == "Decision" for r in g.recs)
          and not any("vote" in k or "proposal" in k for k in report["notes"])
          and not any("Decision" in k for k in report["linker"]["unresolved"]))

    # ---- the CSV reader gives the same row shape
    rows = {r["id"]: r for r in from_csv["people"]}
    check("directory.csv: ';' or '|' separated lists, account_<tool> / accounts.<tool> columns, link method csv",
          from_csv["link_method"] == "csv" and rows["7"]["teams"] == ["Maths", "Engines"]
          and rows["7"]["accounts"] == {"github": "ada-gh", "slack": "U0ADA"}
          and rows["8"]["emails"] == ["bo@corp.example", "bo.li@corp.example"] and rows["8"]["manager"] == "7")

    # ---- settings: defaults name nobody; a family's conventions reach its projects only
    with tempfile.TemporaryDirectory() as tmp:
        plain, fam, unknown = Path(tmp) / "widgets", Path(tmp) / "apache-kafka", Path(tmp) / "apache-newproject"
        for p in (plain, fam, unknown):
            p.mkdir()
        s_plain = settings.project_settings({"tenant": "widgets"}, plain)
        m_fam, _ = settings.load_manifest(fam)
        s_fam = settings.project_settings(m_fam, fam)
        m_unknown, _ = settings.load_manifest(unknown)
        s_unknown = settings.project_settings(m_unknown, unknown)
        m_plain, _ = settings.load_manifest(plain)
        s_old = settings.project_settings({"project": "tvm", "settings": {"notifiers": {"bot@x.org": "jira"}}}, plain)
    check("generic defaults: no foundation's notifier addresses, directory tool 'directory', votes off, branch main",
          not any("apache" in k for k in s_plain["notifiers"]) and s_plain["directory_tool"] == "directory"
          and s_plain["vote_threads"] is False and s_plain["decision_prefix"] is None and s_plain["default_branch"] == "main"
          and s_plain["directory_login_tools"] == [] and m_plain["tenant"] == "widgets" and not m_plain.get("project"))
    check("the kafka preset on the apache family keeps Kafka's values (trunk, KIP, 4.3-style release branches, the ASF roster)",
          m_fam["tenant"] == "apache" and m_fam["project"] == "kafka" and s_fam["default_branch"] == "trunk"
          and s_fam["decision_prefix"] == "KIP" and s_fam["vote_threads"] is True and s_fam["directory_tool"] == "asf"
          and s_fam["notifiers"]["jira@apache.org"] == "tracker" and s_fam["release_branches"] == r"\d+\.\d+"
          and s_fam["release_branch_since"] == "2024-01-01" and s_fam["directory_email_domain"] == "apache.org")
    check("a family's folder with no preset still gets the family's conventions",
          m_unknown["tenant"] == "apache" and m_unknown["project"] == "newproject"
          and s_unknown["directory_tool"] == "asf" and s_unknown["decision_prefix"] is None)
    check("an older manifest's notifier names (jira, github) still work",
          s_old["notifiers"]["bot@x.org"] == "tracker" and s_old["notifiers"]["git@apache.org"] == "code_host")
    rb = re.compile(settings.DEFAULTS["release_branches"])
    check("the default release-branch pattern takes the common namings and leaves working branches alone",
          all(rb.fullmatch(n) for n in ("4.3", "release-1.18", "branch-3.5", "v0.15", "release/2.4.x", "stable-2.1"))
          and not any(rb.fullmatch(n) for n in ("main", "trunk", "feature/x-1.2", "dependabot/pip/a-1.2.3", "KAFKA-1234")))

    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed "
          f"({t['records']} records, {t['links']} links from {', '.join(report['sources'])})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
