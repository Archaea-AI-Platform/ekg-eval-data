"""Code pillar, git side: commits from the clone's log -> Commit, PARENT, CHANGES (files), authorship.

Authors are EmailAddress records (git has no accounts). A GitHub noreply address
("12345+login@users.noreply.github.com") names the GitHub account it belongs to, so
it gets ADDRESS_OF to that account directly. The committer of a GitHub web merge is
GitHub itself, so it is not recorded as a person. References queued for the linker:
the PR number in the subject "(#n)", ticket keys and KIP numbers.
"""

import json
import re
from pathlib import Path

from .common import Graph, file_props, ticket_rx, utc

NOREPLY = re.compile(r"^(\d+)\+([^@]+)@users\.noreply\.github\.com$")
PRNUM = re.compile(r"\(#(\d+)\)\s*$")
KIP = re.compile(r"\bKIP-(\d+)\b")
STATUS = {"A": "added", "M": "modified", "D": "removed", "R": "renamed", "C": "copied", "T": "modified"}


def map_git(g: Graph, d: Path):
    repo = g.ctx["repo"]
    scope = repo["scope"]
    key_rx = ticket_rx(g)
    files, commits, emails = g.ctx.setdefault("files", {}), g.ctx.setdefault("commits", {}), {}
    gh_accounts = g.ctx.setdefault("github_accounts", {})

    def address(addr, name):
        addr = addr.lower()
        if addr not in emails:
            emails[addr] = g.rec("EmailAddress", "email", addr, {"address": addr, "display_name": name,
                                 "normalized_value": addr}, scope)
            m = NOREPLY.match(addr)
            if m:  # the address itself names the GitHub account
                uid, login = int(m.group(1)), m.group(2)
                acct = gh_accounts.get(uid) or g.rec("Account", "github", uid, {"login": login, "status": "active",
                                                     "tool": "github"}, scope)
                gh_accounts[uid] = acct
                g.link("ADDRESS_OF", emails[addr], acct, "field", confidence=1.0)
        return emails[addr]

    rows = [json.loads(line) for line in open(d / "git/commits.jsonl", encoding="utf-8")]
    for c in rows:
        off = re.search(r"([+-])(\d\d):(\d\d)$", c["authored_at"])
        co = re.findall(r"^Co-authored-by: (.+?) <([^>]+)>$", c["message"], re.M)
        commit = g.rec("Commit", "git", c["sha"], {
            "sha": c["sha"], "message": c["message"], "authored_at": utc(c["authored_at"]),
            "committed_at": utc(c["committed_at"]), "on_default_branch": True, "branch": "trunk",
            "co_authors": [f"{n} <{e}>" for n, e in co] or None,
            "author_tz_offset": (1 if off.group(1) == "+" else -1) * (int(off.group(2)) * 60 + int(off.group(3))) if off else None,
        }, scope, acl_from=repo)
        commits[c["sha"]] = commit
        g.link("IN", commit, repo, "field", valid_from=utc(c["committed_at"]))
        g.link("AUTHORED_BY", commit, address(c["author_email"], c["author_name"]), "field", role="author",
               at=utc(c["authored_at"]))
        for name, addr in co:
            g.link("AUTHORED_BY", commit, address(addr, name), "field", role="co_author", at=utc(c["authored_at"]))
        if c["committer_email"] == "noreply@github.com":
            g.notes["git: committed by GitHub's web merge (merger comes from the PR's merged_by)"] += 1
        else:
            g.link("AUTHORED_BY", commit, address(c["committer_email"], c["committer_name"]), "field", role="committer",
                   at=utc(c["committed_at"]))
        for fch in c["files"]:
            path = fch["path"]
            if path not in files:
                files[path] = g.rec("File", "git", f"{repo['props']['native_id']}:{path}", file_props(path), scope, acl_from=repo)
                g.link("IN", files[path], repo, "field")
            g.link("CHANGES", commit, files[path], "field", at=utc(c["committed_at"]),
                   change_type=STATUS.get(fch["status"][0], "modified"), previous_filename=fch.get("from"))

        subject = c["message"].split("\n")[0]
        m = PRNUM.search(subject)
        if m:
            g.ref(commit, "MERGED_AS", "PullRequest", int(m.group(1)), "convention", 0.95, stub=True, reverse=True,
                  role="merge_result", valid_from=utc(c["committed_at"]))
        for k in sorted(set(key_rx.findall(subject))):
            g.ref(commit, "IMPLEMENTS", "Ticket", k, "convention", 0.9, stub=True, valid_from=utc(c["committed_at"]),
                  link_method="commit_message")
        for n in set(KIP.findall(subject)):
            g.ref(commit, "ENACTS", "Decision", int(n), "convention", 0.9, valid_from=utc(c["committed_at"]))
    for c in rows:
        for pos, parent in enumerate(c["parents"]):
            if parent in commits:
                g.link("PARENT", commits[c["sha"]], commits[parent], "field", position=pos)
    g.ctx["git_emails"] = emails
