"""Tickets pillar: Jira (REST v2, issues with changelog) -> Ticket, TicketState, Release, Label, Comment,
Activity / FieldChange, ContentVersion, and dated IN_STATE / ASSIGNED_TO history rebuilt from the changelog.

References to other tools are queued for the linker: PR remote links ("GitHub Pull
Request #n"), PR URLs in comments, KIP numbers in the summary and description.
"""

import re
from pathlib import Path

from .access import place
from .common import Graph, J, day, jira_text, utc

CATEGORY_TYPE = {"new": "unstarted", "indeterminate": "started", "done": "completed"}
KIP = re.compile(r"\bKIP-(\d+)\b")
REMOTE_PR = re.compile(r"Pull Request #(\d+)|GitHub PR #?(\d+)", re.I)
# Jira link type (outward direction) -> schema link and kind
LINK_TYPES = {"Blocker": ("BLOCKS", "blocks"), "Blocked": ("BLOCKS", "blocks"), "Duplicate": ("DUPLICATE_OF", None),
              "dependent": ("BLOCKS", "blocks"), "Dependency": ("BLOCKS", "blocks"), "Required": ("BLOCKS", "blocks")}
NON_FIX = {"Duplicate": "duplicate", "Won't Fix": "canceled", "Won't Do": "canceled", "Invalid": "canceled",
           "Not A Problem": "canceled", "Cannot Reproduce": "canceled", "Abandoned": "canceled"}


def map_jira(g: Graph, d: Path, project_key: str, repo: str):
    g.ctx.setdefault("ticket_prefixes", set()).add(project_key)
    scope, _ = place(g, "jira", "tracker_team", project_key)
    team = g.rec("Group", "jira", f"project:{project_key}", {"name": project_key, "kind": "tracker_team", "tool": "jira"}, scope)
    pr_url = re.compile(rf"github\.com/{re.escape(repo)}/pull/(\d+)")

    category = {}
    for itype in J(d / "jira/statuses.json"):
        for s in itype["statuses"]:
            category[s["id"]] = (s["name"], CATEGORY_TYPE.get(s["statusCategory"]["key"], "backlog"))
    states = {}

    def state(sid, name=None):
        if sid not in states:
            nm, typ = category.get(sid, (name, "backlog"))
            states[sid] = g.rec("TicketState", "jira", sid, {"name": nm or name, "type": typ}, scope)
        return states[sid]

    accounts = {}

    def account(user=None, key=None):
        key = key or (user or {}).get("key")
        if not key:
            return None
        if key not in accounts:
            user = user or {}
            name = user.get("displayName") or ""
            accounts[key] = g.rec("Account", "jira", key, {"login": user.get("name") or key, "display_name": name or None,
                                  "status": None if not user else ("active" if user.get("active") else "deactivated"),
                                  "is_bot": name.endswith(" Bot"), "tool": "jira"}, scope)
        elif user and not accounts[key]["props"].get("display_name"):
            accounts[key]["props"]["display_name"] = user.get("displayName")
        return accounts[key]

    releases = {}
    for v in J(d / "jira/versions.json"):
        rel = bool(v.get("released") and v.get("releaseDate"))
        releases[v["id"]] = g.rec("Release", "jira", v["id"], {"version": v["name"], "archived": v.get("archived"),
                                  "published_at": day(v["releaseDate"]) if rel else None,
                                  "published_at_precision": "day" if rel else None}, scope)
    labels = {}

    def label(name, kind):
        if (name, kind) not in labels:
            labels[(name, kind)] = g.rec("Label", "jira", f"{kind}:{name}", {"name": name, "kind": kind}, scope)
        return labels[(name, kind)]

    tickets = g.ctx.setdefault("tickets", {})
    for p in sorted((d / "jira/issues").glob("*.json")):
        for i in J(p)["issues"]:
            f = i["fields"]
            created = utc(f["created"])
            t = g.rec("Ticket", "jira", i["id"], {
                "identifier": i["key"], "title": f["summary"],
                "kind": re.sub(r"[^a-z]+", "_", f["issuetype"]["name"].lower()),
                "priority": (f.get("priority") or {}).get("name"), "created_at": created,
                "resolution": (f.get("resolution") or {}).get("name"), "resolved_at": utc(f.get("resolutiondate")),
                "due_at": day(f.get("duedate")), "due_at_precision": "day" if f.get("duedate") else None}, scope)
            tickets[i["key"]] = t
            g.link("IN", t, team, "field", valid_from=created)
            if f.get("creator"):
                g.link("CREATED_BY", t, account(f["creator"]), "field", role="creator", at=created)
            if f.get("reporter") and f["reporter"].get("key") != (f.get("creator") or {}).get("key"):
                g.link("CREATED_BY", t, account(f["reporter"]), "field", role="reporter", at=created)
                g.notes["jira: reporter differs from creator (CREATED_BY role reporter; AUTHORED_BY does not allow Ticket)"] += 1
            histories = sorted(i["changelog"]["histories"], key=lambda h: h["created"])

            # ---- dated history from the changelog
            _history(g, t, i, f, histories, created, state, account, scope)

            # ---- description versions (each description change carries the old and new text)
            desc_moves = [(utc(h["created"]), it) for h in histories for it in h["items"] if it["field"] == "description"]
            versions = [(created, desc_moves[0][1].get("fromString") if desc_moves else f.get("description"))]
            versions += [(at, it.get("toString")) for at, it in desc_moves]
            prev = None
            for n, (at, text) in enumerate(versions, 1):
                if not text:
                    continue
                nxt = versions[n][0] if n < len(versions) else None
                cv = g.rec("ContentVersion", "jira", f"{i['id']}:description:v{n}", {"version": n, "valid_from": at,
                           "valid_to": nxt if nxt and nxt > at else None, "text": jira_text(text), "note": "description"},
                           scope, acl_from=t)
                g.link("VERSION_OF", cv, t, "field", kind="revision", order=n)
                if prev:
                    g.link("NEXT", prev, cv, "field", kind="next")
                prev = cv

            # ---- releases, components, labels
            for v in f.get("fixVersions") or []:
                if v["id"] in releases:
                    g.link("IN_RELEASE", t, releases[v["id"]], "field", at=releases[v["id"]]["props"].get("published_at"))
            for c in f.get("components") or []:
                g.link("HAS_LABEL", t, label(c["name"], "component"), "field")
            for name in f.get("labels") or []:
                g.link("HAS_LABEL", t, label(name, "label"), "field")
            if f.get("resolution") and f["resolution"]["name"] in NON_FIX:
                g.notes[f"jira: done as '{NON_FIX[f['resolution']['name']]}' by resolution (state type stays completed)"] += 1

            # ---- links between tickets (outward side only; the inward side is the other issue's copy)
            for lk in f.get("issuelinks") or []:
                other = lk.get("outwardIssue")
                if not other:
                    continue
                ltype, kind = LINK_TYPES.get(lk["type"]["name"], ("RELATED_TO", lk["type"]["outward"].replace(" ", "_")))
                g.ref(t, ltype, "Ticket", other["key"], "field", stub=True, kind=kind, valid_from=None)
            if f.get("parent"):
                g.ref(t, "CHILD_OF", "Ticket", f["parent"]["key"], "field", stub=True)

            # ---- references to other tools
            for h in histories:
                for it in h["items"]:
                    if it["field"] == "RemoteIssueLink" and it.get("toString"):
                        m = REMOTE_PR.search(it["toString"])
                        if m:
                            g.ref(t, "LINKS_TO", "PullRequest", int(m.group(1) or m.group(2)), "integration",
                                  stub=True, kind="url", at=utc(h["created"]))
            for n in set(KIP.findall(f"{f['summary']} {f.get('description') or ''}")):
                g.ref(t, "ABOUT", "Decision", int(n), "text_rule", 0.8, reverse=True)

            # ---- comments
            for c in f["comment"]["comments"]:
                at = utc(c["created"])
                cm = g.rec("Comment", "jira", c["id"], {"text": jira_text(c["body"]), "kind": "ticket", "at": at,
                           "edited_at": utc(c["updated"]) if c["updated"] != c["created"] else None}, scope, acl_from=t)
                g.link("ON", cm, t, "field", at=at)
                if c.get("author"):
                    g.link("AUTHORED_BY", cm, account(c["author"]), "field", role="author", at=at)
                for n in set(pr_url.findall(c["body"] or "")):
                    g.ref(cm, "LINKS_TO", "PullRequest", int(n), "field", 0.95, stub=True, kind="url", at=at)
    g.ctx["jira_accounts"] = accounts


