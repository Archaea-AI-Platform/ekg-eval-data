"""Code pillar, git side: commits -> Commit, PARENT, CHANGES (files), authorship.

Reads git/commits.jsonl. Common shapes load as they are (normalize_commit):
  - this kit's own:   sha, parents, author_name, author_email, authored_at, committer_*, message, files[{status, path}]
  - split message:    subject + body instead of message
  - file lists:       files_changed / files as paths, or as {filename|path, status} with git (A/M/D/R) or
                      GitHub (added/modified/removed/renamed) statuses
  - GitHub's REST commit objects: {sha, commit: {message, author: {name, email, date}, committer: {...}},
    parents: [{sha}], files: [{filename, status}]}
A row with no sha is skipped and counted; a missing field is left empty, never guessed.

Authors are EmailAddress records (git has no accounts). A GitHub noreply address
("12345+login@users.noreply.github.com") names the GitHub account it belongs to, so it gets ADDRESS_OF to
that account directly. The committer of a GitHub web merge is GitHub itself, so it is not a person.
References queued for the linker: the PR number in the subject "(#n)", "Fixes #n" closing keywords and
other #n references (an issue or a PR, settled by the linker), ticket keys, and decision keys.

The commits are on the project's default branch (settings), and this adapter creates the Repository
record itself when no github/ folder did.
"""

import json
import re
from pathlib import Path

from .access import place
from .common import Graph, file_props, ticket_rx, utc

NOREPLY = re.compile(r"^(\d+)\+([^@]+)@users\.noreply\.github\.com$")
PRNUM = re.compile(r"\(#(\d+)\)\s*$")
CLOSES = re.compile(r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.I)
GH_REF = re.compile(r"(?<![\w/&])#(\d{1,6})\b")
STATUS = {"A": "added", "M": "modified", "D": "removed", "R": "renamed", "C": "copied", "T": "modified",
          "added": "added", "modified": "modified", "removed": "removed", "deleted": "removed", "renamed": "renamed",
          "copied": "copied", "changed": "modified"}


def _person(raw):
    """'Name <email>' or {name, email, date} -> (name, email, date)."""
    if isinstance(raw, dict):
        return raw.get("name"), raw.get("email"), raw.get("date")
    m = re.match(r"\s*(.*?)\s*<([^>]+)>", raw or "")
    return (m.group(1), m.group(2), None) if m else (raw, None, None)


def normalize_commit(c):
    """Any supported commit shape -> this kit's own, or None when there is no sha."""
    inner = c.get("commit") if isinstance(c.get("commit"), dict) else {}
    sha = c.get("sha") or c.get("hash") or c.get("id")
    if not sha:
        return None
    message = c.get("message") or inner.get("message")
    if message is None and (c.get("subject") or c.get("body")):
        message = "\n\n".join(x for x in (c.get("subject"), c.get("body")) if x)
    a_name, a_mail, a_date = _person(inner.get("author") or c.get("author"))
    k_name, k_mail, k_date = _person(inner.get("committer") or c.get("committer"))
    files = []
    for f in c.get("files") or c.get("files_changed") or []:
        if isinstance(f, str):
            files.append({"status": "M", "path": f})
        elif isinstance(f, dict) and (f.get("path") or f.get("filename")):
            files.append({"status": f.get("status") or "M", "path": f.get("path") or f.get("filename"),
                          **({"from": f.get("from") or f.get("previous_filename")} if f.get("from") or f.get("previous_filename") else {})})
    return {"sha": sha, "parents": [p["sha"] if isinstance(p, dict) else p for p in c.get("parents") or []],
            "author_name": c.get("author_name") or a_name, "author_email": c.get("author_email") or a_mail,
            "authored_at": c.get("authored_at") or c.get("author_date") or a_date or c.get("date"),
            "committer_name": c.get("committer_name") or k_name, "committer_email": c.get("committer_email") or k_mail,
            "committed_at": c.get("committed_at") or c.get("committer_date") or k_date or c.get("date"),
            "message": message or "", "files": files}


def ensure_repo(g: Graph, repo_name: str):
    """The Repository record the git adapter needs; made from the manifest when no github/ folder made it."""
    if "repo" not in g.ctx:
        public = g.ctx["settings"].get("public", True)
        scope, access = place(g, "github", "repo", repo_name or "repo", public=public)
        repo = g.rec("Repository", "github", repo_name or "repo", {"name": repo_name or "repo",
                     "default_branch": g.ctx["settings"]["default_branch"]}, scope)
        g.link("GOVERNED_BY", repo, access, "snapshot")
        g.ctx["repo"] = repo
        g.notes["git: repository record made from the manifest (no github/ folder)"] += 1
    return g.ctx["repo"]


