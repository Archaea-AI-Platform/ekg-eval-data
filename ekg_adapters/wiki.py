"""Documents pillar: Confluence space -> Folder, Document (page tree), ContentVersion history, DocSection and Piece
for every version that has a body, Attachment, Label, Comment. Knowledge layer: each proposal page -> a Decision.

Page bodies are read by doctext.read_confluence (code blocks, tables, page links, attachments, mentions and
issue macros kept) and written by docgraph.emit, the same emitter every document source uses.

Schema notes (the registry is not changed here; each is reported in the run's notes):
  - EDITED_BY cannot start at a Document, so a page's editors are AUTHORED_BY with role "editor" (first_at,
    last_at, count from the version history); the sections a version changed are EDITED_BY its editor, which
    the registry does allow.
  - a DocSection has no OF_VERSION; its link to the page is dated by the versions it is in (see docgraph).
"""

import collections
import re
import urllib.parse
from pathlib import Path

from . import common, docgraph
from .access import place
from .common import Graph, J, ticket_rx, utc
from .doctext import DocText, read_confluence

STATE_RX = re.compile(r"Current\s+[Ss]tate\s*[:|\-–]?\s*[\"'“”‘’]?\s*([A-Za-z][A-Za-z \-]{2,30})")
KIP_STATUS = {"accepted": "accepted", "adopted": "accepted", "approved": "accepted", "implemented": "accepted",
              "completed": "accepted", "released": "accepted", "under": "proposed", "discussion": "proposed",
              "draft": "proposed", "voting": "proposed", "vote": "proposed", "in": "proposed", "wip": "proposed",
              "superseded": "superseded", "replaced": "superseded",
              "rejected": "rejected", "discarded": "withdrawn", "abandoned": "withdrawn", "withdrawn": "withdrawn",
              "dormant": "proposed"}  # dormant: still open, nobody working on it
DECISION_STATUSES = {"accepted", "proposed", "superseded", "rejected", "withdrawn"}
KEY_TITLE = re.compile(r"\s*([A-Z][A-Z0-9]*-\d+)\s*[:\-–—]?\s*(.*)")
MBOX_MSGID = re.compile(r"mod_mbox/[^\"'\s>]*?/(?:%3[Cc]|<)(.+?)(?:%3[Ee]|>)")
# what the tool calls a page's state -> the schema's document status; a state not listed here leaves status out
PAGE_STATUS = {"current": "published", "draft": "draft", "archived": "archived"}


