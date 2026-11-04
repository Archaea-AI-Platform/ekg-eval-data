"""Tickets pillar: Linear (GraphQL API, as written by fetch_linear.py) -> Workspace, Account, Group (team),
TicketState, Label, Project, StatusUpdate, Milestone, Iteration (cycle), Ticket with dated history,
Comment, Attachment, Document.

Input layout (linear/):
  organization.json   {id, name, urlKey}
  users.json          [User]                          id, name, displayName, email, active, admin, guest, createdAt
  teams.json          [Team + states, labels, members, cycles]
  projects.json       [Project + status, lead, teams, members, projectUpdates, projectMilestones]
  issues/page-*.json  [Issue + labels, history, comments, attachments, relations, subscribers]
  documents.json      [Document]                      optional

Access: a private team's scope is granted to its members only; a public team's to every
non-guest member of the workspace plus the team (guests only see teams they are in).
Cross-tool references queued for the linker: GitHub PR attachments (Linear's GitHub
integration), Slack message attachments (an issue created from or synced with Slack),
the issue's branch name (matched to a PR's head branch), issue keys in descriptions
and comments.
"""

import datetime as dt
import re
from pathlib import Path

from .access import grant, place
from .common import Graph, J, day, ticket_rx, utc

PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
SLACK_MSG_URL = re.compile(r"https://[\w.-]+\.slack\.com/archives/(\w+)/p(\d{10})(\d{6})(?:\?thread_ts=(\d+\.\d+))?")
RELATION = {"blocks": ("BLOCKS", "blocks"), "duplicate": ("DUPLICATE_OF", None), "related": ("RELATED_TO", "related"),
            "similar": ("RELATED_TO", "similar")}


def nodes(conn):
    """A GraphQL connection ({nodes: [...]}) or a plain list -> list."""
    if conn is None:
        return []
    return conn.get("nodes", []) if isinstance(conn, dict) else conn


