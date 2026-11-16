"""Confluence storage XHTML and plain HTML -> DocText.

The source is parsed into a small tree first and the tree is walked, because the things that used to
be lost all need structure: a code block's CDATA body, a table inside a table cell, a link whose
target is a child element (`<ac:link><ri:page ri:content-title=".."/>`), a macro whose parameters
are settings but whose body is content.

Macros are handled on purpose, one by one:
  code, noformat                         body kept as a code block (with its language); `title` is a caption
  info note warning tip panel expand ... `title` and the rich-text body are content
  jira                                   `key` is a ticket reference and visible text; a JQL query is a setting
  status                                 `title` is content (the lozenge's word)
  gliffy drawio view-file                name the attachment they show
  toc anchor children ...                produce nothing of their own: dropped
  anything else                          rich-text / plain-text body kept, parameters dropped, counted in quality
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .model import Block, DocText, Quality, Ref, cell_text, nonws

CDATA_RX = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "template", "noscript", "colgroup"}
BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "nav", "aside", "figure", "figcaption",
         "address", "dl", "dt", "dd", "details", "summary", "fieldset", "form", "center", "tbody", "thead", "tfoot",
         "tr", "td", "th", "caption", "ac:layout", "ac:layout-section", "ac:layout-cell", "ac:rich-text-body",
         "ac:task-body", "body", "html"}
CLOSES_P = {"p", "div", "ul", "ol", "table", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li",
            "section", "article", "dl", "form"}
HEADING = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# ---- Confluence macros
CODE_MACROS = {"code": None, "noformat": None, "plantuml": "plantuml", "plantumlrender": "plantuml", "html": "html",
               "markdown": "markdown", "unmigrated-wiki-markup": "wiki-markup",
               "unmigrated-inline-wiki-markup": "wiki-markup", "latex-formatting": "latex", "mathblock": "latex"}
BODY_MACROS = {"info", "note", "warning", "tip", "panel", "expand", "excerpt", "section", "column", "details", "div",
               "footnote", "quote", "span", "bgcolor", "highlight", "center", "ui-expand", "auiprogress",
               "auiprogressstepstatic"}
INLINE_BODY = {"footnote", "span", "bgcolor", "highlight"}          # their body sits inside a sentence
DROP_MACROS = {"toc", "toc-zone", "anchor", "children", "pagetree", "pagetreesearch", "recently-updated", "livesearch",
               "display-footnotes", "tasks-report-macro", "detailssummary", "create-from-template", "loremipsum",
               "style", "contributors", "content-report-table", "attachments", "listlabels", "change-history",
               "page-info", "profile", "blog-posts", "recently-used-labels", "popular-labels", "space-details",
               "content-by-label", "contentbylabel", "navmap", "index", "gallery"}
DIAGRAM_MACROS = {"gliffy": "name", "drawio": "diagramName", "drawio-sketch": "diagramName", "lucidchart": "name"}
FILE_MACROS = {"view-file", "viewfile", "viewpdf", "viewdoc", "viewxls", "viewppt", "multimedia"}
INCLUDE_MACROS = {"include", "excerpt-include"}
QUERY_MACROS = {"jiraissues", "jql"}   # a live query: the list it shows is not in the page


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, attrs or {}, [], parent

    def find(self, *tags):
        """Descendants with one of these tags, in document order."""
        for c in self.children:
            if isinstance(c, _Node):
                if c.tag in tags:
                    yield c
                yield from c.find(*tags)

    def child(self, tag):
        return next((c for c in self.children if isinstance(c, _Node) and c.tag == tag), None)


class _Tree(HTMLParser):
    """HTML -> a tree of _Node (text is a str child). Forgiving about tags left open."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = _Node("#root")

    def _up_to(self, tags, stop):
        """Close the nearest open element in `tags`, unless one in `stop` is nearer."""
        n = self.cur
        while n is not self.root:
            if n.tag in tags:
                self.cur = n.parent
                return
            if n.tag in stop:
                return
            n = n.parent

    def handle_starttag(self, tag, attrs):
        if tag in CLOSES_P:
            self._up_to({"p"}, {"td", "th", "li", "div", "blockquote", "table", "ac:rich-text-body"})
        if tag == "li":
            self._up_to({"li"}, {"ul", "ol", "ac:task-list"})
        elif tag in ("td", "th"):
            self._up_to({"td", "th"}, {"tr", "table"})
        elif tag == "tr":
            self._up_to({"tr"}, {"table"})
        n = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(source: str):
    """(tree root, the CDATA bodies). CDATA is lifted out before parsing: an HTML parser reads
    `<![CDATA[..]]>` as a declaration and throws its content away, which is how every code block was lost."""
    cdata = []

    def lift(m):
        cdata.append(m.group(1))
        return f'<ekg-cdata n="{len(cdata) - 1}"></ekg-cdata>'

    t = _Tree()
    t.feed(CDATA_RX.sub(lift, source))
    t.close()
    return t.root, cdata


