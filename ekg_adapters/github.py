"""Code pillar, GitHub side: repository, pull requests, reviews, review threads, comments, branches,
labels (dated from the timeline), review requests, changed files, CI workflows and runs, tags.

APPROVED_BY is derived from the reviews (derived, with a rule id) and marked stale
when a commit landed after the approval. The owner organisation is a Workspace the Repository is IN; an app the
source names (performed_via_github_app) is an App, and a bot account is BOT_OF it (the bot posted through it, or
its login is the app's "<slug>[bot]"); every actor the timeline names is linked by what they did (ACTED_BY,
ASSIGNED_TO, SUBSCRIBED, MENTIONS); a commit's committer account is AUTHORED_BY role committer. References queued for the linker: ticket
keys and decision keys (settings) in PR titles, ticket keys in branch names, and "Fixes #n" in descriptions.
"""

import re
from pathlib import Path

from .access import place
from .common import Graph, J, file_props, repo_tags, ticket_rx, utc

CLOSES = re.compile(r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.I)
CHANGE_TYPE = {"added": "added", "removed": "removed", "modified": "modified", "renamed": "renamed",
               "copied": "copied", "changed": "modified", "unchanged": "modified"}


def map_github(g: Graph, d: Path, repo_name: str):
    key_rx = ticket_rx(g)
    prefix = g.ctx["settings"].get("decision_prefix")
    decision_rx = re.compile(rf"\b{re.escape(prefix)}-(\d+)\b") if prefix else None
    repo_json = J(d / "github/repo.json")
    public = repo_json.get("visibility", "public") == "public" and not repo_json.get("private")
    scope, access = place(g, "github", "repo", repo_name, public=public)
    repo = g.rec("Repository", "github", repo_json["id"], {"name": repo_json["full_name"], "visibility": repo_json.get("visibility"),
                 "archived": repo_json.get("archived"), "is_fork": repo_json.get("fork"),
                 "default_branch": repo_json.get("default_branch")}, scope)
    g.link("GOVERNED_BY", repo, access, "snapshot")
    g.ctx["repo"] = repo
    # the organisation that owns the repository (the code host's owner object says whether it is one): a Workspace
    owner = repo_json.get("owner") or {}
    if owner.get("type") == "Organization" and owner.get("id"):
        ws = g.rec("Workspace", "github", owner["id"], {"name": owner.get("login"), "handle": owner.get("login"),
                   "kind": "code"}, scope)
        g.link("IN", repo, ws, "field", valid_from=utc(repo_json.get("created_at")))
        g.ctx["github_workspace"] = ws
    elif owner:
        g.notes["github: repository owned by a user, not an organisation (no Workspace)"] += 1

    accounts = g.ctx.setdefault("github_accounts", {})
    apps, app_slugs, logins = {}, {}, {}  # app id -> App; app slug -> App; account id -> every login it was seen under

    def account(u):
        if not u or not u.get("id"):
            return None
        if u["id"] not in accounts:
            accounts[u["id"]] = g.rec("Account", "github", u["id"], {"login": u["login"], "status": "active",
                                      "is_bot": u.get("type") == "Bot" if u.get("type") else None, "tool": "github"},
                                      scope)
        acct = accounts[u["id"]]
        if u.get("type") and acct["props"].get("is_bot") is None:  # first made from a noreply address: now the type is known
            acct["props"]["is_bot"] = u["type"] == "Bot"
        if u.get("login"):
            logins.setdefault(u["id"], set()).add(u["login"])
        if u.get("login") and u["login"] != acct["props"].get("login"):
            # the same account under a second login (a bot shown by its app's name in one API, by its own in another)
            al = g.rec("Alias", "github", f"login:{u['login'].casefold()}", {"value": u["login"], "kind": "login",
                       "namespace": "github", "normalized_value": u["login"].casefold()}, scope)
            g.link("ALIAS_OF", al, acct, "field")
        return acct

    def acted_via_app(item_user, app):
        """An item a GitHub App posted (the source's performed_via_github_app): the App, and, when the item's author
        is a bot account, that account is the app's bot (BOT_OF)."""
        if not app or not app.get("id"):
            return None
        if app["id"] not in apps:
            apps[app["id"]] = g.rec("App", "github", f"app:{app['id']}", {"name": app.get("name") or app.get("slug")}, scope)
            g.link("IN", apps[app["id"]], repo, "field")
            if app.get("slug"):
                app_slugs[app["slug"].casefold()] = apps[app["id"]]
        if item_user and item_user.get("type") == "Bot":
            g.link("BOT_OF", account(item_user), apps[app["id"]], "field")
        return apps[app["id"]]

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

    # every label the repository defines, not only those a downloaded PR carries
    for lb in J(d / "github/labels.json") if (d / "github/labels.json").exists() else []:
        if lb.get("name"):
            label(lb["name"])
    g.ctx["code_labels"] = label  # the code tree's labeler config names labels by name (code.py)
    milestones = {}
    for ms in J(d / "github/milestones.json") if (d / "github/milestones.json").exists() else []:
        milestones[ms["number"]] = g.rec("Milestone", "github", ms["id"], {"name": ms.get("title"),
                                         "created_at": utc(ms.get("created_at")), "target_date": utc(ms.get("due_on")),
                                         "number": ms.get("number"), "state": ms.get("state"),
                                         "closed_at": utc(ms.get("closed_at"))},
                                         scope, acl_from=repo)
        g.link("IN", milestones[ms["number"]], repo, "field", valid_from=utc(ms.get("created_at")))
        if ms.get("creator"):
            g.link("CREATED_BY", milestones[ms["number"]], account(ms["creator"]), "field", role="creator",
                   at=utc(ms.get("created_at")))
    commits = g.ctx.setdefault("commits", {})
    prs = g.ctx.setdefault("prs", {})
    for path in sorted((d / "github/pulls").glob("pr-*.json")):
        x = J(path)
        p = x["pull"]
        # a PR known only from a search hit ("_partial"): no branches, files, reviews or commits
        if x.get("_partial"):
            g.notes[f"github: PRs known only from a {x['_partial']} hit (no branches, files or reviews)"] += 1
        base, head = p.get("base") or {}, p.get("head") or {}
        created, merged, closed = utc(p["created_at"]), utc(p.get("merged_at")), utc(p.get("closed_at"))
        draft = bool(p.get("draft"))
        state = "merged" if merged else ("draft" if draft and p["state"] == "open" else p["state"])
        pr = g.rec("PullRequest", "github", p["id"], {"number": p["number"], "title": p["title"], "state": state,
                   "is_draft": draft, "created_at": created, "merged_at": merged, "closed_at": closed,
                   "base_ref": base.get("ref"), "head_ref": head.get("ref")}, scope, acl_from=repo)
        prs[p["number"]] = pr
        g.link("IN", pr, repo, "field", valid_from=created)
        if (p.get("milestone") or {}).get("number") in milestones:
            g.link("IN", pr, milestones[p["milestone"]["number"]], "field", valid_from=created)
        if base.get("ref"):
            g.link("BASE", pr, branch(base["ref"]), "field", valid_from=created)
        if head.get("label"):
            g.link("HEAD", pr, branch(head["label"]), "field", valid_from=created)  # owner:branch, so forks stay apart
        g.link("AUTHORED_BY", pr, account(p["user"]), "field", role="author", at=created)
        if p.get("merged_by"):
            g.link("MERGED_BY", pr, account(p["merged_by"]), "field", at=merged)
        if p.get("body"):
            cv = g.rec("ContentVersion", "github", f"{p['id']}:body", {"version": "current", "valid_from": created,
                       "text": p["body"], "note": "description"}, scope, acl_from=pr)
            g.link("VERSION_OF", cv, pr, "field", kind="revision", order=1)

        # references: ticket keys and decision keys named in the title, and ticket keys in the branch name
        title_keys = set(key_rx.findall(p["title"]))
        for k in sorted(title_keys):
            g.ref(pr, "IMPLEMENTS", "Ticket", k, "convention", 0.9, stub=True, valid_from=created, link_method="pr_title",
                  closes_on_merge=False)
        for k in sorted(set(key_rx.findall((head.get("ref") or "").upper().replace("_", "-"))) - title_keys):
            g.ref(pr, "IMPLEMENTS", "Ticket", k, "convention", 0.85, valid_from=created, link_method="branch_name",
                  closes_on_merge=False)
        # a tracker that names branches after the issue (Linear's branchName) links the PR by its head branch
        if g.ctx.get("ticket_by_branch") and head.get("ref"):
            g.ref(pr, "IMPLEMENTS", "TicketByBranch", head["ref"], "convention", 0.9, valid_from=created,
                  link_method="branch_name", closes_on_merge=True)
        if decision_rx:
            for n in set(decision_rx.findall(p["title"])):
                g.ref(pr, "ENACTS", "Decision", int(n), "convention", 0.9, valid_from=created)
        if g.ctx["settings"].get("issue_tracker") == "github":  # "Fixes #12" in the description closes that issue
            for n in sorted({int(x) for _, x in CLOSES.findall(p.get("body") or "")} - {p["number"]}):
                g.ref(pr, "IMPLEMENTS", "GitHubIssue", n, "convention", 0.95, valid_from=created,
                      link_method="closing_keyword", magic_word=True, closes_on_merge=True)

        # files changed
        for fch in x.get("files") or []:
            fpath = fch["filename"]
            if fpath not in files:
                files[fpath] = g.rec("File", "git", f"{repo_json['id']}:{fpath}", file_props(fpath), scope, acl_from=repo)
                g.link("IN", files[fpath], repo, "field")
            g.link("CHANGES", pr, files[fpath], "field", at=created, change_type=CHANGE_TYPE.get(fch["status"], "modified"),
                   additions=fch.get("additions"), deletions=fch.get("deletions"), previous_filename=fch.get("previous_filename"))

        # timeline: labels (dated), review requests (dated)
        timeline = x.get("timeline") or []
        _labels(g, pr, timeline, p, label, created)
        _review_requests(g, pr, timeline, x.get("reviews") or [], account, merged or closed)

        # reviews and approvals
        commit_times = sorted(utc(c["commit"]["committer"]["date"]) for c in x.get("commits") or [])
        for rv in x.get("reviews") or []:
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
        for c in x.get("review_comments") or []:
            root = c.get("in_reply_to_id") or c["id"]
            at = utc(c["created_at"])
            if root not in threads:
                threads[root] = g.rec("Thread", "github", f"review:{root}", {"kind": "review", "path": c["path"],
                                      "line": c.get("line"), "created_at": at}, scope, acl_from=pr)
                g.link("ON", threads[root], pr, "field", at=at)
            cm = g.rec("Comment", "github", c["id"], {"text": c["body"], "kind": "review", "at": at, "path": c["path"],
                       "line": c.get("line"), "side": c.get("side"), "commit_id": c["commit_id"],
                       "original_commit_id": c.get("original_commit_id"), **_reactions(c)}, scope, acl_from=pr)
            g.link("IN", cm, threads[root], "field", valid_from=at)
            g.link("ON", cm, pr, "field", at=at)  # a review comment is a comment on the PR too
            g.link("AUTHORED_BY", cm, account(c["user"]), "field", role="author", at=at)
            if fp := files.get(c["path"]):
                g.link("ON", cm, fp, "field", at=at, line=c.get("line"), side=c.get("side"), at_commit=c["commit_id"])
        for c in x.get("issue_comments") or []:
            at = utc(c["created_at"])
            cm = g.rec("Comment", "github", c["id"], {"text": c["body"], "kind": "conversation", "at": at, **_reactions(c)},
                       scope, acl_from=pr)
            g.link("ON", cm, pr, "field", at=at)
            g.link("AUTHORED_BY", cm, account(c["user"]), "field", role="author", at=at)
            if c["user"]["type"] == "Bot":
                g.notes["github: bot comments (CI notices, kept but marked by the bot author)"] += 1
            acted_via_app(c["user"], c.get("performed_via_github_app"))
        # the PR's own commits (head commits, mostly not on the default branch): referenced by sha for the linker
        for pos, c in enumerate(x.get("commits") or []):
            # the PR's own commits (mostly on its branch, not on the default branch): records of their own, so the PR's commit
            # count and history are in the graph; a default-branch commit with the same sha is the same record
            inner = c.get("commit") or {}
            if c["sha"] not in commits:
                commits[c["sha"]] = g.rec("Commit", "git", c["sha"], {
                    "sha": c["sha"], "message": inner.get("message"),
                    "authored_at": utc((inner.get("author") or {}).get("date")),
                    "committed_at": utc((inner.get("committer") or {}).get("date"))}, scope, acl_from=repo)
                if c.get("author"):
                    g.link("AUTHORED_BY", commits[c["sha"]], account(c["author"]), "field", role="author",
                           at=utc((inner.get("author") or {}).get("date")))
                if c.get("committer"):  # who recorded the commit, as the code host names its account
                    g.link("AUTHORED_BY", commits[c["sha"]], account(c["committer"]), "field", role="committer",
                           at=utc((inner.get("committer") or {}).get("date")))
            g.ref(pr, "CONTAINS", "Commit", c["sha"], "field", position=pos, valid_from=created)
        dated = _actors(g, pr, timeline, account, acted_via_app)
        for u in p.get("assignees") or ([p["assignee"]] if p.get("assignee") else []):
            if u and u.get("id") not in dated:  # assigned now, with no assignment event in the timeline
                g.link("ASSIGNED_TO", pr, account(u), "snapshot")

    _ci(g, d, repo, scope, account)
    _tags(g, d, repo, scope)
    # the code host names an app's bot user "<app slug>[bot]": a bot account seen under that login is the app's bot,
    # even where no item it posted names the app
    for uid, names in logins.items():
        acct = accounts.get(uid)
        if not acct or not acct["props"].get("is_bot"):
            continue
        for name in sorted(names):
            app = app_slugs.get(name.casefold().removesuffix("[bot]")) if name.casefold().endswith("[bot]") else None
            if app is not None:
                g.link("BOT_OF", acct, app, "convention", 0.95)


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