def map_linear(g: Graph, d: Path):
    d = d / "linear"
    org = J(d / "organization.json")
    ws_scope, ws_access = place(g, "linear", "workspace", org["id"], public=False)
    ws = g.rec("Workspace", "linear", org["id"], {"name": org["name"], "handle": org.get("urlKey"), "kind": "tracker"}, ws_scope)
    members_group = g.rec("Group", "linear", f"{org['id']}:members", {"name": f"{org['name']} members",
                          "kind": "workspace_members", "tool": "linear"}, ws_scope)
    grant(g, members_group, ws_access, via="base_permission")

    # ---- users
    accounts = g.ctx.setdefault("linear_accounts", {})
    for u in J(d / "users.json"):
        acct = g.rec("Account", "linear", u["id"], {"login": u.get("displayName"), "display_name": u.get("name"),
                     "emails": [u["email"].lower()] if u.get("email") else None,
                     "status": "active" if u.get("active", True) else "deactivated", "is_bot": False,
                     "is_guest": bool(u.get("guest")), "tool": "linear", "workspace_id": org["id"]}, ws_scope)
        accounts[u["id"]] = acct
        g.link("MEMBER_OF", acct, ws, "snapshot", role="guest" if u.get("guest") else ("owner" if u.get("admin") else "member"),
               basis="snapshot", valid_from=utc(u.get("createdAt")))
        if not u.get("guest") and u.get("active", True):
            g.link("MEMBER_OF", acct, members_group, "snapshot", role="member", basis="snapshot", valid_from=utc(u.get("createdAt")))

    acct = lambda ref: accounts.get((ref or {}).get("id") if isinstance(ref, dict) else ref)

    # ---- teams, states, labels, cycles
    teams, team_scope, states, labels, cycles = {}, {}, {}, {}, {}
    prefixes = g.ctx.setdefault("ticket_prefixes", set())
    tool_by_prefix = g.ctx.setdefault("ticket_tool_by_prefix", {})
    for t in J(d / "teams.json"):
        prefixes.add(t["key"])
        tool_by_prefix[t["key"]] = "linear"
        scope, access = place(g, "linear", "tracker_restricted_team" if t.get("private") else "tracker_team", t["id"], public=False)
        team_scope[t["id"]] = scope
        team = g.rec("Group", "linear", t["id"], {"name": t["name"], "handle": t["key"], "kind": "tracker_team",
                     "cycles_enabled": t.get("cyclesEnabled"), "triage_enabled": t.get("triageEnabled"),
                     "cycle_length_weeks": t.get("cycleDuration"), "visibility": "private" if t.get("private") else "public",
                     "tool": "linear", "formed_at": utc(t.get("createdAt"))}, scope)
        teams[t["id"]] = team
        grant(g, team, access, via="team")
        if not t.get("private"):
            grant(g, members_group, access, via="base_permission")
        for m in nodes(t.get("members")):
            if acct(m):
                g.link("MEMBER_OF", acct(m), team, "snapshot", role="member", basis="snapshot")
        for s in nodes(t.get("states")):
            states[s["id"]] = g.rec("TicketState", "linear", s["id"], {"name": s["name"], "type": s["type"]}, scope)
            g.link("IN", states[s["id"]], team, "field")
        for lb in nodes(t.get("labels")):
            labels[lb["id"]] = g.rec("Label", "linear", lb["id"], {"name": lb["name"], "kind": "ticket",
                                     "is_group": lb.get("isGroup")}, scope)
        for lb in nodes(t.get("labels")):
            if (lb.get("parent") or {}).get("id") in labels:
                g.link("CHILD_OF", labels[lb["id"]], labels[lb["parent"]["id"]], "field")
        for cy in nodes(t.get("cycles")):
            cycles[cy["id"]] = g.rec("Iteration", "linear", cy["id"], {"name": cy.get("name") or f"Cycle {cy['number']}",
                                     "number": cy.get("number"), "starts_at": utc(cy.get("startsAt")),
                                     "ends_at": utc(cy.get("endsAt")), "completed_at": utc(cy.get("completedAt"))}, scope)
            g.link("IN", cycles[cy["id"]], team, "field", valid_from=utc(cy.get("startsAt")))

    # ---- projects, updates, milestones
    projects, milestones = {}, {}
    for p in J(d / "projects.json") if (d / "projects.json").exists() else []:
        tags = [tag for tm in nodes(p.get("teams")) for tag in team_scope.get(tm["id"], [[]])[0]]
        scope = [tags] if tags else ws_scope  # visible through any of its teams
        status = (p.get("status") or {}).get("type") or p.get("state")
        proj = g.rec("Project", "linear", p["id"], {"name": p["name"], "kind": "project", "status": status,
                     "health": p.get("health"), "start_date": day(p.get("startDate")), "target_date": day(p.get("targetDate")),
                     "text": p.get("description") or None}, scope)
        projects[p["id"]] = proj
        if acct(p.get("lead")):
            g.link("LED_BY", proj, acct(p["lead"]), "snapshot", role="lead")
        for up in nodes(p.get("projectUpdates")):
            at = utc(up["createdAt"])
            su = g.rec("StatusUpdate", "linear", up["id"], {"at": at, "health": up.get("health"), "text": up.get("body")},
                       scope, acl_from=proj)
            g.link("ON", su, proj, "field", at=at)
            if acct(up.get("user")):
                g.link("AUTHORED_BY", su, acct(up["user"]), "field", role="author", at=at)
        for ms in nodes(p.get("projectMilestones")):
            milestones[ms["id"]] = g.rec("Milestone", "linear", ms["id"], {"name": ms["name"], "target_date": day(ms.get("targetDate")),
                                         "created_at": utc(ms.get("createdAt"))}, scope, acl_from=proj)
            g.link("IN", milestones[ms["id"]], proj, "field")

    # ---- issues
    tickets, by_branch = g.ctx.setdefault("tickets", {}), g.ctx.setdefault("ticket_by_branch", {})
    issues = [i for p in sorted((d / "issues").glob("page-*.json")) for i in J(p)]
    key_rx = ticket_rx(g)
    for i in issues:
        scope = team_scope.get(i["team"]["id"], ws_scope)
        created = utc(i["createdAt"])
        t = g.rec("Ticket", "linear", i["id"], {
            "identifier": i["identifier"], "title": i["title"], "kind": "sub_issue" if i.get("parent") else "issue",
            "priority": i.get("priorityLabel"), "estimate": i.get("estimate"), "created_at": created,
            "started_at": utc(i.get("startedAt")), "completed_at": utc(i.get("completedAt")),
            "canceled_at": utc(i.get("canceledAt")), "due_at": day(i.get("dueDate")),
            "due_at_precision": "day" if i.get("dueDate") else None, "deleted_at": utc(i.get("archivedAt")) if i.get("trashed") else None,
            "branch_name": i.get("branchName")}, scope)
        tickets[i["identifier"]] = t
        if i.get("branchName"):
            by_branch[i["branchName"]] = t
        g.link("IN", t, teams[i["team"]["id"]], "field", valid_from=created)
        if acct(i.get("creator")):
            g.link("CREATED_BY", t, acct(i["creator"]), "field", role="creator", at=created)
        for ref, recs in (("project", projects), ("cycle", cycles), ("projectMilestone", milestones)):
            if (i.get(ref) or {}).get("id") in recs:
                g.link("IN", t, recs[i[ref]["id"]], "field")
        if i.get("parent"):
            g.ref(t, "CHILD_OF", "Ticket", i["parent"]["identifier"], "field", stub=True)
        for s in nodes(i.get("subscribers")):
            if acct(s):
                g.link("SUBSCRIBED", acct(s), t, "snapshot")
        if i.get("description"):
            cv = g.rec("ContentVersion", "linear", f"{i['id']}:description", {"version": "current",
                       "valid_from": utc(i.get("updatedAt")) or created, "text": i["description"], "note": "description"},
                       scope, acl_from=t)
            g.link("VERSION_OF", cv, t, "field", kind="revision", order=1)
        _history(g, t, i, states, labels, acct, scope)
        _comments(g, t, i, acct, scope, key_rx)
        _attachments(g, t, i, scope)
        for rel in nodes(i.get("relations")):
            ltype, kind = RELATION.get(rel["type"], ("RELATED_TO", rel["type"]))
            g.ref(t, ltype, "Ticket", rel["relatedIssue"]["identifier"], "field", stub=True, kind=kind,
                  valid_from=utc(rel.get("createdAt")))
        for k in set(key_rx.findall(i.get("description") or "")) - {i["identifier"]}:
            g.ref(t, "MENTIONS", "Ticket", k, "text_rule", 0.8, kind="key", at=created)

    # ---- documents
    for doc in J(d / "documents.json") if (d / "documents.json").exists() else []:
        proj = projects.get((doc.get("project") or {}).get("id"))
        scope = proj["scope"] if proj else ws_scope
        dr = g.rec("Document", "linear", doc["id"], {"title": doc["title"], "kind": "tracker_doc",
                   "created_at": utc(doc["createdAt"])}, scope)
        if proj:
            g.link("IN", dr, proj, "field")
        if acct(doc.get("creator")):
            g.link("CREATED_BY", dr, acct(doc["creator"]), "field", role="creator", at=utc(doc["createdAt"]))
        if doc.get("content"):
            cv = g.rec("ContentVersion", "linear", f"{doc['id']}:content", {"version": "current",
                       "valid_from": utc(doc.get("updatedAt")), "text": doc["content"]}, scope, acl_from=dr)
            g.link("VERSION_OF", cv, dr, "field", kind="revision", order=1)