def map_wiki(g: Graph, d: Path):
    prefix = g.ctx["settings"].get("decision_prefix")  # a page titled <prefix>-n is a decision proposal
    title_rx = re.compile(rf"^{re.escape(prefix)}-(\d+)") if prefix else None
    key_rx = ticket_rx(g)
    space_json = J(d / "wiki/space.json")
    space_key = space_json["key"]
    scope, _ = place(g, "confluence", "space", space_key)
    folder = g.rec("Folder", "confluence", f"space:{space_key}", {"name": space_json["name"], "kind": "space"}, scope)
    accounts = {}

    def account(by):
        if not by or not by.get("userKey"):
            return None
        k = by["userKey"]
        if k not in accounts:
            accounts[k] = g.rec("Account", "confluence", k, {"login": by.get("username"), "display_name": by.get("displayName"),
                                "status": "active", "tool": "confluence"}, scope)
        return accounts[k]

    pages = [r for p in sorted((d / "wiki/pages").glob("*.json")) for r in J(p)["results"]]
    history = {h.stem: J(h) for h in (d / "wiki/history").glob("*.json")}
    docs, decisions = {}, g.ctx.setdefault("decisions", {})
    by_id, by_title = g.ctx.setdefault("documents_by_id", {}), g.ctx.setdefault("documents_by_title", {})
    labels = {}
    for p in pages:
        created = utc(p["history"]["createdDate"])
        status = PAGE_STATUS.get(p.get("status"))
        if p.get("status") and status is None:
            g.notes[f"wiki: page state {p['status']!r} has no document status (left out)"] += 1
        docs[p["id"]] = g.rec("Document", "confluence", p["id"], {"title": p["title"], "kind": "wiki_page",
                              "created_at": created, "status": status}, scope)
        by_id[("confluence", str(p["id"]))] = docs[p["id"]]
        by_title[("confluence", ((p.get("space") or {}).get("key") or space_key).lower(), p["title"].casefold())] = docs[p["id"]]
        g.link("IN", docs[p["id"]], folder, "field", valid_from=created)
        if (who := account(p["history"].get("createdBy"))):
            g.link("CREATED_BY", docs[p["id"]], who, "field", role="creator", how="new", at=created)
        for lb in p["metadata"]["labels"]["results"]:
            if lb["name"] not in labels:
                labels[lb["name"]] = g.rec("Label", "confluence", lb["name"], {"name": lb["name"], "kind": "doc"}, scope)
            g.link("HAS_LABEL", docs[p["id"]], labels[lb["name"]], "snapshot")
    # everyone the data names, before any body is read: a mention shows the person's name, not a user key
    for p in pages:
        hist = history.get(p["id"]) or {}
        for by in [p["version"].get("by")] + [v.get("by") for v in hist.get("versions", {}).get("results", [])] \
                + [c["version"].get("by") for c in hist.get("comments", {}).get("results", [])]:
            account(by)
    users = {k: a["props"].get("display_name") or a["props"].get("login") for k, a in accounts.items()}
    g.ctx["confluence_accounts"] = accounts
    max_versions = g.ctx["settings"].get("wiki_piece_versions")   # None: pieces for every version that has a body

    for p in pages:
        doc = docs[p["id"]]
        if p["ancestors"] and p["ancestors"][-1]["id"] in docs:
            g.link("PART_OF", doc, docs[p["ancestors"][-1]["id"]], "field")
        hist = history.get(p["id"])
        text = _versions(g, p, doc, hist, scope, account, users, space_key, max_versions)

        # comments on pages edited in the window
        comments = {}
        for c in (hist or {}).get("comments", {}).get("results", []):
            at = utc(c["version"]["when"])
            ext = c.get("extensions") or {}
            inline = ext.get("inlineProperties") or {}
            resolution = (ext.get("resolution") or {}).get("status")
            parent = (c.get("ancestors") or [{}])[-1].get("id") if c.get("ancestors") else None
            body = read_confluence(c["body"]["storage"]["value"], users=users)
            cm = g.rec("Comment", "confluence", c["id"], {
                "text": body.text, "kind": "doc_reply" if parent else "doc", "at": at,
                "quoted_text": inline.get("originalSelection") or None,
                "resolved": (resolution == "resolved") if resolution else None}, scope, acl_from=doc)
            comments[c["id"]] = cm
            g.link("ON", cm, doc, "field", at=at)
            if parent in comments:
                g.link("REPLY_TO", cm, comments[parent], "field")
            if (who := account(c["version"].get("by"))):
                g.link("AUTHORED_BY", cm, who, "field", role="author", at=at)
            for k in sorted(set(key_rx.findall(body.text))):
                g.ref(cm, "MENTIONS", "Ticket", k, "text_rule", 0.8, kind="key", at=at)

        # knowledge: a proposal page is a decision record
        m = title_rx.match(p["title"]) if title_rx else None
        if m:
            _decision(g, p, doc, int(m.group(1)), key_rx, scope, decisions, text)


