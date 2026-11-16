"""Cross-source linking: resolve the references every adapter queued (ticket keys, PR numbers, #n, shas, decision-proposal keys).

Runs after all adapters, so a reference made by one tool finds the record another
tool produced. A reference the tool itself made (a Jira remote link, a PR title
convention, a commit's "(#n)") to something outside the download gets a stub
record so the link still lands; a mention in free text does not, it is counted
as unresolved instead.

GitHub numbers issues and pull requests from one sequence, so a bare "#n" is a "GitHubRef":
the issue with that number if one was loaded, else the PR, else unresolved. "GitHubIssue" (a
closing keyword such as "Fixes #12") resolves to an issue only. A reference whose link the
schema does not allow between the two record types is counted, never written.

Documents queue five more kinds of target (see docgraph.py):
  Url              an address in a document: the PR, issue, commit, ticket, wiki page or archived email it
                   names, when that record is in the graph. An address that names none of them is an outside
                   address: counted apart (`outside_addresses`), not an unresolved reference.
  DocumentByTitle  (tool, space, title): a wiki page link, which names its target by title
  DocumentById     (tool, id)
  FileByPath       (tool, path): a relative link from one stored or repository file to another
  AccountByKey     (tool, key): a user mention, by the tool's own user key
  EmailAddress     an address written in the text, when the graph has that address
"""

import collections
import re
import urllib.parse

from . import common
from .common import Graph

STUB_PROPS = {"Ticket": ("jira", lambda k: f"key:{k}", lambda k: {"identifier": k}),
              "PullRequest": ("github", lambda n: f"number:{n}", lambda n: {"number": n})}

GH_URL = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/(pull|issues|commit)/([0-9a-fA-F]+)")
TICKET_URL = re.compile(r"https?://[^/\s]+/(?:[\w-]+/)?browse/([A-Z][A-Z0-9_]*-\d+)|https?://linear\.app/[\w-]+/issue/([A-Z][A-Z0-9]+-\d+)")
PAGE_ID_URL = re.compile(r"[?&]pageId=(\d+)|/pages/(\d+)(?:[/?#]|$)")
PAGE_TITLE_URL = re.compile(r"/display/([^/?#]+)/([^?#]+)")
MBOX_URL = re.compile(r"mod_mbox/[^\"']*?/(?:%3[Cc]|<)(.+?)(?:%3[Ee]|>)")
DOC_KINDS = {"Url": "outside address", "DocumentByTitle": "Document", "DocumentById": "Document", "FileByPath": "File",
             "AccountByKey": "Account", "EmailAddress": "EmailAddress"}


def _url_target(g: Graph, url: str, issues, prs):
    """The record an address names, or None when it names nothing in the graph."""
    m = GH_URL.match(url)
    if m:
        if m.group(1).lower().removesuffix(".git") != (g.ctx.get("repo_name") or "").lower():
            return None
        what, ident = m.group(2), m.group(3)
        if what == "commit":
            return g.ctx.get("commits", {}).get(ident.lower()) if len(ident) == 40 else None
        if not ident.isdigit():
            return None
        return prs.get(int(ident)) if what == "pull" else (issues.get(int(ident)) or prs.get(int(ident)))
    m = TICKET_URL.match(url)
    if m:
        return g.ctx.get("tickets", {}).get(m.group(1) or m.group(2))
    m = MBOX_URL.search(url)
    if m:
        return g.ctx.get("emails", {}).get("<" + urllib.parse.unquote(m.group(1)) + ">")
    pages = g.ctx.get("_pages_for_urls")   # page id -> Document and (space, title) -> Document, whatever the tool
    if pages is None:
        pages = g.ctx["_pages_for_urls"] = (
            {pid: d for (_, pid), d in g.ctx.get("documents_by_id", {}).items()},
            {(sp, t): d for (_, sp, t), d in g.ctx.get("documents_by_title", {}).items()})
    if pages[0] or pages[1]:
        m = PAGE_ID_URL.search(url)
        if m and (m.group(1) or m.group(2)) in pages[0]:
            return pages[0][m.group(1) or m.group(2)]
        m = PAGE_TITLE_URL.search(url)
        if m:
            return pages[1].get((m.group(1).lower(), urllib.parse.unquote_plus(m.group(2)).casefold()))
    return None


def _document(g: Graph, tool, space, title):
    """A page by its title: in the space the link names, else the only page with that title."""
    by_title = g.ctx.get("documents_by_title", {})
    hit = by_title.get((tool, (space or "").lower(), title.casefold()))
    if hit is None:
        same = [d for (t, _, name), d in by_title.items() if t == tool and name == title.casefold()]
        hit = same[0] if len(same) == 1 else None
    return hit


def resolve(g: Graph):
    issues = g.ctx.setdefault("issues_by_number", {})
    prs = g.ctx.setdefault("prs", {})
    lookups = {"Ticket": g.ctx.setdefault("tickets", {}), "PullRequest": prs,
               "Commit": g.ctx.setdefault("commits", {}), "Decision": g.ctx.setdefault("decisions", {}),
               "TicketByBranch": g.ctx.setdefault("ticket_by_branch", {}),
               "Message": g.ctx.setdefault("messages", {}), "Thread": g.ctx.setdefault("chat_threads", {}),
               "Channel": g.ctx.setdefault("slack_channels", {}), "GitHubIssue": issues}
    resolved, unresolved, stubbed = collections.Counter(), collections.Counter(), collections.Counter()
    outside = collections.Counter()
    addresses = None   # EmailAddress records by address, built when first asked for
    for source, ltype, ttype, tkey, method, conf, stub, own in g.refs:
        own = dict(own)
        reverse, evidence = own.pop("reverse", False), own.pop("evidence", None)
        if ttype == "GitHubRef":
            target = issues.get(tkey) or prs.get(tkey)
        elif ttype == "Url":
            target = _url_target(g, tkey, issues, prs)
            if target is None:   # an address outside the graph: nothing to link to, and nothing wrong
                outside[f"{source['type']} ({source['props']['source_tool']})"] += 1
                continue
        elif ttype == "DocumentByTitle":
            target = _document(g, *tkey)
        elif ttype == "DocumentById":
            target = g.ctx.get("documents_by_id", {}).get(tkey)
        elif ttype == "FileByPath":
            target = g.ctx.get("files" if tkey[0] == "git" else "doc_files", {}).get(tkey[1])
        elif ttype == "AccountByKey":
            target = g.ctx.get(f"{tkey[0]}_accounts", {}).get(tkey[1])
        elif ttype == "EmailAddress":
            if addresses is None:
                addresses = {r["props"]["address"].lower(): r for r in g.records.values()
                             if r["type"] == "EmailAddress" and r["props"].get("address")}
            target = addresses.get(tkey)
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
                 "GitHubRef": target["type"] if target else "Ticket or PR",
                 **{k: target["type"] if target else v for k, v in DOC_KINDS.items()}}.get(ttype, ttype)
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
    return {"resolved": dict(resolved), "unresolved": dict(unresolved), "stubbed_targets": dict(stubbed),
            "outside_addresses": dict(outside)}
