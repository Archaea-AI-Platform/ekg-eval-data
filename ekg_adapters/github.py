"""Code pillar, GitHub side: repository, pull requests, reviews, review threads, comments, branches,
labels (dated from the timeline), review requests, changed files, CI workflows and runs, tags.

APPROVED_BY is derived from the reviews (derived, with a rule id) and marked stale
when a commit landed after the approval. References queued for the linker: ticket
keys and KIP numbers in PR titles.
"""

import re
from pathlib import Path

from .access import place
from .common import Graph, J, file_props, ticket_rx, utc

KIP = re.compile(r"\bKIP-(\d+)\b")
CHANGE_TYPE = {"added": "added", "removed": "removed", "modified": "modified", "renamed": "renamed",
               "copied": "copied", "changed": "modified", "unchanged": "modified"}


def map_github(g: Graph, d: Path, repo_name: str):
    key_rx = ticket_rx(g)
    repo_json = J(d / "github/repo.json")
    public = repo_json.get("visibility", "public") == "public" and not repo_json.get("private")
    scope, access = place(g, "github", "repo", repo_name, public=public)
    repo = g.rec("Repository", "github", repo_json["id"], {"name": repo_json["full_name"], "visibility": repo_json.get("visibility"),
                 "archived": repo_json.get("archived"), "is_fork": repo_json.get("fork"),
                 "default_branch": repo_json.get("default_branch")}, scope)
    g.link("GOVERNED_BY", repo, access, "snapshot")
    g.ctx["repo"] = repo

    accounts = g.ctx.setdefault("github_accounts", {})

    def account(u):
        if not u:
            return None
        if u["id"] not in accounts:
            accounts[u["id"]] = g.rec("Account", "github", u["id"], {"login": u["login"], "status": "active",
                                      "is_bot": u.get("type") == "Bot", "tool": "github"}, scope)
        return accounts[u["id"]]

    # who may read a private repo: its collaborators (GET /repos/{repo}/collaborators), level from their role
    if (d / "github/collaborators.json").exists():
        for c in J(d / "github/collaborators.json"):
            role = c.get("role_name") or next((r for r in ("admin", "maintain", "push", "triage", "pull")
                                               if (c.get("permissions") or {}).get(r)), "pull")
            level = {"admin": "admin", "maintain": "write", "write": "write", "push": "write"}.get(role, "read")
            g.link("CAN_ACCESS", account(c), access, "snapshot", level=level, via="direct")
    elif not public:
        g.notes["github: private repo with no collaborator list: visible to nobody (fails closed)"] += 1
    # the org's SAML SSO identities (GraphQL organization.samlIdentityProvider.externalIdentities): the only
    # proof of who a GitHub account belongs to; without them GitHub accounts are inferred and grant nothing
    if (d / "github/external_identities.json").exists():
        for e in J(d / "github/external_identities.json"):
            acct = account({"id": e["id"], "login": e["login"], "type": "User"})
            acct["props"]["emails"] = [e["email"].lower()]
            g.ctx.setdefault("github_saml", set()).add(acct["id"])

    branches, files, label_recs = {}, g.ctx.setdefault("files", {}), {}

    def branch(ref):
        if ref not in branches:
            branches[ref] = g.rec("Branch", "github", f"{repo_json['id']}:{ref}", {"name": ref}, scope, acl_from=repo)
        return branches[ref]

    def label(name):
        if name not in label_recs:
            label_recs[name] = g.rec("Label", "github", f"{repo_json['id']}:{name}", {"name": name, "kind": "code"}, scope)
        return label_recs[name]

    prs = g.ctx.setdefault("prs", {})
    for path in sorted((d / "github/pulls").glob("pr-*.json")):
        x = J(path)
        p = x["pull"]
        created, merged, closed = utc(p["created_at"]), utc(p["merged_at"]), utc(p["closed_at"])
        state = "merged" if merged else ("draft" if p["draft"] and p["state"] == "open" else p["state"])
        pr = g.rec("PullRequest", "github", p["id"], {"number": p["number"], "title": p["title"], "state": state,
                   "is_draft": p["draft"], "created_at": created, "merged_at": merged, "closed_at": closed,
                   "base_ref": p["base"]["ref"], "head_ref": p["head"]["ref"]}, scope, acl_from=repo)
        prs[p["number"]] = pr
        g.link("IN", pr, repo, "field", valid_from=created)
        g.link("BASE", pr, branch(p["base"]["ref"]), "field", valid_from=created)
        g.link("HEAD", pr, branch(p["head"]["label"]), "field", valid_from=created)  # owner:branch, so forks stay apart
        g.link("AUTHORED_BY", pr, account(p["user"]), "field", role="author", at=created)
        if p.get("merged_by"):
            g.link("MERGED_BY", pr, account(p["merged_by"]), "field", at=merged)
        if p.get("body"):
            cv = g.rec("ContentVersion", "github", f"{p['id']}:body", {"version": "current", "valid_from": created,
                       "text": p["body"], "note": "description"}, scope, acl_from=pr)
            g.link("VERSION_OF", cv, pr, "field", kind="revision", order=1)

        # references: ticket keys and KIPs named in the title
        for k in sorted(set(key_rx.findall(p["title"]))):
            g.ref(pr, "IMPLEMENTS", "Ticket", k, "convention", 0.9, stub=True, valid_from=created, link_method="pr_title",
                  closes_on_merge=False)
        # a tracker that names branches after the issue (Linear's branchName) links the PR by its head branch
        if g.ctx.get("ticket_by_branch"):
            g.ref(pr, "IMPLEMENTS", "TicketByBranch", p["head"]["ref"], "convention", 0.9, valid_from=created,
                  link_method="branch_name", closes_on_merge=True)
        for n in set(KIP.findall(p["title"])):
            g.ref(pr, "ENACTS", "Decision", int(n), "convention", 0.9, valid_from=created)

        # files changed
        for fch in x["files"]:
            fpath = fch["filename"]
            if fpath not in files:
                files[fpath] = g.rec("File", "git", f"{repo_json['id']}:{fpath}", file_props(fpath), scope, acl_from=repo)
                g.link("IN", files[fpath], repo, "field")
            g.link("CHANGES", pr, files[fpath], "field", at=created, change_type=CHANGE_TYPE.get(fch["status"], "modified"),
                   additions=fch.get("additions"), deletions=fch.get("deletions"), previous_filename=fch.get("previous_filename"))

        # timeline: labels (dated), review requests (dated)
        timeline = x["timeline"]
        _labels(g, pr, timeline, p, label, created)
        _review_requests(g, pr, timeline, x["reviews"], account, merged or closed)

        # reviews and approvals
        commit_times = sorted(utc(c["commit"]["committer"]["date"]) for c in x["commits"])
        for rv in x["reviews"]:
            if not rv.get("submitted_at"):
                continue
            at = utc(rv["submitted_at"])
            r = g.rec("Review", "github", rv["id"], {"state": rv["state"].lower(), "at": at, "text": rv.get("body") or None},
                      scope, acl_from=pr)
            g.link("ON", r, pr, "field", at=at, at_commit=rv["commit_id"])
            g.link("AUTHORED_BY", r, account(rv["user"]), "field", role="author", at=at)
            if rv["state"] == "APPROVED":
                stale = any(ct > at for ct in commit_times)
                g.link("APPROVED_BY", pr, account(rv["user"]), "inferred", evidence=[r], derived=True,
                       rule_id="derive:approved_by_from_review", at=at, at_commit=rv["commit_id"], review_id=str(rv["id"]),
                       stale=stale, stale_reason="new_commits" if stale else None)
                g.notes["github: approvals made stale by a later commit"] += stale

        # review threads and comments
        threads = {}
        for c in x["review_comments"]:
            root = c.get("in_reply_to_id") or c["id"]
            at = utc(c["created_at"])
            if root not in threads:
                threads[root] = g.rec("Thread", "github", f"review:{root}", {"kind": "review", "path": c["path"],
                                      "line": c.get("line"), "created_at": at}, scope, acl_from=pr)
                g.link("ON", threads[root], pr, "field", at=at)
            cm = g.rec("Comment", "github", c["id"], {"text": c["body"], "kind": "review", "at": at, "path": c["path"],
                       "line": c.get("line"), "side": c.get("side"), "commit_id": c["commit_id"],
                       "original_commit_id": c.get("original_commit_id")}, scope, acl_from=pr)
            g.link("IN", cm, threads[root], "field", valid_from=at)
            g.link("AUTHORED_BY", cm, account(c["user"]), "field", role="author", at=at)
            if fp := files.get(c["path"]):
                g.link("ON", cm, fp, "field", at=at, line=c.get("line"), side=c.get("side"), at_commit=c["commit_id"])
        for c in x["issue_comments"]:
            at = utc(c["created_at"])
            cm = g.rec("Comment", "github", c["id"], {"text": c["body"], "kind": "conversation", "at": at}, scope, acl_from=pr)
            g.link("ON", cm, pr, "field", at=at)
            g.link("AUTHORED_BY", cm, account(c["user"]), "field", role="author", at=at)
            if c["user"]["type"] == "Bot":
                g.notes["github: bot comments (CI notices, kept but marked by the bot author)"] += 1
        # the PR's own commits (head commits, mostly not on trunk): referenced by sha for the linker
        for pos, c in enumerate(x["commits"]):
            g.ref(pr, "CONTAINS", "Commit", c["sha"], "field", position=pos, valid_from=created)

    _ci(g, d, repo, scope, account)
    _tags(g, d, repo, scope)