class _Walk:
    def __init__(self, cdata, q: Quality, users=None):
        self.cdata, self.q, self.users = cdata, q, users or {}
        self.blocks, self.buf, self.refs = [], [], []
        self.lists = []      # open lists: {"ordered", "n", "first"}
        self.item = None     # the open list item: {"checked", "first"}
        self.quote = 0
        self.title = None

    # ---- text
    def say(self, s):
        """Content text into the open block."""
        if s:
            self.q.source_chars += nonws(s)
            self.buf.append(s)

    def raw(self, node) -> str:
        """All the text under a node exactly as written (code, parameter values)."""
        out = []
        for c in node.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag == "ekg-cdata":
                out.append(self.cdata[int(c.attrs["n"])])
            elif c.tag == "br":
                out.append("\n")
            else:
                out.append(self.raw(c))
        return "".join(out)

    def flush(self, heading=0):
        raw, self.buf = "".join(self.buf), []
        lines = [re.sub(r"[ \t\r\f\v ​]+", " ", ln).strip() for ln in raw.split("\n")]
        text = "\n".join(ln for ln in lines if ln)
        refs, self.refs = self.refs, []
        if not text:
            if refs:  # a link with no words of its own (should not happen: every ref says something)
                (self.blocks[-1].refs if self.blocks else self.refs).extend(refs)
            return
        if heading:
            b = Block("heading", text.replace("\n", " "), level=heading, refs=refs)
        elif self.lists:
            top = self.lists[-1]
            meta = {"ordered": top["ordered"], "number": top["n"]} if top["ordered"] else {}
            if self.item and self.item.get("checked") is not None:
                meta["checked"] = self.item["checked"]
            if self.item and not self.item["first"]:
                meta["cont"] = True
            if self.item:
                self.item["first"] = False
            b = Block("list_item", text, level=len(self.lists), refs=refs, meta=meta)
        elif self.quote:
            b = Block("quote", text, refs=refs)
        else:
            b = Block("paragraph", text, refs=refs)
        self.blocks.append(b)

    # ---- the walk
    def children(self, node):
        for c in node.children:
            if isinstance(c, str):
                self.say(re.sub(r"\s+", " ", c))
            else:
                self.node(c)

    def node(self, n):
        tag = n.tag
        if tag == "script" and re.search(r"template|text/html", n.attrs.get("type", ""), re.I):
            # a client-side template (text/x-handlebars-template ...): the page's content, kept as markup in a script
            root, cdata = parse(self.raw(n))
            inner = _Walk(cdata, self.q, self.users)
            inner.lists, inner.quote = list(self.lists), self.quote
            self.flush()
            inner.children(root)
            inner.flush()
            self.blocks.extend(inner.blocks)
            return
        if tag in SKIP:
            return
        if tag == "head":   # nothing in it is body text; its <title> names the document
            for t in n.find("title"):
                self.title = self.title or re.sub(r"\s+", " ", self.raw(t)).strip() or None
            return
        if tag in HEADING:
            self.flush()
            self.children(n)
            self.flush(heading=HEADING[tag])
        elif tag in BLOCK:
            self.flush()
            self.children(n)
            self.flush()
        elif tag == "br":
            self.buf.append("\n")
        elif tag == "hr":
            self.flush()
        elif tag in ("ul", "ol", "ac:task-list", "menu"):
            self.flush()
            outer_item = self.item
            self.lists.append({"ordered": tag == "ol", "n": 0})
            self.item = None
            self.children(n)
            self.flush()
            self.lists.pop()
            self.item = outer_item
        elif tag in ("li", "ac:task"):
            self.flush()
            if not self.lists:  # an item outside any list
                self.lists.append({"ordered": False, "n": 0, "loose": True})
            self.lists[-1]["n"] += 1
            self.item = {"first": True, "checked": None}
            if tag == "ac:task":
                status = n.child("ac:task-status")
                self.item["checked"] = (self.raw(status).strip() == "complete") if status is not None else False
                body = n.child("ac:task-body")
                if body is not None:
                    self.children(body)
            else:
                box = next((c for c in n.find("input") if c.attrs.get("type") == "checkbox"), None)
                if box is not None:
                    self.item["checked"] = "checked" in box.attrs
                self.children(n)
            self.flush()
            self.item = None
            if self.lists[-1].get("loose"):
                self.lists.pop()
        elif tag == "blockquote":
            self.flush()
            self.quote += 1
            self.children(n)
            self.flush()
            self.quote -= 1
        elif tag == "pre":
            self.flush()
            code = self.raw(n).strip("\n")
            inner = _Walk(self.cdata, Quality(), self.users)   # links and images inside the block still point somewhere
            for e in n.find("ac:image", "ac:link", "a", "img"):
                inner.node(e)
            inner.flush()
            refs = [r for b in inner.blocks for r in b.refs] + inner.refs
            if code.strip():
                self.q.source_chars += nonws(code)
                self.blocks.append(Block("code", code, lang=_pre_lang(n), refs=refs))
            elif inner.blocks:   # a block that holds only a picture
                self.q.source_chars += sum(nonws(b.text) for b in inner.blocks)
                self.q.images += inner.q.images
                self.blocks.extend(inner.blocks)
        elif tag == "table":
            self.table(n)
        elif tag == "a":
            self.anchor(n)
        elif tag == "img":
            src, alt = n.attrs.get("src", ""), n.attrs.get("alt") or n.attrs.get("title") or ""
            if src and not src.startswith("data:"):
                self.refs.append(Ref("image", src, alt, {"alt": alt} if alt else {}))
                self.q.images += 1
                self.say(f"[image: {alt or src.rsplit('/', 1)[-1]}]")
            elif alt:
                self.say(f"[image: {alt}]")
        elif tag == "title":
            self.title = self.title or re.sub(r"\s+", " ", self.raw(n)).strip() or None
        elif tag == "time":
            inner = self.raw(n).strip()
            self.say(inner or n.attrs.get("datetime", ""))
        elif tag == "input":
            pass
        elif tag == "ekg-cdata":
            self.say(self.cdata[int(n.attrs["n"])])
        elif tag == "ac:link":
            self.link(n)
        elif tag == "ac:image":
            self.image(n)
        elif tag in ("ac:structured-macro", "ac:macro"):
            self.macro(n)
        elif tag == "ac:emoticon":
            self.say(f"({n.attrs.get('ac:name', 'emoticon')})")
        elif tag == "ac:placeholder":
            self.q.dropped["template placeholder"] += 1
        elif tag in ("ac:parameter", "ac:task-id", "ac:task-status", "ac:default-parameter"):
            self.q.dropped["macro parameter"] += 1
        elif tag == "ri:user":
            self.user(n, "")
        elif tag == "ri:url":
            url = n.attrs.get("ri:value", "")
            if url:
                self.refs.append(Ref("url", url))
                self.say(f"<{url}>")
        else:
            if tag.startswith(("ac:", "ri:")) and tag not in ("ac:inline-comment-marker", "ac:link-body",
                                                              "ac:plain-text-link-body", "ac:plain-text-body"):
                self.q.unknown[f"element {tag}"] += 1
            self.children(n)  # inline (b, i, span, code, sup ...) and anything unknown: keep what it holds

    # ---- links
    def anchor(self, n):
        href = (n.attrs.get("href") or "").strip()
        at = len(self.buf)
        self.children(n)
        words = "".join(self.buf[at:])
        if not href or href.startswith(("#", "javascript:")):
            return
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            if addr:
                self.refs.append(Ref("email", addr, words.strip()))
                if addr not in words.lower():
                    self.say(f" <{addr}>")
            return
        kind = "url" if re.match(r"(?:[a-z][a-z0-9+.-]*:)?//", href, re.I) else "file"
        self.refs.append(Ref(kind, href, words.strip()))
        if href not in words:
            self.say(f" <{href}>")

    def user(self, n, body):
        by = next((k for k in ("ri:userkey", "ri:username", "ri:account-id") if n.attrs.get(k)), None)
        key = n.attrs.get(by, "") if by else ""
        name = self.users.get(key)
        self.refs.append(Ref("user", key, body, {"by": (by or "")[3:].replace("-", "_"), **({"name": name} if name else {})}))
        self.say(body or f"@{name or key}")

    def link(self, n):
        """<ac:link> : a page, attachment, user, space or address, with an optional body."""
        plain, rich = n.child("ac:plain-text-link-body"), n.child("ac:link-body")
        at = len(self.buf)
        if plain is not None:
            self.say(re.sub(r"\s+", " ", self.raw(plain)))
        elif rich is not None:
            self.children(rich)
        body = "".join(self.buf[at:]).strip()
        anchor = n.attrs.get("ac:anchor")
        target = next((c for c in n.children if isinstance(c, _Node) and c.tag.startswith("ri:")), None)
        if target is None:
            if not body and anchor:
                self.say(anchor)
            return
        t = target.tag
        if t == "ri:page" or t == "ri:blog-post":
            title = target.attrs.get("ri:content-title", "")
            extra = {k: v for k, v in (("space", target.attrs.get("ri:space-key")), ("anchor", anchor)) if v}
            self.refs.append(Ref("page", title, body, extra))
            self._target(body, title, "page")
        elif t == "ri:attachment":
            name = target.attrs.get("ri:filename", "")
            on = target.child("ri:page")
            extra = {"page": on.attrs.get("ri:content-title", "")} if on is not None else {}
            if on is not None and on.attrs.get("ri:space-key"):
                extra["space"] = on.attrs["ri:space-key"]
            self.refs.append(Ref("attachment", name, body, extra))
            self._target(body, name, "file")
        elif t == "ri:user":
            del self.buf[at:]
            self.user(target, body)
        elif t == "ri:url":
            url = target.attrs.get("ri:value", "")
            self.refs.append(Ref("url", url, body))
            if url and url not in body:
                self.say(f" <{url}>" if body else f"<{url}>")
        elif t == "ri:space":
            key = target.attrs.get("ri:space-key", "")
            self.refs.append(Ref("page", "", body, {"space": key}))
            self._target(body, key, "space")
        elif t == "ri:content-entity":
            cid = target.attrs.get("ri:content-id", "")
            self.refs.append(Ref("page", "", body, {"id": cid}))
            self._target(body, cid, "page id")
        elif t == "ri:shortcut":
            self._target(body, f"{target.attrs.get('ri:parameter', '')}@{target.attrs.get('ri:key', '')}", "shortcut")
        else:
            self.q.unknown[f"link target {t}"] += 1

    def _target(self, body, name, what):
        """Say where a link points when its words do not: `words <page: Title>`; with no words, the name itself."""
        if not name:
            return
        if not body:
            self.say(name)
        elif name not in body:
            self.say(f" <{what}: {name}>")

    def image(self, n):
        att, url = n.child("ri:attachment"), n.child("ri:url")
        alt = n.attrs.get("ac:alt") or n.attrs.get("ac:title") or ""
        self.q.images += 1
        if att is not None:
            name = att.attrs.get("ri:filename", "")
            on = att.child("ri:page")
            extra = {"attachment": True, **({"alt": alt} if alt else {}),
                     **({"page": on.attrs.get("ri:content-title", "")} if on is not None else {})}
            self.refs.append(Ref("image", name, alt, extra))
            self.say(f"[image: {name}]")
        elif url is not None:
            src = url.attrs.get("ri:value", "")
            self.refs.append(Ref("image", src, alt, {"alt": alt} if alt else {}))
            self.say(f"[image: {alt or src}]")
        else:
            self.q.unknown["image with no source"] += 1

    # ---- macros
    def macro(self, n):
        name = (n.attrs.get("ac:name") or "").lower()
        params, param_nodes = {}, []
        for c in n.children:
            if isinstance(c, _Node) and c.tag in ("ac:parameter", "ac:default-parameter"):
                params[c.attrs.get("ac:name", "")] = self.raw(c).strip()
                param_nodes.append(c)
        rich, plain = n.child("ac:rich-text-body"), n.child("ac:plain-text-body")
        used = set()   # parameters that are content; the rest are settings

        def caption(key="title"):
            if params.get(key):
                used.add(key)
                self.say(params[key])
                self.flush()

        if name in CODE_MACROS or (plain is not None and rich is None and name not in ("jira",)):
            self.flush()
            caption()
            code = self.raw(plain).strip("\n") if plain is not None else ""
            lang = params.get("language") or params.get("lang") or CODE_MACROS.get(name)
            if name not in CODE_MACROS:
                self.q.unknown[f"macro {name} (plain body kept as code)"] += 1
                lang = lang or name
            if code.strip():
                self.q.source_chars += nonws(code)
                self.blocks.append(Block("code", code, lang=(lang or "").lower() or None))
            if rich is not None:
                self.children(rich)
                self.flush()
        elif name in BODY_MACROS:
            inline = name in INLINE_BODY
            if not inline:
                self.flush()
                caption()
            else:
                self.say(" ")
            if rich is not None:
                self.children(rich)
            if not inline:
                self.flush()
            else:
                self.say(" ")
        elif name == "jira":
            key = params.get("key")
            if key:
                used.add("key")
                self.refs.append(Ref("ticket", key))
                self.say(key)
            else:
                # a live query: the list it shows is not in the page. The keys the query itself spells out are.
                keys = list(dict.fromkeys(re.findall(r"\b[A-Z][A-Z0-9_]+-\d+\b", params.get("jqlQuery", ""))))
                for k in keys:
                    self.refs.append(Ref("ticket", k))
                if keys:
                    self.say(", ".join(keys))
                self.q.dropped["jira query macro (a live list, not page content)"] += 1
        elif name == "status":
            used.add("title")
            self.say(params.get("title") or params.get("status") or "")
        elif name in DIAGRAM_MACROS:
            diagram = params.get(DIAGRAM_MACROS[name]) or params.get("name") or params.get("displayName")
            if diagram:
                used.add(DIAGRAM_MACROS[name])
                self.refs.append(Ref("attachment", diagram, "", {"diagram": name}))
                self.say(f"[diagram: {diagram}]")
        elif name in FILE_MACROS:
            for att in [a for p in param_nodes for a in p.find("ri:attachment")]:
                fname = att.attrs.get("ri:filename", "")
                self.refs.append(Ref("attachment", fname))
                self.say(f"[file: {fname}]")
            used.add("name")
        elif name in INCLUDE_MACROS:
            for pg in [a for p in param_nodes for a in p.find("ri:page")]:
                title = pg.attrs.get("ri:content-title", "")
                extra = {"space": pg.attrs["ri:space-key"]} if pg.attrs.get("ri:space-key") else {}
                self.refs.append(Ref("page", title, "", {**extra, "include": True}))
                self.say(f"<page: {title}>")
            used.add("")
        elif name == "widget":
            if params.get("url"):
                used.add("url")
                self.refs.append(Ref("url", params["url"]))
                self.say(f"<{params['url']}>")
        elif name in DROP_MACROS or name in QUERY_MACROS:
            self.q.dropped[f"macro {name}"] += 1
        else:
            self.q.unknown[f"macro {name}"] += 1
            if rich is not None:
                self.children(rich)
        # parameters are settings: never text. What they point at (a page, a file) is still a reference.
        for p in param_nodes:
            key = p.attrs.get("ac:name", "")
            if key in used:
                continue
            self.q.dropped["macro parameter"] += 1
            if name in FILE_MACROS or name in INCLUDE_MACROS:
                continue
            for r in p.find("ri:attachment", "ri:page", "ri:url", "ri:user"):
                if r.tag == "ri:user":
                    by = next((k for k in ("ri:userkey", "ri:username", "ri:account-id") if r.attrs.get(k)), "")
                    self.refs.append(Ref("user", r.attrs.get(by, ""), "", {"by": by[3:].replace("-", "_"), "setting": True}))
                elif r.tag == "ri:attachment":
                    self.refs.append(Ref("attachment", r.attrs.get("ri:filename", "")))
                elif r.tag == "ri:page":
                    self.refs.append(Ref("page", r.attrs.get("ri:content-title", ""),
                                         "", {"space": r.attrs["ri:space-key"]} if r.attrs.get("ri:space-key") else {}))
                else:
                    self.refs.append(Ref("url", r.attrs.get("ri:value", "")))

    # ---- tables
    def table(self, n):
        self.flush()
        cap = n.child("caption")
        if cap is not None:
            self.children(cap)
            self.flush()
        rows = []  # [(in thead, [(tag, blocks, colspan, rowspan)])]
        for tr, in_head in _rows(n):
            cells = []
            for c in tr.children:
                if isinstance(c, _Node) and c.tag in ("td", "th"):
                    w = _Walk(self.cdata, self.q, self.users)
                    w.children(c)
                    w.flush()
                    if w.refs and w.blocks:
                        w.blocks[-1].refs.extend(w.refs)
                    cells.append((c.tag, w.blocks, _span(c, "colspan"), _span(c, "rowspan")))
            if cells:
                rows.append((in_head, cells))
        if not rows:
            return
        n_cells = sum(len(cells) for _, cells in rows)
        structured = any(b.kind in ("heading", "code", "table") for _, cells in rows for _, bl, _, _ in cells for b in bl)
        if structured or n_cells == 1:
            # a layout table (a box around content, or cells holding whole sections): its cells are the document
            # flow, one after another, so nothing in them is flattened
            self.q.dropped["layout table read as flow"] += 1
            for _, cells in rows:
                for _, bl, _, _ in cells:
                    self.blocks.extend(bl)
            return
        grid, refs, carry = [], [], {}   # carry: column -> rows a rowspan still covers
        for _, cells in rows:
            line, col = [], 0
            for tag, bl, colspan, rowspan in cells:
                while carry.get(col, 0) > 0:
                    carry[col] -= 1
                    line.append("")
                    col += 1
                line.append(" / ".join(cell_text(b.render()) for b in bl))
                refs += [r for b in bl for r in b.refs]
                if rowspan > 1:
                    carry[col] = rowspan - 1
                col += 1
                for _ in range(colspan - 1):
                    line.append("")
                    if rowspan > 1:
                        carry[col] = rowspan - 1
                    col += 1
            grid.append(line)
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid if any(c.strip() for c in r)]
        if not grid:
            return
        first = rows[0]
        header = first[0] or all(tag == "th" for tag, _, _, _ in first[1])
        self.blocks.append(Block("table", rows=grid, header=bool(header) and len(grid) > 1, refs=refs))


