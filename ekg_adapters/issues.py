"""Tickets pillar: GitHub issues -> Ticket, TicketState (open / closed), Label, Comment, dated IN_STATE.

Reads issues/*.json (one issue, or a list) and issues/*.jsonl. Two shapes load as they are:
  - a scraper's:   number, title, state, body, author, labels[], assignees[], createdAt, closedAt,
                   linkedPullRequests[], comments[{author, body, createdAt}]
  - GitHub REST:   number, title, state, state_reason, body, user{login, id}, labels[{name}], assignees[{login}],
                   created_at, closed_at, comments as a list (a count is ignored); pull requests (rows with
                   pull_request) are left to the GitHub adapter
An issue's identifier is "<owner/repo>#<number>". Issues follow the repository's scope, as the spec says.
Links: an issue's linked PRs (LINKS_TO, the code host's own link), and #n references in its body and
comments, which the linker settles to an issue or a PR (GitHub numbers both from one sequence).
"""

import json
import re
from pathlib import Path

from .common import Graph, utc
from .git import ensure_repo

GH_REF = re.compile(r"(?<![\w/&])#(\d{1,6})\b")
GH_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)")


def _rows(d: Path):
    for p in sorted((d / "issues").glob("*.json")) + sorted((d / "issues").glob("*.jsonl")):
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".jsonl":
            yield from (json.loads(line) for line in text.splitlines() if line.strip())
        else:
            obj = json.loads(text)
            yield from (obj if isinstance(obj, list) else obj.get("issues", [obj]) if isinstance(obj, dict) else [])


def _login(u):
    if isinstance(u, dict):
        return u.get("login"), u.get("id")
    return (u or None), None


def _names(xs):
    return [x.get("name") if isinstance(x, dict) else x for x in xs or [] if x]


def map_issues(g: Graph, d: Path):
    repo_name = g.ctx.get("repo_name", "")
    repo = ensure_repo(g, repo_name)
    scope = repo["scope"]
    accounts = g.ctx.setdefault("github_accounts", {})
    by_login = {a["props"].get("login"): a for a in accounts.values() if a["props"].get("login")}

    def account(u):
        login, uid = _login(u)
        if not login:
            return None
        if login not in by_login:
            a = g.rec("Account", "github", uid if uid else f"login:{login}", {"login": login, "status": "active",
                      "is_bot": login.endswith("[bot]"), "tool": "github"}, scope)
            accounts[uid if uid else f"login:{login}"] = a
            by_login[login] = a
        return by_login[login]

    states = {}

    def state(name, typ):
        if name not in states:
            states[name] = g.rec("TicketState", "github", f"{repo_name}:{name}", {"name": name, "type": typ}, scope)
        return states[name]

    labels, tickets = {}, g.ctx.setdefault("tickets", {})
    by_number = g.ctx.setdefault("issues_by_number", {})
    for row in _rows(d):
        try:
            if row.get("pull_request"):
                g.notes["issues: pull requests in the issue list (left to the GitHub adapter)"] += 1
                continue
            num = int(row["number"])
        except (KeyError, TypeError, ValueError):
            g.notes["issues: row without a number skipped"] += 1
            continue
        created = utc(row.get("created_at") or row.get("createdAt"))
        closed = utc(row.get("closed_at") or row.get("closedAt"))
        st = (row.get("state") or "open").lower()
        reason = (row.get("state_reason") or row.get("stateReason") or "").lower()
        canceled = st == "closed" and reason in ("not_planned", "duplicate")
        t = g.rec("Ticket", "github", f"{repo_name}#{num}", {
            "identifier": f"{repo_name}#{num}", "title": row.get("title"), "kind": "issue", "created_at": created,
            "completed_at": closed if st == "closed" and not canceled else None,
            "canceled_at": closed if canceled else None,
            "resolution": {"not_planned": "Won't Do", "duplicate": "Duplicate", "completed": "Fixed"}.get(reason)}, scope,
            acl_from=repo)
        tickets[f"{repo_name}#{num}"] = t
        by_number[num] = t
        g.link("IN", t, repo, "field", valid_from=created)
        author = account(row.get("author") or row.get("user"))
        if author:
            g.link("CREATED_BY", t, author, "field", role="creator", at=created)
        for a in row.get("assignees") or []:
            if account(a):
                g.link("ASSIGNED_TO", t, account(a), "snapshot")
        for name in _names(row.get("labels")):
            if name not in labels:
                labels[name] = g.rec("Label", "github", f"{repo_name}:{name}", {"name": name, "kind": "ticket"}, scope)
            g.link("HAS_LABEL", t, labels[name], "snapshot")
        # state: open from creation, then closed (or canceled) from the close time
        if st == "closed" and closed and created and closed > created:
            g.link("IN_STATE", t, state("open", "unstarted"), "field", valid_from=created, valid_to=closed,
                   started_at=created, ended_at=closed, cause="manual")
            g.link("IN_STATE", t, state("closed", "canceled" if canceled else "completed"), "field", valid_from=closed,
                   started_at=closed, cause="manual")
        else:
            g.link("IN_STATE", t, state(st, "completed" if st == "closed" else "unstarted"), "field", valid_from=created,
                   started_at=created, cause="manual")
        if row.get("body"):
            cv = g.rec("ContentVersion", "github", f"{repo_name}#{num}:body", {"version": "current", "valid_from": created,
                       "text": row["body"], "note": "description"}, scope, acl_from=t)
            g.link("VERSION_OF", cv, t, "field", kind="revision", order=1)
        for pr in row.get("linkedPullRequests") or row.get("linked_pull_requests") or []:
            n = pr.get("number") if isinstance(pr, dict) else pr
            if isinstance(n, str) and "/pull/" in n:
                n = n.rsplit("/", 1)[-1]
            try:
                g.ref(t, "LINKS_TO", "PullRequest", int(n), "integration", stub=True, kind="url", at=created)
            except (TypeError, ValueError):
                pass
        _refs(g, t, row.get("body") or "", num, created, repo_name)
        comments = row.get("comments") if isinstance(row.get("comments"), list) else []
        for i, c in enumerate(comments):
            at = utc(c.get("created_at") or c.get("createdAt"))
            cm = g.rec("Comment", "github", c.get("id") or f"{repo_name}#{num}:c{i}", {"text": c.get("body"),
                       "kind": "conversation", "at": at}, scope, acl_from=t)
            g.link("ON", cm, t, "field", at=at)
            if account(c.get("author") or c.get("user")):
                g.link("AUTHORED_BY", cm, account(c.get("author") or c.get("user")), "field", role="author", at=at)
            _refs(g, cm, c.get("body") or "", num, at, repo_name)
    g.notes["issues: GitHub issues loaded"] = len(by_number)


def _refs(g, rec, text, own, at, repo_name):
    for r, what, n in sorted(set(GH_URL.findall(text))):
        if r.lower() == repo_name.lower() and int(n) != own:
            g.ref(rec, "LINKS_TO", "PullRequest" if what == "pull" else "GitHubRef", int(n), "field", 0.95, kind="url", at=at)
    for n in {int(x) for x in GH_REF.findall(text)} - {own}:
        g.ref(rec, "MENTIONS", "GitHubRef", n, "text_rule", 0.7, kind="key", at=at)