def _history(g, t, i, states, labels, acct, scope):
    """IN_STATE, ASSIGNED_TO and HAS_LABEL intervals, and one Activity per history entry."""
    hist = sorted(nodes(i.get("history")), key=lambda h: h["createdAt"])
    created = utc(i["createdAt"])
    # an automatic close on a GitHub-linked issue is the merge closing it; the registry's cause enum has no
    # plain "automation" (the spec says it adds one), so other automatic changes carry no cause
    via_git = any(a.get("sourceType") == "github" for a in nodes(i.get("attachments")))

    def cause(auto):
        if not auto:
            return "manual"
        if via_git:
            return "git_automation"
        g.notes["linear: automatic state change with no schema cause (registry lacks 'automation')"] += 1
        return None

    def intervals(link_type, from_key, to_key, current, targets):
        moves = [(utc(h["createdAt"]), h) for h in hist if h.get(to_key) or h.get(from_key)]
        holder = moves[0][1].get(from_key) if moves else current
        start, actor, auto = created, (i.get("creator") or {}).get("id"), False
        for at, h in moves:
            if holder in targets and at > start:
                g.link(link_type, t, targets[holder], "event", valid_from=start, valid_to=at,
                       **({"started_at": start, "ended_at": at, "cause": cause(auto)}
                          if link_type == "IN_STATE" else {}))
            holder, start = h.get(to_key), at
            auto = bool(h.get("autoClosed") or h.get("autoArchived"))
        if holder in targets:
            g.link(link_type, t, targets[holder], "event", valid_from=start,
                   **({"started_at": start, "cause": cause(auto)} if link_type == "IN_STATE" else {}))

    intervals("IN_STATE", "fromStateId", "toStateId", (i.get("state") or {}).get("id"), states)
    assignees = {u: acct(u) for h in hist for u in (h.get("fromAssigneeId"), h.get("toAssigneeId")) if acct(u)}
    if acct(i.get("assignee")):
        assignees[i["assignee"]["id"]] = acct(i["assignee"])
    intervals("ASSIGNED_TO", "fromAssigneeId", "toAssigneeId", (i.get("assignee") or {}).get("id"), assignees)

    # labels: added and removed over time; the issue's current labels close the story
    open_at = {}
    for h in hist:
        at = utc(h["createdAt"])
        for lid in h.get("addedLabelIds") or []:
            open_at.setdefault(lid, at)
        for lid in h.get("removedLabelIds") or []:
            start = open_at.pop(lid, created)
            if lid in labels and at > start:
                g.link("HAS_LABEL", t, labels[lid], "event", valid_from=start, valid_to=at)
    for lb in nodes(i.get("labels")):
        if lb["id"] in labels:
            g.link("HAS_LABEL", t, labels[lb["id"]], "event" if lb["id"] in open_at else "snapshot",
                   valid_from=open_at.get(lb["id"], created))

    fields = [("fromStateId", "toStateId", "state"), ("fromAssigneeId", "toAssigneeId", "assignee"),
              ("fromPriority", "toPriority", "priority"), ("fromTitle", "toTitle", "title"),
              ("fromEstimate", "toEstimate", "estimate"), ("fromDueDate", "toDueDate", "due_date"),
              ("fromProjectId", "toProjectId", "project"), ("fromCycleId", "toCycleId", "cycle"),
              ("fromParentId", "toParentId", "parent")]
    for h in hist:
        at = utc(h["createdAt"])
        act = g.rec("Activity", "linear", h["id"], {"verb": "update", "at": at}, scope, acl_from=t)
        g.link("ACTS_ON", act, t, "event")
        actor = acct(h.get("actorId") or h.get("actor"))
        if actor:
            g.link("ACTED_BY", act, actor, "event", role="actor", action="perform", at=at)
        changes = [(name, h.get(f), h.get(to)) for f, to, name in fields if h.get(f) is not None or h.get(to) is not None]
        changes += [("labels", None, ",".join(h["addedLabelIds"]))] if h.get("addedLabelIds") else []
        changes += [("labels", ",".join(h["removedLabelIds"]), None)] if h.get("removedLabelIds") else []
        changes += [("description", None, "edited")] if h.get("updatedDescription") else []
        for n, (name, old, new) in enumerate(changes):
            fc = g.rec("FieldChange", "linear", f"{h['id']}:{n}", {"field": name, "at": at,
                       "from_value": None if old is None else str(old), "to_value": None if new is None else str(new)},
                       scope, acl_from=t)
            g.link("PART_OF", fc, act, "event")