def _labels(g, pr, timeline, p, label, created):
    open_at = {}
    for e in timeline:
        if e.get("event") == "labeled":
            open_at[e["label"]["name"]] = utc(e["created_at"])
        elif e.get("event") == "unlabeled" and e["label"]["name"] in open_at:
            start, end = open_at.pop(e["label"]["name"]), utc(e["created_at"])
            if end > start:
                g.link("HAS_LABEL", pr, label(e["label"]["name"]), "event", valid_from=start, valid_to=end)
    for lb in p.get("labels") or []:
        g.link("HAS_LABEL", pr, label(lb["name"]), "event" if lb["name"] in open_at else "snapshot",
               valid_from=open_at.get(lb["name"]))


def _review_requests(g, pr, timeline, reviews, account, ended):
    first_review = {}
    for rv in reviews:
        if rv.get("submitted_at") and rv["user"]:
            first_review.setdefault(rv["user"]["id"], utc(rv["submitted_at"]))
    for e in timeline:
        if e.get("event") == "review_requested" and e.get("requested_reviewer"):
            who, at = e["requested_reviewer"], utc(e["created_at"])
            end = first_review.get(who["id"]) or ended
            g.link("REVIEW_REQUESTED", pr, account(who), "event", valid_from=at, valid_to=end if end and end > at else None,
                   requested_at=at, requested_by=(e.get("actor") or {}).get("login"))


