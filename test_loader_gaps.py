"""Tests for the loader rules added from the evaluation's failed questions, on small synthetic fixtures.

fixtures/acme (+ acme_directory) with more laid over it in a temporary folder:
  - mail: a thread spread over two monthly archives whose last reply cut References short, a reply the list
    relayed with the sender kept in Cc, one relayed with nothing kept, a release announcement and a vote result
  - jira: a release version and a ticket whose duplicate link is known only from its inward side
  - github: the repository's owner organisation, a pull request with bot activity through an app, a timeline with
    labels, a close, an assignment, a subscription and a mention, commit committers, a bot seen under two logins
  - git: a real (tiny) repo.git with a cherry-pick on a release branch (identical blob changes), a web-merge
    committer and a bot's noreply author
  - code: a checkout with a labeler workflow + config and a CODEOWNERS file
and a second dataset with an ASF-style roster and board minutes (a chair change).

    python -I ekg-eval-data/test_loader_gaps.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters.pipeline import run  # noqa: E402
from test_adapters import G  # noqa: E402

QM = HERE.parent / "query-master"

MBOX_SEP = """From a@x Thu Oct  1 09:00:00 2026
From: Alice Ng <alice@acme.io>
To: eng@acme.io
Subject: Retry budget
Date: Thu, 10 Sep 2026 09:00:00 +0000
Message-ID: <r1@acme.io>
List-Id: <eng.acme.io>

Should retries have a budget?

"""
MBOX_NOV = """From a@x Thu Nov  1 09:00:00 2026
From: Bob Ruiz <bob@acme.io>
To: eng@acme.io
Subject: Re: Retry budget
Date: Sun, 01 Nov 2026 09:00:00 +0000
Message-ID: <p1@acme.io>
In-Reply-To: <r1@acme.io>
References: <r1@acme.io>
List-Id: <eng.acme.io>

Yes.

From a@x Thu Nov  2 09:00:00 2026
From: "Carol Diaz via eng" <eng@acme.io>
Reply-To: <eng@acme.io>
Cc: Carol Diaz <carol@acme.io>
To: eng@acme.io
Subject: Re: Retry budget
Date: Mon, 02 Nov 2026 09:00:00 +0000
Message-ID: <p2@acme.io>
In-Reply-To: <p1@acme.io>
References: <p1@acme.io>
List-Id: <eng.acme.io>

Agreed, a budget of three.

From a@x Thu Nov  3 09:00:00 2026
From: "Dan Wu via eng" <eng@acme.io>
Reply-To: <eng@acme.io>
To: eng@acme.io
Subject: A question
Date: Tue, 03 Nov 2026 09:00:00 +0000
Message-ID: <q1@acme.io>
List-Id: <eng.acme.io>

Hello?

From a@x Thu Nov  4 09:00:00 2026
From: Alice Ng <alice@acme.io>
To: eng@acme.io
Subject: [ANNOUNCE] Payments 1.2.0 released
Date: Wed, 04 Nov 2026 09:00:00 +0000
Message-ID: <a1@acme.io>
List-Id: <eng.acme.io>

Payments 1.2.0 is out.

From a@x Thu Nov  5 09:00:00 2026
From: Alice Ng <alice@acme.io>
To: eng@acme.io
Subject: Re: [VOTE] RFC-7 retry budget for refunds
Date: Thu, 05 Nov 2026 09:00:00 +0000
Message-ID: <v9@acme.io>
In-Reply-To: <v2@acme.io>
References: <v1@acme.io> <v2@acme.io>
List-Id: <eng.acme.io>

The vote passes with 3 binding +1 votes.