def _comments(g, t, i, acct, scope, key_rx):
    comments = {}
    for c in sorted(nodes(i.get("comments")), key=lambda c: c["createdAt"]):
        at = utc(c["createdAt"])
        cm = g.rec("Comment", "linear", c["id"], {"text": c["body"], "kind": "ticket", "at": at,
                   "edited_at": utc(c.get("editedAt")), "resolved": bool(c.get("resolvedAt")) or None,
                   "resolved_at": utc(c.get("resolvedAt"))}, scope, acl_from=t)
        comments[c["id"]] = cm
        g.link("ON", cm, t, "field", at=at)
        if acct(c.get("user")):
            g.link("AUTHORED_BY", cm, acct(c["user"]), "field", role="author", at=at)
        if (c.get("parent") or {}).get("id") in comments:
            g.link("REPLY_TO", cm, comments[c["parent"]["id"]], "field")
        if acct(c.get("resolvingUser")):
            g.link("RESOLVED_BY", cm, acct(c["resolvingUser"]), "field", at=utc(c.get("resolvedAt")))
        for repo, num in PR_URL.findall(c["body"] or ""):
            if repo.lower() == g.ctx.get("repo_name", "").lower():
                g.ref(cm, "LINKS_TO", "PullRequest", int(num), "field", 0.95, stub=True, kind="url", at=at)
        for k in set(key_rx.findall(c["body"] or "")) - {i["identifier"]}:
            g.ref(cm, "MENTIONS", "Ticket", k, "text_rule", 0.8, kind="key", at=at)


def _attachments(g, t, i, scope):
    for a in nodes(i.get("attachments")):
        at = utc(a.get("createdAt"))
        att = g.rec("Attachment", "linear", a["id"], {"title": a.get("title"), "url": a.get("url"),
                    "kind": a.get("sourceType") or "link", "created_at": at}, scope, acl_from=t)
        g.link("HAS_ATTACHMENT", t, att, "field", valid_from=at)
        url = a.get("url") or ""
        pr = PR_URL.match(url)
        if pr and pr.group(1).lower() == g.ctx.get("repo_name", "").lower():
            g.ref(t, "LINKS_TO", "PullRequest", int(pr.group(2)), "integration", stub=True, kind="url", at=at)
        sm = SLACK_MSG_URL.match(url)
        if sm:  # a Slack message attached to the issue; its thread is where the issue is discussed
            channel, ts = sm.group(1), f"{sm.group(2)}.{sm.group(3)}"
            created = utc(i["createdAt"])
            made_from = at and created and abs(_secs(at) - _secs(created)) <= 120  # attached as the issue was made
            g.ref(t, "CREATED_FROM" if made_from else "LINKS_TO", "Message", f"{channel}:{ts}", "integration",
                  at=at, **({} if made_from else {"kind": "url"}))
            g.ref(t, "DISCUSSED_IN", "Thread", f"{channel}:{sm.group(4) or ts}", "integration", at=at)


def _secs(utc_text):
    return dt.datetime.strptime(utc_text, "%Y-%m-%dT%H:%M:%SZ").timestamp()