def map_git(g: Graph, d: Path):
    s = g.ctx["settings"]
    repo = ensure_repo(g, g.ctx.get("repo_name", ""))
    scope = repo["scope"]
    key_rx = ticket_rx(g)
    decision_rx = re.compile(rf"\b{re.escape(s['decision_prefix'])}-(\d+)\b") if s.get("decision_prefix") else None
    github_issues = s.get("issue_tracker") == "github"
    branch = s["default_branch"]
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

    rows = []
    for n, line in enumerate(open(d / "git/commits.jsonl", encoding="utf-8"), 1):
        if not line.strip():
            continue
        try:
            c = normalize_commit(json.loads(line))
        except (json.JSONDecodeError, AttributeError, TypeError):
            c = None
        if c is None:
            g.notes["git: unreadable commit row skipped"] += 1
            continue
        rows.append(c)
    for c in rows:
        authored, committed = utc(c["authored_at"]), utc(c["committed_at"])
        off = re.search(r"([+-])(\d\d):?(\d\d)$", c["authored_at"] or "")
        co = re.findall(r"^Co-authored-by: (.+?) <([^>]+)>$", c["message"], re.M)
        commit = g.rec("Commit", "git", c["sha"], {
            "sha": c["sha"], "message": c["message"], "authored_at": authored, "committed_at": committed,
            "on_default_branch": True, "branch": branch, "co_authors": [f"{n} <{e}>" for n, e in co] or None,
            "author_tz_offset": (1 if off.group(1) == "+" else -1) * (int(off.group(2)) * 60 + int(off.group(3))) if off else None,
        }, scope, acl_from=repo)
        commits[c["sha"]] = commit
        g.link("IN", commit, repo, "field", valid_from=committed)
        if c["author_email"]:
            g.link("AUTHORED_BY", commit, address(c["author_email"], c["author_name"]), "field", role="author", at=authored)
        else:
            g.notes["git: commit with no author email (no author link)"] += 1
        for name, addr in co:
            g.link("AUTHORED_BY", commit, address(addr, name), "field", role="co_author", at=authored)
        if (c["committer_email"] or "").lower() == "noreply@github.com":
            g.notes["git: committed by GitHub's web merge (merger comes from the PR's merged_by)"] += 1
        elif c["committer_email"]:
            g.link("AUTHORED_BY", commit, address(c["committer_email"], c["committer_name"]), "field", role="committer",
                   at=committed)
        for fch in c["files"]:
            path = fch["path"]
            if path not in files:
                files[path] = g.rec("File", "git", f"{repo['props']['native_id']}:{path}", file_props(path), scope, acl_from=repo)
                g.link("IN", files[path], repo, "field")
            st = fch["status"]
            g.link("CHANGES", commit, files[path], "field", at=committed,
                   change_type=STATUS.get(st, STATUS.get(str(st)[:1].upper(), "modified")), previous_filename=fch.get("from"))

        subject = c["message"].split("\n")[0]
        m = PRNUM.search(subject)
        pr_num = int(m.group(1)) if m else None
        if pr_num:
            g.ref(commit, "MERGED_AS", "PullRequest", pr_num, "convention", 0.95, stub=True, reverse=True,
                  role="merge_result", valid_from=committed)
        for k in sorted(set(key_rx.findall(subject))):
            g.ref(commit, "IMPLEMENTS", "Ticket", k, "convention", 0.9, stub=True, valid_from=committed,
                  link_method="commit_message")
        if decision_rx:
            for num in set(decision_rx.findall(subject)):
                g.ref(commit, "ENACTS", "Decision", int(num), "convention", 0.9, valid_from=committed)
        if github_issues:  # GitHub issues: "Fixes #12" closes it; another #n names an issue or a PR
            closing = {int(n) for _, n in CLOSES.findall(c["message"])}
            for num in sorted(closing):
                g.ref(commit, "IMPLEMENTS", "GitHubIssue", num, "convention", 0.95, valid_from=committed,
                      link_method="closing_keyword", closes_on_merge=True)
            for num in sorted({int(n) for n in GH_REF.findall(c["message"])} - closing - {pr_num}):
                g.ref(commit, "MENTIONS", "GitHubRef", num, "text_rule", 0.7, kind="key", at=committed)
    for c in rows:
        for pos, parent in enumerate(c["parents"]):
            if parent in commits:
                g.link("PARENT", commits[c["sha"]], commits[parent], "field", position=pos)
    g.ctx["git_emails"] = emails