"""

PR43 = {
    "pull": {"id": 7700043, "number": 43, "title": "Split retry module", "state": "closed", "draft": False,
             "created_at": "2026-10-02T08:30:00Z", "merged_at": None, "closed_at": "2026-10-03T10:00:00Z",
             "base": {"ref": "main", "label": "acme:main"}, "head": {"ref": "dan/split", "label": "acme:dan/split"},
             "user": {"id": 503, "login": "dan-gh", "type": "User"}, "labels": [{"name": "payments"}],
             "assignees": [{"id": 504, "login": "erin-gh", "type": "User"}]},
    "reviews": [{"id": 8901, "user": {"id": 600, "login": "reviewer-bot[bot]", "type": "Bot"}, "state": "COMMENTED",
                 "submitted_at": "2026-10-02T09:00:00Z", "commit_id": "c" * 40, "body": "Looks fine"}],
    "review_comments": [{"id": 8902, "user": {"id": 600, "login": "Reviewer", "type": "Bot"}, "body": "nit",
                         "path": "src/a/x.py", "line": 3, "created_at": "2026-10-02T09:01:00Z", "commit_id": "c" * 40}],
    "issue_comments": [{"id": 8903, "user": {"id": 601, "login": "labelbot[bot]", "type": "Bot"}, "body": "labelled",
                        "created_at": "2026-10-02T08:31:00Z",
                        "performed_via_github_app": {"id": 77001, "slug": "labelbot", "name": "Label Bot",
                                                     "owner": {"id": 77, "login": "acme", "type": "Organization"}}}],
    "commits": [{"sha": "c" * 40, "commit": {"message": "split", "author": {"date": "2026-10-02T08:00:00Z"},
                                             "committer": {"date": "2026-10-02T08:05:00Z"}},
                 "author": {"id": 503, "login": "dan-gh", "type": "User"},
                 "committer": {"id": 505, "login": "web-flow", "type": "User"}}],
    "files": [{"filename": "src/a/x.py", "status": "modified", "additions": 1, "deletions": 0},
              {"filename": "src/a/y.py", "status": "added", "additions": 5, "deletions": 0}],
    "timeline": [
        {"event": "labeled", "created_at": "2026-10-02T08:31:00Z", "label": {"name": "payments"},
         "actor": {"id": 601, "login": "labelbot[bot]", "type": "Bot"}},
        {"event": "assigned", "created_at": "2026-10-02T08:40:00Z", "actor": {"id": 503, "login": "dan-gh", "type": "User"},
         "assignee": {"id": 504, "login": "erin-gh", "type": "User"}},
        {"event": "subscribed", "created_at": "2026-10-02T08:41:00Z", "actor": {"id": 502, "login": "carol-gh", "type": "User"}},
        {"event": "mentioned", "created_at": "2026-10-02T08:42:00Z", "actor": {"id": 500, "login": "alice-gh", "type": "User"}},
        {"event": "closed", "created_at": "2026-10-03T10:00:00Z", "actor": {"id": 500, "login": "alice-gh", "type": "User"}},
        {"event": "review_requested", "created_at": "2026-10-02T08:50:00Z", "actor": {"id": 503, "login": "dan-gh", "type": "User"},
         "requested_reviewer": {"id": 600, "login": "Reviewer", "type": "Bot"},
         "performed_via_github_app": {"id": 77002, "slug": "reviewer-bot", "name": "Reviewer Bot"}},
    ],
}

JIRA_ISSUE = {"issues": [{"id": "1001", "key": "PAY-1", "fields": {
    "summary": "Retry refunds twice", "issuetype": {"name": "Bug"}, "created": "2026-10-01T10:00:00.000+0000",
    "status": {"id": "1", "name": "Open"}, "resolution": None, "comment": {"comments": []},
    "issuelinks": [{"type": {"name": "Duplicate", "outward": "duplicates", "inward": "is duplicated by"},
                    "inwardIssue": {"key": "PAY-999"}}]},
    "changelog": {"histories": []}}]}


def git(cwd, *args, date="2026-10-02T12:00:00+00:00", **who):
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "GIT_AUTHOR_NAME": who.get("an", "Bob Ruiz"), "GIT_AUTHOR_EMAIL": who.get("ae", "bob@acme.io"),
           "GIT_COMMITTER_NAME": who.get("cn", "Bob Ruiz"), "GIT_COMMITTER_EMAIL": who.get("ce", "bob@acme.io")}
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", *args], cwd=cwd,
                          env=env, check=True, capture_output=True).stdout.decode().strip()


def make_repo(tmp: Path, data: Path):
    """main: A (adds f.txt, by a bot's noreply address), B (f.txt 1 -> 2, web-merge committer); branch 1.2 off A:
    C, the same change as B. Returns {name: sha}."""
    work = tmp / "work"
    work.mkdir()
    git(work, "init", "-q")
    (work / "f.txt").write_text("1\n", encoding="utf-8")
    git(work, "add", "f.txt")
    git(work, "commit", "-q", "-m", "add f", an="dependabot[bot]", ae="49699333+dependabot[bot]@users.noreply.github.com")
    a = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "-b", "1.2")
    (work / "f.txt").write_text("2\n", encoding="utf-8")
    git(work, "commit", "-q", "-am", "two on 1.2", date="2026-10-02T13:00:00+00:00")
    c = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "main")
    (work / "f.txt").write_text("2\n", encoding="utf-8")
    git(work, "commit", "-q", "-am", "two", date="2026-10-02T12:30:00+00:00", cn="GitHub", ce="noreply@github.com")
    b = git(work, "rev-parse", "HEAD")
    (data / "git").mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(data / "git" / "repo.git")], check=True, capture_output=True)
    rows = []
    for sha, parents, msg, an, ae, cn, ce, at in (
            (a, [], "add f", "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com", "Bob Ruiz",
             "bob@acme.io", "2026-10-02T12:00:00+00:00"),
            (b, [a], "two", "Bob Ruiz", "bob@acme.io", "GitHub", "noreply@github.com", "2026-10-02T12:30:00+00:00")):
        rows.append({"sha": sha, "parents": parents, "author_name": an, "author_email": ae, "authored_at": at,
                     "committer_name": cn, "committer_email": ce, "committed_at": at, "message": msg,
                     "files": [{"status": "A" if not parents else "M", "path": "f.txt"}]})
    (data / "git" / "commits.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return {"A": a, "B": b, "C": c}


def build_main(tmp: Path):
    data, out = tmp / "acme-gaps", tmp / "out"
    shutil.copytree(HERE / "fixtures" / "acme", data)
    shutil.copytree(HERE / "fixtures" / "acme_directory", data, dirs_exist_ok=True)
    m = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    m["jira"] = "PAY"
    m["settings"]["decision_prefix"] = "RFC"
    m["settings"]["vote_threads"] = True
    m["window"]["since"] = "2026-09-01"
    (data / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    (data / "mail" / "eng" / "2026-09.mbox").write_text(MBOX_SEP, encoding="utf-8")
    (data / "mail" / "eng" / "2026-11.mbox").write_text(MBOX_NOV, encoding="utf-8")
    repo = json.loads((data / "github" / "repo.json").read_text(encoding="utf-8"))
    repo["owner"] = {"login": "acme", "id": 77, "type": "Organization"}
    repo["created_at"] = "2020-01-01T00:00:00Z"
    (data / "github" / "repo.json").write_text(json.dumps(repo), encoding="utf-8")
    (data / "github" / "pulls" / "pr-43.json").write_text(json.dumps(PR43), encoding="utf-8")
    (data / "jira" / "issues").mkdir(parents=True)
    (data / "jira" / "statuses.json").write_text(json.dumps([{"statuses": [
        {"id": "1", "name": "Open", "statusCategory": {"key": "new"}}]}]), encoding="utf-8")
    (data / "jira" / "versions.json").write_text(json.dumps([
        {"id": "10", "name": "1.2.0", "released": True, "releaseDate": "2026-11-03"}]), encoding="utf-8")
    (data / "jira" / "issues" / "p1.json").write_text(json.dumps(JIRA_ISSUE), encoding="utf-8")
    code = data / "code"
    for path, text in {
        "src/a/x.py": "x = 1\n", "src/a/y.py": "y = 2\n", "src/refunds.py": "def refund():\n    pass\n",
        "build.gradle": "\n",
        ".github/workflows/label.yml": "on: pull_request_target\njobs:\n  label:\n    runs-on: ubuntu-latest\n"
                                       "    steps:\n      - uses: actions/labeler@v5\n        with:\n"
                                       "          configuration-path: .github/configs/labeler.yml\n",
        ".github/configs/labeler.yml": "payments:\n- changed-files:\n  - any-glob-to-any-file:\n    - 'src/a/**'\n"
                                       "build:\n- changed-files:\n  - any-glob-to-any-file: ['*.gradle']\n"
                                       "  - head-branch: ['^src']\n",
        "CODEOWNERS": "# owners\n/src/a/ @bob-gh\n*.py @alice-gh @acme/team\n",
    }.items():
        (code / path).parent.mkdir(parents=True, exist_ok=True)
        (code / path).write_text(text, encoding="utf-8")
    shas = make_repo(tmp, data)
    report = run(data, QM, out, log=lambda *_: None)
    return report, G(out), shas


ROSTER_MINUTES = """<html><body>
21 Nov 2012 ¶ Special Order 7A, Establish the Apache Proj Project, was approved by Unanimous Vote.
RESOLVED, that Ann Alder (ann) be appointed to the office of Vice President, Apache Proj.
19 Apr 2023 ¶ Special Order 7B, Change the Apache Proj Project Chair, was approved by Unanimous Vote.
WHEREAS, the Board of Directors heretofore appointed Ann Alder (ann) to the office of Vice President, Apache Proj,
NOW, THEREFORE, BE IT RESOLVED, that Ben Birch (ben) be and hereby is appointed to the office of Vice President, Apache Proj.
</body></html>"""


def build_roster(tmp: Path):
    data, out = tmp / "roster", tmp / "out-roster"
    (data / "people").mkdir(parents=True)
    (data / "extra" / "people").mkdir(parents=True)
    (data / "manifest.json").write_text(json.dumps({"tenant": "t", "project": "proj", "fetched_at": "2026-10-05T00:00:00Z",
                                                    "settings": {"directory_tool": "asf", "directory_email_domain": "x.org"}}),
                                        encoding="utf-8")
    (data / "people" / "public_ldap_people.json").write_text(json.dumps({"people": {
        "ann": {"name": "Ann Alder", "createTimestamp": "20120101000000Z"},
        "ben": {"name": "Ben Birch", "createTimestamp": "20150101000000Z"}}}), encoding="utf-8")
    (data / "people" / "committee-info.json").write_text(json.dumps({"committees": {"proj": {
        "display_name": "Proj", "roster": {"ann": {"date": "2012-11-21"}, "ben": {"date": "2015-01-01"}},
        "chair": {"ben": {"name": "Ben Birch"}}}}}), encoding="utf-8")
    (data / "people" / "public_ldap_projects.json").write_text(json.dumps({"projects": {"proj": {
        "members": ["ann", "ben"], "owners": ["ann", "ben"]}}}), encoding="utf-8")
    (data / "extra" / "people" / "board_minutes_Proj.html").write_text(ROSTER_MINUTES, encoding="utf-8")
    report = run(data, QM, out, log=lambda *_: None)
    return report, G(out)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as tmp:
        report, g, shas = build_main(Path(tmp))
        r_report, rg = build_roster(Path(tmp))

    t = report["totals"]
    check("every record and link validates", t["records"] == t["records_valid"] and t["links"] == t["links_valid"])
    by_type = lambda lt: [lk for lk in g.links if lk["type"] == lt]
    rec = g.by_id

    # ---- mail: threads across archives and a cut References chain
    msgs = {r["props"]["message_id"]: r for r in g.recs if r["type"] == "EmailMessage"}
    thread_of = {lk["a"]: lk["b"] for lk in by_type("IN") if rec[lk["b"]]["type"] == "EmailThread"}
    check("mail: a reply whose References name only its parent joins the thread of that parent's root",
          thread_of[msgs["<p2@acme.io>"]["id"]] == thread_of[msgs["<r1@acme.io>"]["id"]] ==
          thread_of[msgs["<p1@acme.io>"]["id"]])
    check("mail: the joined thread keeps the root id of its earliest message",
          rec[thread_of[msgs["<p2@acme.io>"]["id"]]]["props"]["root_message_id"] == "<r1@acme.io>")
    check("mail: the join is counted", report["notes"].get("mail: thread records merged away by that join") == 1)

    # ---- mail: relayed senders
    carol = g.one("EmailAddress", address="carol@acme.io")
    frm = g.linked("FROM", msgs["<p2@acme.io>"], carol)
    check("mail: a relayed sender kept in Cc under the same name gets FROM, method inferred, kind relayed",
          len(frm) == 1 and frm[0]["props"]["method"] == "inferred" and frm[0]["props"]["kind"] == "relayed"
          and frm[0]["props"]["confidence"] < 1)
    check("mail: the relayed sender's own Cc copy is not a recipient", not g.linked("SENT_TO", msgs["<p2@acme.io>"], carol))
    check("mail: the relayed sender takes part in the thread",
          any(lk["a"] == carol["id"] and lk["b"] == thread_of[msgs["<p2@acme.io>"]["id"]] for lk in by_type("PARTICIPATED_IN")))
    check("mail: a relayed sender nothing preserves gets no FROM, and is counted",
          not [lk for lk in by_type("FROM") if lk["a"] == msgs["<q1@acme.io>"]["id"]]
          and any("relayed sender not recovered" in k for k in report["notes"]))
    alice = g.one("EmailAddress", address="alice@acme.io")
    check("mail: the thread starter's participation is role started",
          any(lk["a"] == alice["id"] and lk["b"] == thread_of[msgs["<r1@acme.io>"]["id"]] and lk["props"]["role"] == "started"
              for lk in by_type("PARTICIPATED_IN")))

    # ---- mail: release announcement, vote result
    rel = g.one("Release", version="1.2.0")
    ann = g.linked("ANNOUNCES", msgs["<a1@acme.io>"], rel)
    check("mail: an [ANNOUNCE] naming a release's version ANNOUNCES that Release (kind announce)",
          len(ann) == 1 and ann[0]["props"]["kind"] == "announce")
    # (no wiki here, so no Decision record: the MADE_IN references are queued and counted unresolved)
    check("mail: the vote result message is queued as where the decision was made (MADE_IN), besides the thread's first",
          report["notes"].get("mail: vote result messages (MADE_IN their Decision)") == 1
          and report["linker"]["unresolved"].get("Decision → EmailMessage: MADE_IN (text_rule)", 0) >= 2)

    # ---- jira: a duplicate link known only from the inward side
    pay1 = g.one("Ticket", identifier="PAY-1")
    pay999 = g.one("Ticket", identifier="PAY-999")
    check("jira: an inward duplicate link to an issue outside the download is written in the outward direction",
          len(g.linked("DUPLICATE_OF", pay999, pay1)) == 1 and not g.linked("DUPLICATE_OF", pay1, pay999))

    # ---- github: workspace, apps and bots, actors, committers, aliases
    repo = g.one("Repository", name="acme/payments")
    ws = g.native("Workspace", "github", "77")
    check("github: the owner organisation is a Workspace (kind code) and the Repository is IN it",
          ws["props"]["kind"] == "code" and g.linked("IN", repo, ws))
    app = g.one("App", name="Label Bot")
    bot = g.native("Account", "github", "601")
    check("github: an app the source names is an App, and its bot account is BOT_OF it",
          bot["props"]["is_bot"] is True and g.linked("BOT_OF", bot, app))
    check("github: a bot seen as '<app slug>[bot]' is BOT_OF that app (the code host's naming), a human acting "
          "through an app is not", g.linked("BOT_OF", g.native("Account", "github", "600"), g.one("App", name="Reviewer Bot"),
                                            method="convention")
          and not [lk for lk in by_type("BOT_OF") if lk["a"] == g.native("Account", "github", "503")["id"]])
    pr43 = g.one("PullRequest", number=43)
    acc = lambda i: g.native("Account", "github", str(i))
    check("github: a timeline close is ACTED_BY its actor (role closer, action close)",
          any(lk["props"]["action"] == "close" and lk["props"]["role"] == "closer" for lk in g.linked("ACTED_BY", pr43, acc(500))))
    check("github: a bot's labelling is ACTED_BY the bot (action triage)",
          any(lk["props"]["action"] == "triage" for lk in g.linked("ACTED_BY", pr43, bot)))
    check("github: an assignment event is a dated ASSIGNED_TO",
          any(lk["props"].get("valid_from") == "2026-10-02T08:40:00Z" for lk in g.linked("ASSIGNED_TO", pr43, acc(504))))
    check("github: a subscription is SUBSCRIBED and a mention MENTIONS",
          g.linked("SUBSCRIBED", acc(502), pr43) and g.linked("MENTIONS", pr43, acc(500)))
    pr_commit = g.native("Commit", "git", "c" * 40)
    check("github: a PR commit's committer account is AUTHORED_BY role committer",
          g.linked("AUTHORED_BY", pr_commit, acc(505), role="committer"))
    reviewer = acc(600)
    alias = [lk for lk in by_type("ALIAS_OF") if lk["b"] == reviewer["id"]]
    check("github: an account seen under a second login keeps it as an Alias",
          len(alias) == 1 and rec[alias[0]["a"]]["props"]["value"] in ("Reviewer", "reviewer-bot[bot]"))

    # ---- git: committers, bots, file lists and identical changes
    ca, cb, cc = (g.native("Commit", "git", shas[k]) for k in "ABC")
    web = g.one("EmailAddress", address="noreply@github.com")
    check("git: a web-merge committer is AUTHORED_BY role committer to the code host's automation address",
          web["props"].get("kind") == "automation" and g.linked("AUTHORED_BY", cb, web, role="committer"))
    dep = next(r for r in g.recs if r["type"] == "Account" and r["props"].get("login") == "dependabot[bot]")
    check("git: a [bot] noreply address names a bot account", dep["props"].get("is_bot") is True)
    fpath = next(r for r in g.recs if r["type"] == "File" and r["props"]["path"] == "f.txt")
    check("git: a release-branch commit gets its file list from repo.git's trees (CHANGES)",
          cc["props"].get("on_default_branch") is False and g.linked("CHANGES", cc, fpath, change_type="modified"))
    eq = g.linked("EQUIVALENT_TO", cc, cb)
    check("git: the cherry-pick with identical blob changes is EQUIVALENT_TO the earlier commit (derived, timeless)",
          len(eq) == 1 and eq[0]["props"].get("derived") and not eq[0]["props"].get("valid_from")
          and not g.linked("EQUIVALENT_TO", ca, cb))

    # ---- code: labeler, code owners, change folders
    folder = lambda p: g.one("Folder", path=p)
    payments = g.one("Label", name="payments", kind="code")
    check("code: the labeler config's folder glob gives that Folder the label (HAS_LABEL)",
          g.linked("HAS_LABEL", folder("src/a"), payments))
    build = g.one("Label", name="build", kind="code")
    gradle = next(r for r in g.recs if r["type"] == "File" and r["props"]["path"] == "build.gradle")
    check("code: a file glob gives the files it matches the label; branch matchers are not paths",
          g.linked("HAS_LABEL", gradle, build) and not [lk for lk in by_type("HAS_LABEL") if lk["b"] == build["id"]
                                                        and rec[lk["a"]]["type"] == "Folder"])
    rules = [r for r in g.recs if r["type"] == "CodeOwnersRule"]
    check("code: one CodeOwnersRule per code-owners line", len(rules) == 2)
    check("code: a directory line MATCHES and OWNS its folder",
          g.linked("OWNED_BY", folder("src/a"), acc(501), source="codeowners"))
    refunds = next(r for r in g.recs if r["type"] == "File" and r["props"]["path"] == "src/refunds.py")
    check("code: an unanchored pattern owns matching files anywhere; a team owner is counted, not linked",
          g.linked("OWNED_BY", refunds, acc(500)) and report["notes"].get(
              "code: code owners not linked (a team, an address or an unknown login)", 0) >= 1)
    check("code: a PR is ABOUT the deepest folder holding every file it changed",
          g.linked("ABOUT", pr43, folder("src/a"), kind="changed_folder"))

    # ---- people: a chair change
    t = r_report["totals"]
    check("roster: every record and link validates", t["records"] == t["records_valid"] and t["links"] == t["links_valid"])
    chair = rg.one("Position", kind="chair")
    change = rg.one("Decision", identifier="Special Order 7B")
    ann_p, ben_p = rg.one("Person", full_name="Ann Alder"), rg.one("Person", full_name="Ben Birch")
    check("roster: the seat-change decision has its title and AFFECTS the seat",
          change["props"].get("title", "").startswith("Change the") and rg.linked("AFFECTS", change, chair))
    check("roster: it is ABOUT the person appointed and the one replaced",
          rg.linked("ABOUT", change, ben_p, role="appointed") and rg.linked("ABOUT", change, ann_p, role="replaced"))
    past = rg.linked("HOLDS_POSITION", ann_p, chair, valid_to="2023-04-19T00:00:00Z")
    check("roster: the term it ended carries both resolutions as evidence",
          past and change["id"] in past[0]["props"]["evidence"])
    now = [lk for lk in rg.linked("HOLDS_POSITION", ben_p, chair) if change["id"] in lk["props"]["evidence"]]
    check("roster: the term it started carries it as evidence", bool(now))

    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(ok for _, ok in checks)
    print(f"\n{passed} of {len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
