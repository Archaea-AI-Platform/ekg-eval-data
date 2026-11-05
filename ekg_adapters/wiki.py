"""Documents pillar: Confluence space -> Folder, Document (page tree), ContentVersion history, Piece per
heading section, Label, Comment. Knowledge layer: each KIP page -> a Decision (rule-based extraction).

Sections are Pieces, not DocSections: the registry lets a DocSection attach only to a
File or another DocSection, never to a Document, so a wiki page's sections can't be
DocSections (reported as a schema gap).
"""

import re
import urllib.parse
from pathlib import Path

from .access import place
from .common import Graph, J, ticket_rx, utc, xhtml_sections

STATE_RX = re.compile(r"Current\s+[Ss]tate\s*(?:</[^>]+>\s*)*:?\s*(?:<[^>]+>\s*)*(?:&quot;|&ldquo;|&rdquo;|&#39;|[\"'“”])?\s*([A-Za-z][A-Za-z \-]{2,30})")
KIP_STATUS = {"accepted": "accepted", "adopted": "accepted", "approved": "accepted", "implemented": "accepted",
              "completed": "accepted", "released": "accepted", "under": "proposed", "discussion": "proposed",
              "draft": "proposed", "voting": "proposed", "vote": "proposed", "in": "proposed", "wip": "proposed",
              "superseded": "superseded", "replaced": "superseded",
              "rejected": "rejected", "discarded": "withdrawn", "abandoned": "withdrawn", "withdrawn": "withdrawn",
              "dormant": "proposed"}  # dormant: still open, nobody working on it
DECISION_STATUSES = {"accepted", "proposed", "superseded", "rejected", "withdrawn"}
KEY_TITLE = re.compile(r"\s*([A-Z][A-Z0-9]*-\d+)\s*[:\-\u2013\u2014]?\s*(.*)")
MBOX_MSGID = re.compile(r"mod_mbox/[^\"']*?/(?:%3[Cc]|<)(.+?)(?:%3[Ee]|>)")


def map_wiki(g: Graph, d: Path):
    prefix = g.ctx["settings"].get("decision_prefix")  # a page titled <prefix>-n is a decision proposal
    title_rx = re.compile(rf"^{re.escape(prefix)}-(\d+)") if prefix else None
    key_rx = ticket_rx(g)
    space_json = J(d / "wiki/space.json")
    scope, _ = place(g, "confluence", "space", space_json["key"])
    folder = g.rec("Folder", "confluence", f"space:{space_json['key']}", {"name": space_json["name"], "kind": "space"}, scope)
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
    labels = {}
    for p in pages:
        created = utc(p["history"]["createdDate"])
        docs[p["id"]] = g.rec("Document", "confluence", p["id"], {"title": p["title"], "kind": "wiki_page",
                              "created_at": created, "status": "published"}, scope)
        g.link("IN", docs[p["id"]], folder, "field", valid_from=created)
        if (who := account(p["history"].get("createdBy"))):
            g.link("CREATED_BY", docs[p["id"]], who, "field", role="creator", how="new", at=created)
        for lb in p["metadata"]["labels"]["results"]:
            if lb["name"] not in labels:
                labels[lb["name"]] = g.rec("Label", "confluence", lb["name"], {"name": lb["name"], "kind": "doc"}, scope)
            g.link("HAS_LABEL", docs[p["id"]], labels[lb["name"]], "snapshot")

    for p in pages:
        doc = docs[p["id"]]
        if p["ancestors"] and p["ancestors"][-1]["id"] in docs:
            g.link("PART_OF", doc, docs[p["ancestors"][-1]["id"]], "field")
        current = _versions(g, p, doc, history.get(p["id"]), scope, account)

        # sections of the current version, as Pieces
        for order, (level, heading, text) in enumerate(xhtml_sections(p["body"]["storage"]["value"])):
            if not text:
                continue
            piece = g.rec("Piece", "confluence", f"{p['id']}:v{p['version']['number']}:{order}", {"text": text,
                          "kind": "section", "heading": heading or None, "order": order,
                          "created_at": utc(p["version"]["when"])}, scope, acl_from=doc)
            g.link("PART_OF", piece, doc, "field", order=order)
            g.link("OF_VERSION", piece, current, "field")

        # comments on pages edited in the window
        for c in (history.get(p["id"]) or {}).get("comments", {}).get("results", []):
            at = utc(c["version"]["when"])
            cm = g.rec("Comment", "confluence", c["id"], {"text": re.sub(r"<[^>]+>", " ", c["body"]["storage"]["value"]).strip(),
                       "kind": "doc", "at": at}, scope, acl_from=doc)
            g.link("ON", cm, doc, "field", at=at)
            if (who := account(c["version"].get("by"))):
                g.link("AUTHORED_BY", cm, who, "field", role="author", at=at)

        # knowledge: a KIP page is a decision record
        m = title_rx.match(p["title"]) if title_rx else None
        if m:
            _decision(g, p, doc, int(m.group(1)), key_rx, scope, decisions)
    g.ctx["confluence_accounts"] = accounts