def _versions(g, p, doc, hist, scope, account, users, space_key, max_versions=None) -> DocText:
    """ContentVersion per version; sections and pieces for every version whose body was downloaded; metadata only
    for the rest. Returns the current version as read (an empty DocText when it could not be read)."""
    cur_n = p["version"]["number"]
    listed = {v["number"]: v for v in (hist or {}).get("versions", {}).get("results", [])}
    listed.setdefault(cur_n, p["version"])
    bodies = {b["version"]["number"]: b["body"]["storage"]["value"] for b in (hist or {}).get("version_bodies", [])}
    bodies[cur_n] = p["body"]["storage"]["value"]
    numbers = sorted(listed)
    with_pieces = set(sorted(bodies)[-max_versions:]) if max_versions else set(bodies)
    prev, current, read_before = None, DocText(format="confluence"), None
    edits = collections.OrderedDict()   # editor account id -> [account, first_at, last_at, count]
    for idx, n in enumerate(numbers):
        v = listed[n]
        nxt = listed[numbers[idx + 1]]["when"] if idx + 1 < len(numbers) else None
        start, end = utc(v["when"]), utc(nxt)
        who = account(v.get("by"))
        if who is not None:
            e = edits.setdefault(who["id"], [who, start, start, 0])
            e[1], e[2], e[3] = min(e[1], start), max(e[2], start), e[3] + 1
        body, text = bodies.get(n), None
        if body is not None:
            try:
                text = read_confluence(body, title=p["title"], users=users)
            except Exception as ex:   # one unreadable body never stops the run
                g.notes[f"wiki: version body not readable ({type(ex).__name__}), kept as metadata only"] += 1
        cv = g.rec("ContentVersion", "confluence", f"{p['id']}:v{n}", {
            "version": n, "valid_from": start, "valid_to": end if end and end > start else None,
            "editor": (v.get("by") or {}).get("username"), "note": v.get("message") or None,
            "text": text.text if text is not None else None}, scope, acl_from=doc)
        g.link("VERSION_OF", cv, doc, "field", kind="revision", order=n)
        if prev:
            g.link("NEXT", prev, cv, "field", kind="next")
        if text is None:
            if body is None:
                g.notes["wiki: versions kept as metadata only (body not fetched)"] += 1
        elif n in with_pieces:
            # who changed a section is only known when the version before was read too (or this is the first)
            docgraph.emit(g, doc, text, cv, scope, "confluence", f"{p['id']}:v{n}", at=start, current=n == cur_n,
                          editor=who, attribute=n == 1 or read_before == n - 1, space=space_key)
            read_before = n
        else:
            g.notes["wiki: older versions with text but no pieces (settings wiki_piece_versions)"] += 1
        prev = cv
        if n == cur_n and text is not None:
            current = text
    if hist and len(hist.get("versions", {}).get("results", [])) >= 200:
        g.notes["wiki: version list cut at 200 by the downloader"] += 1
    # editors, from the versions the download lists (all of them for a page with history, else the current one):
    # count is the number of listed versions by that editor
    direct = common._reg.link_allows("EDITED_BY", "Document", "Account")
    for who, first, last, count in edits.values():
        if direct:
            g.link("EDITED_BY", doc, who, "event", role="editor", at=last, first_at=first, last_at=last, count=count)
        else:
            g.link("AUTHORED_BY", doc, who, "event", role="editor", at=last, first_at=first, last_at=last, count=count)
            g.notes["wiki: page editors written as AUTHORED_BY role editor (EDITED_BY cannot start at a Document)"] += 1
    return current


def _decision(g, p, doc, kip, key_rx, scope, decisions, text: DocText):
    """The proposal's state, discussion thread and tickets, read from the page as the reader gives it (its text
    and its links), not from the raw markup."""
    st = STATE_RX.search(text.text)
    word = st.group(1).strip().split()[0].lower() if st else None
    status = KIP_STATUS.get(word, "unmapped") if word else "unparsed"
    g.notes[f"knowledge: KIP status -> {status}"] += 1
    m = KEY_TITLE.match(p["title"])
    dec = g.rec("Decision", "derived", f"kip:{kip}", {"statement": p["title"],
                "identifier": m.group(1) if m else None, "title": (m.group(2) if m else p["title"]) or None,
                "status": status if status in DECISION_STATUSES else None}, scope)
    decisions[kip] = dec
    tallies = g.ctx.get("proposal_tally") or g.ctx.get("kip_tally") or {}
    tally = tallies.get(int(kip)) if str(kip).isdigit() else None
    if tally:  # the vote result its [VOTE] thread states
        dec["props"].update({k: v for k, v in tally.items() if k != "at"})
    factor = docgraph.quality_factor(g, doc)   # a badly read page makes what is read off it less sure
    # where it was made: the page itself (written), and its discussion thread when the link names a message
    g.link("MADE_IN", dec, doc, "text_rule", round(0.95 * factor, 3), evidence=[doc], at=utc(p["history"]["createdDate"]))
    urls = sorted({r.target for r in text.refs("url")})
    for mid in sorted({mm for u in urls for mm in MBOX_MSGID.findall(u)}):
        mid = "<" + urllib.parse.unquote(mid) + ">"
        if (msg := g.ctx.get("emails", {}).get(mid)):
            g.link("MADE_IN", dec, msg, "text_rule", round(0.95 * factor, 3), evidence=[doc], at=msg["props"]["sent_at"])
            g.notes["knowledge: KIP linked to its discussion email by Message-ID"] += 1
        else:
            g.notes["knowledge: KIP discussion link to an email outside the window"] += 1
    g.notes["knowledge: KIP discussion links without a Message-ID (lists.apache.org/thread/…)"] += sum(
        1 for u in urls if "lists.apache.org/thread" in u)
    # what it is about: the tickets it names (only those in the download; text mentions make no stubs)
    macro_keys = {r.target for r in text.refs("ticket")}
    for k in sorted(set(key_rx.findall(text.text)) | {k for k in macro_keys if key_rx.fullmatch(k)}):
        g.ref(dec, "ABOUT", "Ticket", k, "text_rule", round(0.8 * factor, 3), evidence=[doc])
