"""PDF -> DocText: the text flowing across pages (each block says its page), headings, tables as tables, the
document's own properties as meta.

With pdfplumber the page's ruled tables are read as tables and the text around them as paragraphs,
in reading order; a table drawn without lines is found from where its words sit (see `_borderless`). Without it, pypdf gives the page's text (a table then comes out as its lines).
With neither, the format is unsupported. A page with no text layer (a scan) is counted in
`quality.empty_pages` and warned about; nothing is recognised here, but `ocr` may be passed: a
callable `ocr(pdf_bytes, page_index) -> text` that is asked for exactly those pages.
"""

from __future__ import annotations

import collections
import datetime as dt
import difflib
import io
import re

from .model import Block, DocText, Unreadable, Unsupported, cell_text, nonws

PDF_DATE = re.compile(r"D:(\d{4})(\d\d)?(\d\d)?(\d\d)?(\d\d)?(\d\d)?\s*([Zz+-])?(\d\d)?'?(\d\d)?")


def pdf_date(value):
    """A PDF date (D:20261110032941+05'30') -> UTC ISO, or None."""
    m = PDF_DATE.match(str(value or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, sign, oh, om = m.groups()
    try:
        t = dt.datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    if sign in ("+", "-") and oh:
        off = dt.timedelta(hours=int(oh), minutes=int(om or 0))
        t = t - off if sign == "+" else t + off
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _meta(doc: DocText, info: dict):
    """The document information dictionary. /Author is who wrote it; /Creator is the program that made it."""
    get = lambda k: (str(info.get(k) or info.get("/" + k) or "").strip() or None)
    blank = {"(unspecified)", "unknown", "untitled", "(anonymous)"}
    clean = lambda v: None if v is None or v.lower() in blank else v
    doc.title = doc.title or clean(get("Title"))
    for key, value in (("creator", clean(get("Author"))), ("application", clean(get("Creator")) or clean(get("Producer"))),
                       ("created_at", pdf_date(get("CreationDate"))), ("modified_at", pdf_date(get("ModDate"))),
                       ("subject", clean(get("Subject"))), ("keywords", clean(get("Keywords")))):
        if value:
            doc.meta[key] = value


def _paragraphs(text: str):
    return [p.strip("\n") for p in re.split(r"\n\s*\n", text) if p.strip()]


def _columns(page, table, rows):
    """A table ruled between its rows only (no vertical lines) comes out with one cell per row. Its columns are
    then the vertical strips of white that run through every row: a strip no word of any row crosses, at least
    three quarters of a text line wide (an ordinary space between words is about a quarter). A boundary can
    never fall inside a word, so no text is cut; with no such strip the table stays as its rows of text."""
    try:
        lines = [page.crop(r.bbox).extract_words() for r in table.rows]
    except Exception:
        return None
    words = [w for ws in lines for w in ws]
    if len(lines) != len(rows) or len(words) < 2:
        return None
    min_gap = 0.75 * _line_height(words)
    cuts = _strips(words, min_gap)
    if not cuts:
        return None
    out = []
    for ws in lines:
        cells = [[] for _ in range(len(cuts) + 1)]
        for w in ws:
            cells[sum(1 for c in cuts if (w["x0"] + w["x1"]) / 2 > c)].append(w)
        out.append([_cell_lines(c, min_gap) for c in cells])
    return out


def _line_height(words):
    """The height of a text line among these words: the median word height."""
    heights = sorted(w["bottom"] - w["top"] for w in words)
    return heights[len(heights) // 2]


def _strips(words, min_gap):
    """[x] the middles of the vertical strips of white no word crosses, each at least `min_gap` wide."""
    spans = []   # the x ranges that hold text in some word, merged
    for x0, x1 in sorted((w["x0"], w["x1"]) for w in words):
        if spans and x0 <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], x1)
        else:
            spans.append([x0, x1])
    return [(a[1] + b[0]) / 2 for a, b in zip(spans, spans[1:]) if b[0] - a[1] >= min_gap]


def _cell_lines(words, gap):
    """One cell's words as its own lines: words side by side are one line (joined by spaces), a word below the
    line before starts a new one (joined by a line break, as pdfplumber keeps a ruled cell's lines). A cell
    written on two lines ("E003 Lionel" over "eng-orders") so keeps the boundary between its two values."""
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and w["top"] - rows[-1][0] < gap:   # the same line: its top within a fraction of a line height
            rows[-1][1].append(w)
        else:
            rows.append([w["top"], [w]])
    return "\n".join(" ".join(x["text"] for x in sorted(ws, key=lambda x: x["x0"])) for _, ws in rows)


BOLD_WORDS = ("bold", "black", "heavy")   # what a font's name says when its weight is bold


def _line_style(ln):
    """(size, bold share) of a text line: its characters' median size and the share of them in a bold face."""
    chars = [c for c in ln.get("chars", ()) if str(c.get("text", "")).strip()]
    if not chars:
        return max(ln["bottom"] - ln["top"], 1.0), 0.0
    sizes = sorted(float(c.get("size") or 0) for c in chars)
    bold = sum(1 for c in chars if any(w in str(c.get("fontname", "")).lower() for w in BOLD_WORDS))
    return sizes[len(sizes) // 2], bold / len(chars)


def _plumber_page(page):
    """One page's tables [(top, Block, bbox)] and its lines outside them [{top, bottom, x0, x1, text, size, bold}].
    A table has at least two rows and two columns."""
    tables = []
    for t in page.find_tables():
        rows = [[(c or "").strip() for c in r] for r in (t.extract() or [])]
        if len(rows) >= 2 and max(len(r) for r in rows) == 1:
            rows = _columns(page, t, rows) or rows
        rows = [r for r in rows if any(r)]
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
            tables.append((t.bbox, rows))
    boxes = [b for b, _ in tables]

    def outside(obj):
        if "x0" not in obj or "top" not in obj:
            return True
        cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
        return not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)

    rest = page.filter(outside) if boxes else page
    out = []
    for bbox, rows in tables:
        width = max(len(r) for r in rows)
        out.append((bbox[1], Block("table", rows=[r + [""] * (width - len(r)) for r in rows], header=True), bbox))
    lines, words = [], rest.extract_words()
    for ln in rest.extract_text_lines():
        text = ln["text"].strip()
        if text:
            size, bold = _line_style(ln)
            lines.append({"top": ln["top"], "bottom": ln["bottom"], "x0": ln["x0"], "x1": ln["x1"], "text": text,
                          "size": size, "bold": bold, "words": _line_words(ln, text, words)})
    return out, lines


def _line_words(ln, text, words):
    """The words of one text line, each with its place, or None when they do not hold exactly the line's text
    (the line is then never read as part of a table)."""
    mine = [{k: w[k] for k in ("x0", "x1", "top", "bottom", "text")} for w in words
            if ln["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= ln["bottom"] + 1
            and ln["x0"] - 1 <= w["x0"] and w["x1"] <= ln["x1"] + 1]
    return mine if mine and sum(nonws(w["text"]) for w in mine) == nonws(text) else None


def _borderless(groups):
    """A table drawn without lines at the start of `groups` (a page's runs of paragraph lines, a run ending where
    the white between two lines is wider than a line gap): (how many runs it takes, rows, bbox, where each column
    starts), or None.

    Its columns are the vertical strips of white that run through all its lines, as for a table ruled between
    its rows only (`_strips`): prose has none, since every full line of it crosses them. A strip setting off a
    column narrower than two line heights (a list's bullets or numbers) is not taken. Its rows are the runs of
    lines, where the table sets its rows apart with white (three runs or more); otherwise, its lines evenly
    spaced, a row starts at each line with text in the first column. A cell's lines are one text, wrapped to
    the cell's width, so they are joined by spaces. Every row has text in at least two columns: the table
    ends at the first run that has not, or that would change its columns; a table has two rows at least."""
    best = None
    for end in range(1, len(groups) + 1):
        grid = _grid(groups[:end])
        if grid is not None and (best is None or len(grid[0][0]) == len(best[1][0])):
            best = (end, *grid)
        elif best is not None or end >= 3:   # a header over a first row wrapped in its first cell only takes three
            break
    return best


def _grid(groups):
    """(rows, bbox, where each column starts) of these runs of lines read as one table, or None when they are
    not one."""
    lines = [ln for g in groups for ln in g]
    if len(lines) < 2 or any(not ln["words"] for ln in lines):
        return None
    words = [w for ln in lines for w in ln["words"]]
    height = _line_height(words)
    cuts = _strips(words, 0.75 * height)
    col = lambda w: sum(1 for c in cuts if (w["x0"] + w["x1"]) / 2 > c)
    while cuts:
        spans = collections.defaultdict(list)
        for w in words:
            spans[col(w)] += [w["x0"], w["x1"]]
        narrow = [k for k in sorted(spans) if max(spans[k]) - min(spans[k]) < 2 * height]
        if not narrow:
            break
        del cuts[min(narrow[0], len(cuts) - 1)]   # the strip after it (before it, for the last column)
    if not cuts:
        return None
    if len(groups) >= 3:
        sets = groups
    else:
        sets = []
        for g in groups:
            for n, ln in enumerate(g):
                if n == 0 or any(col(w) == 0 for w in ln["words"]):
                    sets.append([])
                sets[-1].append(ln)
    rows = []
    for st in sets:
        cells = [[] for _ in range(len(cuts) + 1)]
        for ln in st:
            for w in sorted(ln["words"], key=lambda w: w["x0"]):
                cells[col(w)].append(w["text"])
        rows.append([" ".join(c) for c in cells])
    if len(rows) < 2 or any(sum(1 for c in r if c) < 2 for r in rows):
        return None
    bbox = (min(w["x0"] for w in words), min(w["top"] for w in words), max(w["x1"] for w in words),
            max(w["bottom"] for w in words))
    starts = [min(w["x0"] for w in words if col(w) == k) for k in range(len(cuts) + 1)]
    return rows, bbox, starts


# ---------------------------------------------------------------- the text flows across pages
# A PDF is laid out in pages, but what it says runs on across them. So the reader gives no section per page:
# every block carries its page (meta "page", and "page_end" when it runs onto a later one); the lines every
# page repeats (running headers and footers, page numbers) are left out; a paragraph or table a page break
# cuts is put back together; and headings are recovered from how the PDF sets them (size and weight).

DIGITS = re.compile(r"\d+")
SPACES = re.compile(r"\s+")
NO_WORD = re.compile(r"[#\W_]")
NEAR_PAGES = 3   # how many pages before and after a page its running header / footer is looked for on


def _mask(text: str) -> str:
    """A line as running headers and footers are compared: case and spacing ignored, every number the same
    (the page number in it changes from page to page)."""
    return SPACES.sub(" ", DIGITS.sub("#", text.casefold())).strip()


def _similar(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def _furniture(pages, body):
    """{(page, line index)} of the running headers, footers and page numbers. A candidate is a line at a page's
    top or bottom edge (nothing on the page above / below it but other candidates), set apart from the text
    further in by more than its own height (it sits in the margin), and not set as a heading. It is furniture when it is
    only a number, or when another page has a line at the same edge, at about the same height, with about the
    same words (numbers aside). Pages are compared with the few pages around them (a running header repeats on
    the next page, or on the one after when odd and even pages differ), so a long PDF costs no more per page."""
    cands = collections.defaultdict(list)   # page -> [(line index, edge, top, height, masked text)]
    for p, (tables, lines) in enumerate(pages):
        t_top = min((bbox[1] for _, _, bbox in tables), default=None)
        t_bot = max((bbox[3] for _, _, bbox in tables), default=None)
        order = sorted(range(len(lines)), key=lambda i: lines[i]["top"])
        for edge, seq in (("top", order[:3]), ("bottom", order[::-1][:3])):
            for k, i in enumerate(seq[:2]):
                ln = lines[i]
                if (edge == "top" and t_top is not None and ln["top"] > t_top) or \
                        (edge == "bottom" and t_bot is not None and ln["bottom"] < t_bot) or _is_heading(ln, body):
                    break
                # the white between it and what comes next further in (the next line, or a table)
                inner = [lines[seq[k + 1]]["top"] - ln["bottom"] if edge == "top" else ln["top"] - lines[seq[k + 1]]["bottom"]] \
                    if k + 1 < len(seq) else []
                if edge == "top" and t_top is not None:
                    inner.append(t_top - ln["bottom"])
                if edge == "bottom" and t_bot is not None:
                    inner.append(ln["top"] - t_bot)
                if min(inner, default=float("inf")) <= ln["bottom"] - ln["top"]:
                    break   # no wider gap than its own height: it is part of the text, not in the margin
                cands[p].append((i, edge, ln["top"], ln["bottom"] - ln["top"], _mask(ln["text"])))
    hit = set()
    for p, mine in cands.items():
        near = [c for q in range(max(0, p - NEAR_PAGES), p + NEAR_PAGES + 1) if q != p for c in cands.get(q, ())]
        for i, edge, top, h, key in mine:
            if not NO_WORD.sub("", key) or any(e2 == edge and abs(t2 - top) <= max(h, h2) and _similar(key, k2)
                                               for _, e2, t2, h2, k2 in near):
                hit.add((p, i))
    out = set()   # only an unbroken run from the edge: a line further in counts when the ones outside it do
    for p, (_, lines) in enumerate(pages):
        order = sorted(range(len(lines)), key=lambda i: lines[i]["top"])
        for seq in (order[:2], order[::-1][:2]):
            for i in seq:
                if (p, i) not in hit:
                    break
                out.add((p, i))
    return out


def _body_size(pages, skip):
    """The size most of the document's text is set in: the median over its characters."""
    sizes = sorted((round(ln["size"], 1), len(ln["text"])) for p, (_, lines) in enumerate(pages)
                   for i, ln in enumerate(lines) if (p, i) not in skip)
    total, run = sum(n for _, n in sizes), 0
    for s, n in sizes:
        run += n
        if run * 2 >= total:
            return s
    return 0.0


def _is_heading(ln, body):
    """A heading line: bold and set larger than the body text, or much larger. A bold line at the body's size
    (a table's header row, a label) is not one, nor a long line of large text."""
    if not body or len(ln["text"]) > 200:
        return False
    return (ln["bold"] >= 0.9 and ln["size"] >= 1.08 * body) or ln["size"] >= 1.3 * body


def _levels(sizes):
    """{size: heading level}: the largest size is level 1; sizes within about half a point are one level."""
    levels, last, n = {}, None, 0
    for s in sorted(set(sizes), reverse=True):
        if last is None or last - s > max(0.6, 0.05 * last):
            n, last = n + 1, s
        levels[s] = min(n, 6)
    return levels


def _page_blocks(tables, lines, body, levels, skip, p):
    """[(top, Block, layout)] of one page in reading order: its tables, its headings (a heading line, or lines
    of one heading size in a row) and its paragraphs (cut where the gap to the next line is wider than a line
    gap, or where a heading starts or ends). Paragraphs in a row that line up as a table drawn without lines
    are that table."""
    out = [(top, b, {"bbox": bbox}) for top, b, bbox in tables]
    groups, last = [], None   # [(kind, lines)]
    for i in sorted(range(len(lines)), key=lambda i: lines[i]["top"]):
        if (p, i) in skip:
            continue
        ln = lines[i]
        k = "heading" if _is_heading(ln, body) else "paragraph"
        gap = last is not None and ln["top"] - last > 0.7 * max(ln["bottom"] - ln["top"], 1.0)
        if not groups or gap or k != groups[-1][0] or \
                (k == "heading" and abs(ln["size"] - groups[-1][1][-1]["size"]) > 0.6):
            groups.append((k, []))
        groups[-1][1].append(ln)
        last = ln["bottom"]
    g = 0
    while g < len(groups):
        kind, cur = groups[g]
        if kind == "paragraph":
            run = []
            for k, ls in groups[g:]:
                if k != "paragraph":
                    break
                run.append(ls)
            table = _borderless(run)
            if table:
                n, rows, bbox, starts = table
                out.append((bbox[1], Block("table", rows=rows, header=True), {"bbox": bbox, "starts": starts}))
                g += n
                continue
        if kind == "heading":
            b = Block("heading", " ".join(ln["text"] for ln in cur), level=levels.get(round(cur[0]["size"], 1), 6))
        else:
            b = Block("paragraph", "\n".join(ln["text"] for ln in cur))
        out.append((cur[0]["top"], b, {"lines": list(cur)}))
        g += 1
    out.sort(key=lambda x: x[0])
    return out


def _continues(prev, nxt, right):
    """Does a page's last block run on into the next page's first one? A paragraph does when its last line
    reaches the right edge of the text (a paragraph's own last line stops short of it) and the next page goes
    on in the same type size; a table does when the next page starts with a table of the same columns (for one
    drawn without lines, its columns starting at the same places: its right edge is only where its longest
    text ends)."""
    (_, a, la), (_, b, lb) = prev, nxt
    if a.kind == "paragraph" and b.kind == "paragraph":
        end, start = la["lines"][-1], lb["lines"][0]
        char = (end["x1"] - end["x0"]) / max(len(end["text"]), 1)
        return end["x1"] >= right - 3 * char and abs(end["size"] - start["size"]) <= 0.6
    if a.kind == "table" and b.kind == "table":
        ba, bb = la["bbox"], lb["bbox"]
        width = max(ba[2] - ba[0], 1.0)
        if len(a.rows[0]) != len(b.rows[0]):
            return False
        if "starts" in la and "starts" in lb:
            return all(abs(x - y) <= 0.05 * width for x, y in zip(la["starts"], lb["starts"]))
        return abs(ba[0] - bb[0]) <= 0.05 * width and abs(ba[2] - bb[2]) <= 0.05 * width
    return False


def _join(prev, nxt, page):
    """Put the next page's first block onto the end of this one."""
    (_, a, la), (_, b, lb) = prev, nxt
    if a.kind == "paragraph":
        a.text += "\n" + b.text
        la["lines"] = la["lines"] + lb["lines"]
    else:
        rows = b.rows
        if [cell_text(c).casefold() for c in rows[0]] == [cell_text(c).casefold() for c in a.rows[0]]:
            rows = rows[1:]   # its header row, repeated on the new page
        a.rows = a.rows + rows
        la["bbox"] = lb["bbox"]
    a.meta["page_end"] = page


def _flow(pages, q):
    """Every page's (tables, lines) -> the document's blocks, flowing across the page breaks. A page read as
    plain text (a list of blocks: its layout could not be read) is taken as it is."""
    plain = {p: x for p, x in enumerate(pages) if isinstance(x, list)}
    pages = [([], []) if p in plain else x for p, x in enumerate(pages)]
    skip = _furniture(pages, _body_size(pages, set()))
    if skip:
        q.dropped["running headers, footers and page numbers (lines)"] += len(skip)
    body = _body_size(pages, skip)
    levels = _levels([round(ln["size"], 1) for p, (_, lines) in enumerate(pages) for i, ln in enumerate(lines)
                      if (p, i) not in skip and _is_heading(ln, body)])
    rights = sorted(ln["x1"] for p, (_, lines) in enumerate(pages) for i, ln in enumerate(lines) if (p, i) not in skip)
    right = rights[int(0.95 * (len(rights) - 1))] if rights else 0.0   # the text's right edge (a few lines stick out)
    blocks = []
    for p, (tables, lines) in enumerate(pages):
        here = [(0, b, {}) for b in plain[p]] if p in plain else _page_blocks(tables, lines, body, levels, skip, p)
        for _, b, _ in here:
            b.meta["page"] = p + 1
        if here and blocks and p not in plain and blocks[-1][2] and blocks[-1][1].meta.get("page_end", blocks[-1][1].meta["page"]) == p \
                and _continues(blocks[-1], here[0], right):
            _join(blocks[-1], here.pop(0), p + 1)
        blocks.extend(here)
    return [b for _, b, _ in blocks]


def read_pdf(data: bytes, title=None, ocr=None) -> DocText:
    try:
        import pypdf
    except ImportError:
        pypdf = None
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pypdf is None and pdfplumber is None:
        raise Unsupported("no PDF library installed (pypdf or pdfplumber)")
    doc = DocText(title=title, format="pdf")
    q = doc.quality
    reader = plumb = None
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise Unreadable("encrypted PDF")
            _meta(doc, dict(reader.metadata or {}))
        except Unreadable:
            raise
        except Exception as e:
            if pdfplumber is None:
                raise Unreadable(f"damaged PDF ({type(e).__name__})") from e
            reader = None
    if pdfplumber is not None:
        try:
            plumb = pdfplumber.open(io.BytesIO(data))
            if reader is None:
                _meta(doc, dict(plumb.metadata or {}))
        except Exception as e:
            if reader is None:
                raise Unreadable(f"damaged PDF ({type(e).__name__})") from e
            plumb = None
            q.warn(f"tables not read: pdfplumber could not open the file ({type(e).__name__})")
    else:
        q.warn("tables read as text lines (pdfplumber is not installed)")
    try:
        n_pages = len(plumb.pages) if plumb is not None else len(reader.pages)
        pages = []   # per page: (tables, lines) when its layout was read, else its blocks as plain text
        for i in range(n_pages):
            q.pages += 1
            page = None
            if plumb is not None:
                try:
                    page = _plumber_page(plumb.pages[i])
                    if not page[0] and not page[1]:
                        page = None
                except Exception as e:   # one bad page: fall back to plain text for it
                    q.warn(f"page {i + 1}: layout not read ({type(e).__name__}), plain text used")
            if page is None:
                try:
                    text = (reader.pages[i].extract_text() or "") if reader is not None else ""
                    page = [Block("paragraph", p) for p in _paragraphs(text)]
                except Exception as e:
                    q.failed_parts += 1
                    q.warn(f"page {i + 1}: not readable ({type(e).__name__})")
                    pages.append([])
                    continue
            if not page and ocr is not None:
                text = ocr(data, i) or ""
                page = [Block("paragraph", p) for p in _paragraphs(text)]
                if page:
                    q.ocr_pages += 1
            if not page:
                q.empty_pages += 1
            pages.append(page)
        doc.blocks = _flow(pages, q)
        for b in doc.blocks:
            q.source_chars += nonws(b.text) if b.kind != "table" else sum(nonws(c) for r in b.rows for c in r)
    finally:
        if plumb is not None:
            plumb.close()
    if q.empty_pages:
        q.warn(f"{q.empty_pages} of {q.pages} pages have no text layer (scanned or image-only): OCR needed")
    return doc.finish()