def _versions(g, p, doc, hist, scope, account):
    """ContentVersion per version: full text for the current one and the older bodies fetched; metadata for the rest."""
    cur_n = p["version"]["number"]
    listed = {v["number"]: v for v in (hist or {}).get("versions", {}).get("results", [])}
    listed.setdefault(cur_n, p["version"])
    bodies = {b["version"]["number"]: b["body"]["storage"]["value"] for b in (hist or {}).get("version_bodies", [])}
    bodies[cur_n] = p["body"]["storage"]["value"]
    numbers = sorted(listed)
    prev, current = None, None
    for idx, n in enumerate(numbers):
        v = listed[n]
        nxt = listed[numbers[idx + 1]]["when"] if idx + 1 < len(numbers) else None
        start, end = utc(v["when"]), utc(nxt)
        text = bodies.get(n)
        cv = g.rec("ContentVersion", "confluence", f"{p['id']}:v{n}", {
            "version": n, "valid_from": start, "valid_to": end if end and end > start else None,
            "editor": (v.get("by") or {}).get("username"), "note": v.get("message") or None,
            "text": "\n".join(f"{h}\n{t}" if h else t for _, h, t in xhtml_sections(text)).strip() if text else None},
            scope, acl_from=doc)
        g.link("VERSION_OF", cv, doc, "field", kind="revision", order=n)
        if prev:
            g.link("NEXT", prev, cv, "field", kind="next")
        if not text:
            g.notes["wiki: versions kept as metadata only (body not fetched)"] += 1
        prev = cv
        if n == cur_n:
            current = cv
    if hist and len(hist.get("versions", {}).get("results", [])) >= 200:
        g.notes["wiki: version list cut at 200 by the downloader"] += 1
    return current


def _decision(g, p, doc, kip, key_rx, scope, decisions):
    body = p["body"]["storage"]["value"]
    st = STATE_RX.search(body)
    word = st.group(1).strip().split()[0].lower() if st else None
    status = KIP_STATUS.get(word, "unmapped") if word else "unparsed"
    g.notes[f"knowledge: KIP status -> {status}"] += 1
    m = KEY_TITLE.match(p["title"])
    dec = g.rec("Decision", "derived", f"kip:{kip}", {"statement": p["title"],
                "identifier": m.group(1) if m else None, "title": (m.group(2) if m else p["title"]) or None,
                "status": status if status in DECISION_STATUSES else None}, scope)
    decisions[kip] = dec
    # where it was made: the KIP page itself (written), and its discussion thread when the link names a message
    g.link("MADE_IN", dec, doc, "text_rule", 0.95, evidence=[doc], at=utc(p["history"]["createdDate"]))
    for mid in MBOX_MSGID.findall(body):
        mid = "<" + urllib.parse.unquote(mid) + ">"
        if (msg := g.ctx.get("emails", {}).get(mid)):
            g.link("MADE_IN", dec, msg, "text_rule", 0.95, evidence=[doc], at=msg["props"]["sent_at"])
            g.notes["knowledge: KIP linked to its discussion email by Message-ID"] += 1
        else:
            g.notes["knowledge: KIP discussion link to an email outside the window"] += 1
    g.notes["knowledge: KIP discussion links without a Message-ID (lists.apache.org/thread/…)"] += len(
        re.findall(r"lists\.apache\.org/thread", body))
    # what it is about: the tickets it names (only those in the download; text mentions make no stubs)
    for k in sorted(set(key_rx.findall(body))):
        g.ref(dec, "ABOUT", "Ticket", k, "text_rule", 0.8, evidence=[doc])