def _rows(table):
    """(tr, inside thead) for the table's own rows, not those of a table nested in a cell."""
    for c in table.children:
        if not isinstance(c, _Node):
            continue
        if c.tag == "tr":
            yield c, False
        elif c.tag in ("thead", "tbody", "tfoot"):
            for r in c.children:
                if isinstance(r, _Node) and r.tag == "tr":
                    yield r, c.tag == "thead"


def _span(node, name):
    v = node.attrs.get(name, "")
    return min(int(v), 1000) if v.isdigit() and int(v) > 0 else 1


def _pre_lang(pre):
    for n in [pre, *pre.find("code")]:
        m = re.search(r"(?:language|lang|brush:|highlight-source)[-:\s]*([\w+#-]+)", n.attrs.get("class", ""))
        if m:
            return m.group(1).lower()
    return None


def _decode(data) -> str:
    if isinstance(data, str):
        return data
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
    for enc in ([m.group(1).decode("ascii", "replace")] if m else []) + ["utf-8"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", "replace")


def _read(data, fmt, title=None, users=None) -> DocText:
    doc = DocText(title=title, format=fmt)
    root, cdata = parse(_decode(data))
    w = _Walk(cdata, doc.quality, users)
    w.children(root)
    w.flush()
    if w.refs and w.blocks:
        w.blocks[-1].refs.extend(w.refs)
    doc.blocks = w.blocks
    doc.title = doc.title or w.title
    return doc.finish()


def read_confluence(data, title=None, users=None) -> DocText:
    """Confluence storage format. `users` maps a userkey to a display name, for mentions."""
    return _read(data or "", "confluence", title, users)


def read_html(data, title=None) -> DocText:
    return _read(data or "", "html", title)