def _ci(g, d, repo, scope, account):
    if not (d / "github/workflows.json").exists():
        return
    pipelines = {}
    for w in J(d / "github/workflows.json").get("workflows", []):
        pipelines[w["id"]] = g.rec("Pipeline", "github", w["id"], {"name": w["name"], "path": w["path"], "state": w["state"]},
                                   scope, acl_from=repo)
    for r in J(d / "github/workflow_runs.json") if (d / "github/workflow_runs.json").exists() else []:
        run = g.rec("PipelineRun", "github", r["id"], {"name": r["name"], "event": r["event"], "conclusion": r["conclusion"],
                    "started_at": utc(r.get("run_started_at") or r["created_at"]), "head_sha": r["head_sha"],
                    "attempt": r.get("run_attempt")}, scope, acl_from=repo)
        if r["workflow_id"] in pipelines:
            g.link("INSTANCE_OF", run, pipelines[r["workflow_id"]], "field")
        if r.get("actor"):
            g.link("ACTED_BY", run, account(r["actor"]), "field", role="initiator", action="start",
                   at=utc(r.get("run_started_at") or r["created_at"]))
        g.ref(run, "POINTS_TO", "Commit", r["head_sha"], "field")
    g.notes["github: CI runs without job detail (no Check records yet)"] = len(J(d / "github/workflow_runs.json")) if (d / "github/workflow_runs.json").exists() else 0


def _tags(g, d, repo, scope):
    if not (d / "github/tags.json").exists():
        return
    for t in J(d / "github/tags.json"):
        tag = g.rec("Tag", "github", f"{repo['props']['native_id']}:{t['name']}", {"name": t["name"]}, scope, acl_from=repo)
        g.ref(tag, "POINTS_TO", "Commit", t["commit"]["sha"], "field", role="tag")