def _history(g, t, i, f, histories, created, state, account, scope):
    status_moves = [(utc(h["created"]), it, h.get("author")) for h in histories for it in h["items"] if it["field"] == "status"]
    cur = (status_moves[0][1]["from"], status_moves[0][1]["fromString"]) if status_moves else (f["status"]["id"], f["status"]["name"])
    start, actor = created, f.get("creator")
    for at, it, who in status_moves:
        if at > start:
            g.link("IN_STATE", t, state(*cur), "field", valid_from=start, valid_to=at, started_at=start, ended_at=at,
                   cause="manual", actor=(actor or {}).get("name"))
        else:
            g.notes["jira: zero-length state skipped (two changes in the same second)"] += 1
        cur, start, actor = (it["to"], it["toString"]), at, who
    g.link("IN_STATE", t, state(*cur), "field", valid_from=start, started_at=start, cause="manual", actor=(actor or {}).get("name"))

    assign_moves = [(utc(h["created"]), it) for h in histories for it in h["items"] if it["field"] == "assignee"]
    holder = assign_moves[0][1].get("from") if assign_moves else (f.get("assignee") or {}).get("key")
    start = created
    for at, it in assign_moves:
        if holder and at > start:
            g.link("ASSIGNED_TO", t, account(key=holder), "field", valid_from=start, valid_to=at)
        holder, start = it.get("to"), at
    if holder:
        g.link("ASSIGNED_TO", t, account(f.get("assignee") if (f.get("assignee") or {}).get("key") == holder else None, key=holder),
               "field", valid_from=start)

    for h in histories:
        at = utc(h["created"])
        act = g.rec("Activity", "jira", h["id"], {"verb": "update", "at": at}, scope, acl_from=t)
        g.link("ACTS_ON", act, t, "event")
        if h.get("author"):
            g.link("ACTED_BY", act, account(h["author"]), "event", role="actor", action="perform", at=at)
        for n, it in enumerate(h["items"]):
            fc = g.rec("FieldChange", "jira", f"{h['id']}:{n}", {"field": it["field"], "at": at,
                       "from_value": (it.get("fromString") or it.get("from") or None) and str(it.get("fromString") or it.get("from"))[:500],
                       "to_value": (it.get("toString") or it.get("to") or None) and str(it.get("toString") or it.get("to"))[:500]},
                       scope, acl_from=t)
            g.link("PART_OF", fc, act, "event")
