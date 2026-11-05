"""Cross-source linking: resolve the references every adapter queued (ticket keys, PR numbers, #n, shas, KIPs).

Runs after all adapters, so a reference made by one tool finds the record another
tool produced. A reference the tool itself made (a Jira remote link, a PR title
convention, a commit's "(#n)") to something outside the download gets a stub
record so the link still lands; a mention in free text does not, it is counted
as unresolved instead.

GitHub numbers issues and pull requests from one sequence, so a bare "#n" is a "GitHubRef":
the issue with that number if one was loaded, else the PR, else unresolved. "GitHubIssue" (a
closing keyword such as "Fixes #12") resolves to an issue only. A reference whose link the
schema does not allow between the two record types is counted, never written.
"""

import collections

from . import common
from .common import Graph

STUB_PROPS = {"Ticket": ("jira", lambda k: f"key:{k}", lambda k: {"identifier": k}),
              "PullRequest": ("github", lambda n: f"number:{n}", lambda n: {"number": n})}


def resolve(g: Graph):
    issues = g.ctx.setdefault("issues_by_number", {})
    prs = g.ctx.setdefault("prs", {})
    lookups = {"Ticket": g.ctx.setdefault("tickets", {}), "PullRequest": prs,
               "Commit": g.ctx.setdefault("commits", {}), "Decision": g.ctx.setdefault("decisions", {}),
               "TicketByBranch": g.ctx.setdefault("ticket_by_branch", {}),
               "Message": g.ctx.setdefault("messages", {}), "Thread": g.ctx.setdefault("chat_threads", {}),
               "Channel": g.ctx.setdefault("slack_channels", {}), "GitHubIssue": issues}
    resolved, unresolved, stubbed = collections.Counter(), collections.Counter(), collections.Counter()
    for source, ltype, ttype, tkey, method, conf, stub, own in g.refs:
        own = dict(own)
        reverse, evidence = own.pop("reverse", False), own.pop("evidence", None)
        if ttype == "GitHubRef":
            target = issues.get(tkey) or prs.get(tkey)
        else:
            target = lookups[ttype].get(tkey)
        if target is None and stub and ttype in STUB_PROPS:
            tool, native, props = STUB_PROPS[ttype]
            if ttype == "Ticket":  # the key's prefix says which tracker it belongs to
                tool = g.ctx.get("ticket_tool_by_prefix", {}).get(str(tkey).split("-")[0], tool)
            target = g.stub(ttype, tool, native(tkey), props(tkey), g.ctx["scope_by_tool"][tool])
            lookups[ttype][tkey] = target
            stubbed[ttype] += 1
        shown = {"TicketByBranch": "Ticket", "GitHubIssue": "Ticket",
                 "GitHubRef": target["type"] if target else "Ticket or PR"}.get(ttype, ttype)
        label = f"{shown if reverse else source['type']} → {source['type'] if reverse else shown}: {ltype} ({method})"
        if target is None:
            unresolved[label] += 1
            continue
        a, b = (target, source) if reverse else (source, target)
        if a["id"] == b["id"]:
            continue
        if not common._reg.link_allows(ltype, a["type"], b["type"]):
            unresolved[f"{label} [schema does not allow {a['type']} → {b['type']}]"] += 1
            continue
        g.link(ltype, a, b, method, conf, evidence=evidence or [source], **own)
        resolved[label] += 1
    return {"resolved": dict(resolved), "unresolved": dict(unresolved), "stubbed_targets": dict(stubbed)}