# timeline events whose actor acted on the pull request itself -> ACTED_BY's action (the event name is the link's kind)
ACTS = {"closed": "close", "reopened": "modify", "labeled": "triage", "unlabeled": "triage", "renamed": "modify",
        "ready_for_review": "modify", "convert_to_draft": "modify", "base_ref_changed": "modify",
        "head_ref_deleted": "modify", "head_ref_force_pushed": "push", "review_dismissed": "dismiss",
        "copilot_work_started": "start"}


def _actors(g, pr, timeline, account, acted_via_app):
    """Everyone the timeline names, linked by what they did: an act on the PR (ACTED_BY, kind = the event), an
    assignment (ASSIGNED_TO, dated), a subscription (SUBSCRIBED, dated) and a mention (MENTIONS). Commits, comments,
    reviews, review requests and the merge are linked from their own records, not again here."""
    assigned, subscribed = {}, {}
    for e in timeline:
        ev, at, who = e.get("event"), utc(e.get("created_at")), e.get("actor")
        acted_via_app(e.get("actor") or e.get("user"), e.get("performed_via_github_app"))
        if ev in ACTS and who:
            g.link("ACTED_BY", pr, account(who), "event", role="closer" if ev == "closed" else "actor", action=ACTS[ev],
                   kind=ev, at=at, forced=True if ev == "head_ref_force_pushed" else None)
        elif ev == "assigned" and e.get("assignee"):
            assigned[e["assignee"]["id"]] = (e["assignee"], at)
        elif ev == "unassigned" and e.get("assignee") and e["assignee"]["id"] in assigned:
            u, since = assigned.pop(e["assignee"]["id"])
            if at and since and at > since:
                g.link("ASSIGNED_TO", pr, account(u), "event", valid_from=since, valid_to=at)
        elif ev == "subscribed" and who:
            subscribed[who["id"]] = (who, at)
        elif ev == "unsubscribed" and who and who["id"] in subscribed:
            u, since = subscribed.pop(who["id"])
            if at and since and at > since:
                g.link("SUBSCRIBED", account(u), pr, "event", valid_from=since, valid_to=at)
        elif ev == "mentioned" and who:
            g.link("MENTIONS", pr, account(who), "event", at=at, kind="mention")
    for u, since in assigned.values():
        g.link("ASSIGNED_TO", pr, account(u), "event", valid_from=since)
    for u, since in subscribed.values():
        g.link("SUBSCRIBED", account(u), pr, "event", valid_from=since)
    return {u["id"] for u, _ in assigned.values()}


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
    runs = {}
    for r in J(d / "github/workflow_runs.json") if (d / "github/workflow_runs.json").exists() else []:
        run = runs[r["id"]] = g.rec("PipelineRun", "github", r["id"], {"name": r["name"], "event": r["event"], "conclusion": r["conclusion"],
                    "started_at": utc(r.get("run_started_at") or r["created_at"]), "head_sha": r["head_sha"],
                    "attempt": r.get("run_attempt"), "run_number": r.get("run_number"),
                    "head_branch": r.get("head_branch"), "previous_attempt_url": r.get("previous_attempt_url")},
                    scope, acl_from=repo)
        if r["workflow_id"] in pipelines:
            g.link("INSTANCE_OF", run, pipelines[r["workflow_id"]], "field")
        if r.get("actor"):
            g.link("ACTED_BY", run, account(r["actor"]), "field", role="initiator", action="start",
                   at=utc(r.get("run_started_at") or r["created_at"]))
        trig = r.get("triggering_actor")
        if trig and trig.get("id") != (r.get("actor") or {}).get("id"):  # a re-run started by someone else
            g.link("ACTED_BY", run, account(trig), "field", role="actor", action="rerun", kind="rerun",
                   at=utc(r.get("run_started_at") or r["created_at"]), attempt=r.get("run_attempt"))
        g.ref(run, "POINTS_TO", "Commit", r["head_sha"], "field")
    with_jobs = 0
    for f in sorted((d / "github/run_jobs").glob("*.json")) if (d / "github/run_jobs").is_dir() else []:
        jobs = J(f).get("jobs") or []
        if not jobs:
            continue
        with_jobs += 1
        run = runs.get(jobs[0]["run_id"])
        if run is None:  # a run outside the downloaded run list: what its jobs say about it
            j0 = jobs[0]
            run = runs[j0["run_id"]] = g.rec("PipelineRun", "github", j0["run_id"], {
                "name": j0.get("workflow_name"), "started_at": utc(j0.get("started_at") or j0.get("created_at")),
                "head_sha": j0.get("head_sha"), "attempt": j0.get("run_attempt")}, scope, acl_from=repo)
            g.ref(run, "POINTS_TO", "Commit", j0["head_sha"], "field")
        for j in jobs:
            failed = [st["name"] for st in j.get("steps") or [] if st.get("conclusion") == "failure"]
            chk = g.rec("Check", "github", j["id"], {
                "name": j.get("name"), "kind": "job", "conclusion": j.get("conclusion"),
                "started_at": utc(j.get("started_at")), "completed_at": utc(j.get("completed_at")),
                "head_sha": j.get("head_sha"), "attempt": j.get("run_attempt"), "runner": j.get("runner_name"),
                "summary": ("failed steps: " + ", ".join(failed)) if failed else None}, scope, acl_from=run)
            g.link("IN", chk, run, "field", valid_from=utc(j.get("started_at") or j.get("created_at")))
            g.notes["github: CI jobs (Check records)"] += 1
    runs_total = len(J(d / "github/workflow_runs.json")) if (d / "github/workflow_runs.json").exists() else 0
    g.notes["github: CI runs without job detail"] = max(0, runs_total - with_jobs)


def _tags(g, d, repo, scope):
    if not (d / "github/tags.json").exists():
        return
    local = repo_tags(d)  # dates and the commit a tag really points to, from repo.git when it is there
    g.ctx["tag_targets"] = {v["sha"] for v in local.values() if v.get("sha")}
    for t in J(d / "github/tags.json"):
        tag = g.rec("Tag", "github", f"{repo['props']['native_id']}:{t['name']}", {"name": t["name"]}, scope, acl_from=repo)
        info = local.get(t["name"]) or {}
        g.ref(tag, "POINTS_TO", "Commit", info.get("sha") or t["commit"]["sha"], "field", role="tag",
              valid_from=utc(info.get("date")))


def _reactions(c) -> dict:
    """A comment's reaction totals (the code host gives counts per emoji, not who reacted)."""
    r = c.get("reactions") or {}
    per = {k: v for k, v in r.items() if k not in ("url", "total_count") and isinstance(v, int) and v}
    if "total_count" not in r:
        return {}
    return {"reaction_count": r["total_count"], "reactions": per or None}
