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

import base64
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
WIKI_HOSTS = set()  # the wiki tool's own hosts (from the manifest's sources), set by resolve()
TINY_URL = re.compile(r"/x/([A-Za-z0-9_-]{2,11})(?:[/?#]|$)")
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
    form = page_url_form(url)
    if form and (pages[0] or pages[1]):
        pid, space, title = page_url_key(url, form)
        hit = pages[0].get(pid) if pid else pages[1].get(((space or "").lower(), title.casefold())) if title else None
        return hit
    return None


def page_url_form(url: str):
    """Which of the wiki tool's standard page-address forms a URL is, or None: page_id (?pageId=N, /pages/N/...),
    title (/display/SPACE/Title+With+Pluses, or viewpage.action?spaceKey=S&title=T) or tiny (/x/<code>)."""
    if PAGE_ID_URL.search(url):
        return "page_id"
    if PAGE_TITLE_URL.search(url) or ("viewpage.action" in url and "title=" in url):
        return "title"
    if TINY_URL.search(url):  # a tiny link's code is only a page id on the wiki tool's own host
        return "tiny" if urllib.parse.urlsplit(url).netloc.lower() in WIKI_HOSTS else None
    return None


def page_url_key(url: str, form: str):
    """(page id, space, title) a page address names. A tiny link's code is the page id itself: the id's bytes, little
    endian with trailing zero bytes dropped, in base64 with '/' written '-' and '+' written '_' and the padding cut."""
    if form == "page_id":
        m = PAGE_ID_URL.search(url)
        return m.group(1) or m.group(2), None, None
    if form == "tiny":
        code = TINY_URL.search(url).group(1)
        if len(code) > 11:
            return None, None, None
        raw = base64.b64decode(code.replace("-", "/").replace("_", "+").ljust(11, "A") + "=")
        return str(int.from_bytes(raw, "little")), None, None
    m = PAGE_TITLE_URL.search(url)
    if m:
        return None, urllib.parse.unquote(m.group(1)), urllib.parse.unquote_plus(m.group(2)).strip("/")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return None, (q.get("spaceKey") or [""])[0], (q.get("title") or [""])[0]


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
    WIKI_HOSTS.clear()
    WIKI_HOSTS.update(urllib.parse.urlsplit(u).netloc.lower() for k, u in (g.ctx.get("source_urls") or {}).items()
                      if k in ("wiki", "confluence") and isinstance(u, str))
    page_urls = collections.Counter()  # (form, resolved or not) of the wiki tool's page addresses
    for source, ltype, ttype, tkey, method, conf, stub, own in g.refs:
        own = dict(own)
        reverse, evidence = own.pop("reverse", False), own.pop("evidence", None)
        only = own.pop("only", None)  # a second, document-level copy of a piece's link: only to that record type
        if ttype == "GitHubRef":
            target = issues.get(tkey) or prs.get(tkey)
        elif ttype == "Url":
            target = _url_target(g, tkey, issues, prs)
            form = page_url_form(tkey)
            if form and only is None:
                page_urls[f"{form}: {'resolved' if target is not None and target['type'] == 'Document' else 'unresolved'}"] += 1
            if target is None:   # an address outside the graph: nothing to link to, and nothing wrong
                if only is None:
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
        if only is not None:
            if target is None or target["type"] != only:
                continue   # the piece's own reference is counted; its document-level copy only adds the link
        elif target is None and stub and ttype in STUB_PROPS:
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
            "outside_addresses": dict(outside), "wiki_page_addresses": dict(sorted(page_urls.items()))}
