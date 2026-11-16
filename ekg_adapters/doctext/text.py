"""Markdown, plain text and reStructuredText -> DocText.

Markdown is read line by line into headings, fenced code, tables, lists, quotes and paragraphs. A
front-matter block at the top (`---` ... `---`, the YAML-ish kind static-site and docs tools write)
goes to `meta["front_matter"]`, not into the text: owner, status and date are claims about the
document, not part of what it says. Inline markup is left as written, except links and images,
whose targets become refs.
"""

from __future__ import annotations

import re

from .model import Block, DocText, Ref, nonws

FENCE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})\s*([\w+#.-]*)[^`]*$")
ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
LIST = re.compile(r"^(\s*)(?:([-*+])|(\d{1,9})[.)])\s+(.*)$")
TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
LINK_DEF = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s+[\"'(].*[\"')])?\s*$")
IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\[([^\]]*)\]")
AUTOLINK = re.compile(r"<((?:https?|ftp)://[^>\s]+)>")
HAS_SCHEME = re.compile(r"(?:[a-z][a-z0-9+.-]*:)?//", re.I)


def decode(data, q=None) -> str:
    """Bytes -> text: UTF-8 (with or without a byte-order mark), UTF-16 by its mark, else Windows-1252, counted."""
    if isinstance(data, str):
        return data[1:] if data[:1] == "﻿" else data
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    elif data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if q is not None:
            q.dropped["not UTF-8: read as Windows-1252"] += 1
            q.warn("not UTF-8: read as Windows-1252")
        return data.decode("cp1252", "replace")


def front_matter(text: str):
    """(front matter dict or None, the rest). Understands `key: value`, `key: [a, b]`, and `key:` followed by
    `- item` lines; quotes around a value are removed. Anything richer is kept as the raw string."""
    m = re.match(r"\A(---|\+\+\+)[ \t]*\r?\n(.*?)\r?\n\1[ \t]*(?:\r?\n|\Z)", text, re.S)
    if not m:
        return None, text
    fm, key = {}, None
    for line in m.group(2).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", line) or re.match(r"^-\s+(.*)$", line)
        if item and key:
            if not isinstance(fm.get(key), list):
                fm[key] = []
            fm[key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_][\w .-]*?)\s*[:=]\s*(.*)$", line)
        if not kv or line[:1].isspace():
            if key and isinstance(fm.get(key), str):   # a folded value continues
                fm[key] = (fm[key] + " " + line.strip()).strip()
            continue
        key, value = kv.group(1).strip().lower().replace(" ", "_"), kv.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            fm[key] = [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
        elif value in ("", "|", ">", "|-", ">-"):
            fm[key] = ""
        else:
            fm[key] = _scalar(value)
    return fm, text[m.end():]


def _scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _cells(line: str):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _inline(text: str, defs: dict, refs: list) -> str:
    """Links and images: the words stay, the target becomes a ref and is shown when the words do not show it."""
    def target(url):
        return "url" if HAS_SCHEME.match(url) else "email" if url.lower().startswith("mailto:") else "file"

    def image(m):
        alt, src = m.group(1), m.group(2)
        refs.append(Ref("image", src, alt, {"alt": alt} if alt else {}))
        return f"[image: {alt or src.rsplit('/', 1)[-1]}]"

    def link(m):
        words, url = m.group(1), m.group(2)
        if url.startswith("#"):
            return words
        kind = target(url)
        if kind == "email":
            url = url[7:].split("?")[0].lower()
        refs.append(Ref(kind, url, words))
        return words if url in words else f"{words} <{url}>"

    def ref_link(m):
        words, key = m.group(1), (m.group(2) or m.group(1)).strip().lower()
        url = defs.get(key)
        if not url:
            return m.group(0)
        refs.append(Ref(target(url), url, words))
        return words if url in words else f"{words} <{url}>"

    text = IMAGE.sub(image, text)
    text = LINK.sub(link, text)
    if defs:
        text = REF_LINK.sub(ref_link, text)
    for m in AUTOLINK.finditer(text):
        refs.append(Ref("url", m.group(1)))
    return text


def read_markdown(data, title=None) -> DocText:
    doc = DocText(title=title, format="markdown")
    q = doc.quality
    text = decode(data, q).replace("\r\n", "\n").replace("\r", "\n")
    q.source_chars = nonws(text)
    fm, body = front_matter(text)
    if fm is not None:
        doc.meta["front_matter"] = fm
        if fm.get("title") and isinstance(fm["title"], str):
            doc.title = doc.title or fm["title"]
    lines = body.split("\n")
    defs = {}
    in_fence = None
    for ln in lines:  # link definitions can come after their use: collect them first
        f = FENCE.match(ln)
        if f and (in_fence is None or (f.group(2)[0] == in_fence[0] and len(f.group(2)) >= len(in_fence))):
            in_fence = None if in_fence else f.group(2)
        elif in_fence is None and (d := LINK_DEF.match(ln)):
            defs[d.group(1).strip().lower()] = d.group(2)

    blocks, para, i = [], [], 0
    list_indents = []  # indent widths of the open list levels

    def add(kind, raw, **kw):
        refs = []
        blocks.append(Block(kind, _inline(raw, defs, refs), refs=refs, **kw))

    def end_para():
        if para:
            add("paragraph", "\n".join(para))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        f = FENCE.match(ln)
        if f:
            end_para()
            mark, lang, code = f.group(2), f.group(3), []
            i += 1
            while i < len(lines):
                c = lines[i].strip()
                if c.startswith(mark[0] * len(mark)) and not c.strip(mark[0]):
                    break
                code.append(lines[i])
                i += 1
            pad = len(f.group(1))
            blocks.append(Block("code", "\n".join(x[pad:] if x[:pad].isspace() else x for x in code), lang=lang.lower() or None))
            i += 1
            list_indents.clear()
            continue
        if not ln.strip():
            end_para()
            i += 1
            continue
        if LINK_DEF.match(ln) and not para:
            i += 1
            continue
        h = ATX.match(ln)
        if h:
            end_para()
            add("heading", h.group(2), level=len(h.group(1)))
            list_indents.clear()
            i += 1
            continue
        if para and re.fullmatch(r"\s{0,3}(=+|-+)\s*", ln) and len(para) == 1 and not LIST.match(para[0]):
            level = 1 if ln.strip()[0] == "=" else 2  # a setext heading: the line above, underlined
            raw = para.pop()
            add("heading", raw.strip(), level=level)
            i += 1
            continue
        if re.fullmatch(r"\s{0,3}([-*_])(\s*\1){2,}\s*", ln):   # a rule
            end_para()
            i += 1
            continue
        if "|" in ln and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1] \
                and len(_cells(lines[i + 1])) == len(_cells(ln)):
            end_para()
            refs = []
            rows = [[_inline(c, defs, refs) for c in _cells(ln)]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([_inline(c, defs, refs) for c in _cells(lines[i])])
                i += 1
            width = max(len(r) for r in rows)
            blocks.append(Block("table", rows=[r + [""] * (width - len(r)) for r in rows], header=len(rows) > 1, refs=refs))
            list_indents.clear()
            continue
        if ln.lstrip().startswith(">"):
            end_para()
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            add("quote", "\n".join(quote).strip("\n"))
            continue
        li = LIST.match(ln)
        if li and (not para or list_indents or li.group(2) or li.group(3) == "1"):
            end_para()
            indent = len(li.group(1).expandtabs(4))
            while list_indents and indent < list_indents[-1]:
                list_indents.pop()
            if not list_indents or indent > list_indents[-1]:
                list_indents.append(indent)
            item, meta = li.group(4), {}
            if li.group(3):
                meta = {"ordered": True, "number": int(li.group(3))}
            t = TASK.match(item)
            if t:
                meta["checked"] = t.group(1) != " "
                item = t.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() and lines[i][:1].isspace() and not LIST.match(lines[i]) \
                    and not FENCE.match(lines[i]):
                item += "\n" + lines[i].strip()   # a wrapped line of the same item
                i += 1
            add("list_item", item, level=len(list_indents), meta=meta)
            continue
        if list_indents and ln[:1].isspace() and blocks and blocks[-1].kind == "list_item" and not para:
            # a further paragraph of the item above
            add("list_item", ln.strip(), level=blocks[-1].level, meta={"cont": True})
            i += 1
            continue
        if not ln[:1].isspace():
            list_indents.clear()
        para.append(ln.rstrip())
        i += 1
    end_para()
    doc.blocks = [b for b in blocks if b.kind == "table" or b.text.strip()]
    return doc.finish()


RST_ADORN = re.compile(r"^([=\-~^\"'`#*+_.:])\1{2,}\s*$")


def read_text(data, title=None, fmt="text") -> DocText:
    """Plain text and reStructuredText: paragraphs split on blank lines; a line underlined with ===, --- ... is a
    heading (levels in the order the underline styles first appear); `.. code-block::` and a paragraph ending
    in `::` introduce a literal block, kept as code."""
    doc = DocText(title=title, format=fmt)
    text = decode(data, doc.quality).replace("\r\n", "\n").replace("\r", "\n")
    doc.quality.source_chars = nonws(text)
    lines, blocks, para, styles, i = text.split("\n"), [], [], [], 0

    def end_para():
        if para:
            blocks.append(Block("paragraph", "\n".join(para)))
            para.clear()

    def literal(start):
        """The indented block after `start` (blank lines inside it included): (code, next index)."""
        j, code = start, []
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or not lines[j][:1].isspace():
            return None, start
        while j < len(lines) and (not lines[j].strip() or lines[j][:1].isspace()):
            code.append(lines[j])
            j += 1
        while code and not code[-1].strip():
            code.pop()
        pad = min(len(c) - len(c.lstrip()) for c in code if c.strip())
        return "\n".join(c[pad:] for c in code), j

    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            end_para()
            i += 1
            continue
        under = lines[i + 1] if i + 1 < len(lines) else ""
        if not para and ln.strip() and RST_ADORN.match(under) and len(under.rstrip()) >= len(ln.strip()) \
                and not RST_ADORN.match(ln) and len(ln.strip()) <= 200:
            style = under[0]
            if style not in styles:
                styles.append(style)
            blocks.append(Block("heading", ln.strip(), level=min(styles.index(style) + 1, 6)))
            i += 2
            continue
        if RST_ADORN.match(ln) and not para:   # an overline or a rule: not content
            i += 1
            continue
        if fmt == "rst":
            d = re.match(r"^\.\.\s+(code-block|code|sourcecode|literalinclude|parsed-literal)::\s*(\S*)", ln)
            if d:
                end_para()
                j = i + 1
                while j < len(lines) and re.match(r"^\s+:\w[\w-]*:", lines[j]):   # the directive's options
                    j += 1
                code, nxt = literal(j)
                if code:
                    blocks.append(Block("code", code, lang=d.group(2).lower() or None))
                    i = nxt
                    continue
                if d.group(1) == "literalinclude" and d.group(2):
                    blocks.append(Block("paragraph", ln.strip(), refs=[Ref("file", d.group(2))]))
                i = j
                continue
            if ln.rstrip().endswith("::") and not ln.lstrip().startswith(".."):
                code, nxt = literal(i + 1)
                if code:
                    para.append(ln.rstrip()[:-1] if ln.rstrip() != "::" else "")
                    if not para[-1].strip(":"):
                        para.pop()
                    end_para()
                    blocks.append(Block("code", code))
                    i = nxt
                    continue
        para.append(ln.rstrip())
        i += 1
    end_para()
    doc.blocks = blocks
    return doc.finish()
