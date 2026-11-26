"""Behaviour tests for the document readers (ekg_adapters.doctext) and the document emitter (docgraph).

Small fixtures are built in a temp folder for every format (docx / pptx / xlsx with zipfile, so the
test needs no Office library); the PDF checks read the real PDFs next to the eval set when they are
there. The checks are about loss: nothing a document holds may go missing on the way to pieces.

    python -I ekg-eval-data/test_doctext.py
"""

import datetime as dt
import io
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters import common, docgraph, docs, linker  # noqa: E402
from ekg_adapters.access import place  # noqa: E402
from ekg_adapters.common import Graph  # noqa: E402
from ekg_adapters.doctext import (Block, DocText, Unsupported, read, read_confluence, read_csv, read_docx,  # noqa: E402
                                  read_html, read_markdown, read_pdf, read_pptx, read_text, read_xlsx)

QM = HERE.parent / "query-master"
PDF_DIR = HERE.parent.parent / "kyro-knowledge-eval"

CONFLUENCE = """
<ac:structured-macro ac:name="toc"><ac:parameter ac:name="maxLevel">3</ac:parameter></ac:structured-macro>
<h1>Design</h1>
<p>Current state: <ac:structured-macro ac:name="status"><ac:parameter ac:name="colour">Green</ac:parameter>
<ac:parameter ac:name="title">Accepted</ac:parameter></ac:structured-macro></p>
<p>See <ac:link><ri:page ri:content-title="Rollout plan" ri:space-key="ENG"/><ac:plain-text-link-body><![CDATA[the plan]]></ac:plain-text-link-body></ac:link>,
the file <ac:link><ri:attachment ri:filename="budget.xlsx"/></ac:link>, ask <ac:link><ri:user ri:userkey="u-bob"/></ac:link>
and track <ac:structured-macro ac:name="jira"><ac:parameter ac:name="server">Tracker</ac:parameter>
<ac:parameter ac:name="serverId">abc-123-server</ac:parameter><ac:parameter ac:name="key">ENG-1</ac:parameter></ac:structured-macro>.
Mail <a href="mailto:carol@acme.io">Carol</a> or read <a href="https://example.org/spec">the spec</a>.</p>
<ac:image ac:alt="flow"><ri:attachment ri:filename="flow.png"/></ac:image>
<h2>Config</h2>
<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">java</ac:parameter><ac:parameter ac:name="linenumbers">true</ac:parameter>
<ac:plain-text-body><![CDATA[int[] a = b[c[0]]]]><![CDATA[>;
retry.count = 3]]></ac:plain-text-body></ac:structured-macro>
<table><tbody><tr><th>Name</th><th>Default</th><th>Notes</th></tr>
<tr><td>retries</td><td>3</td><td><p>first line</p><p>second line</p></td></tr>
<tr><td>timeout</td><td></td><td>none</td></tr></tbody></table>
<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">Heads up</ac:parameter>
<ac:rich-text-body><p>Restart after changing it.</p></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="shiny-new-thing"><ac:parameter ac:name="mode">hidden-setting</ac:parameter>
<ac:rich-text-body><p>Body of an unknown macro.</p></ac:rich-text-body></ac:structured-macro>
<ac:task-list><ac:task><ac:task-id>7</ac:task-id><ac:task-status>complete</ac:task-status><ac:task-body>ship it</ac:task-body></ac:task>
<ac:task><ac:task-id>8</ac:task-id><ac:task-status>incomplete</ac:task-status><ac:task-body>write docs</ac:task-body></ac:task></ac:task-list>
<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>
"""

MARKDOWN = """---
title: Retry handbook
owner: Bob Kumar
status: Draft
date: 2026-03-02
tags: [payments, retries]
reviewers:
  - alice@acme.io
---
# Retries

Refunds are retried once. See [the runbook](runbook.md) and <https://example.org/a>.
![diagram](img/flow.png)

## Settings

| key | default | note |
|-----|---------|------|
| retries | 3 | |
| timeout |  | seconds |

```python
def retry(n):
    return n + 1
```

- [x] done item
- [ ] open item
  1. nested step

> quoted line

Setext title
------------
Tail paragraph mentioning ENG-1 and [a ref link][spec].

[spec]: https://example.org/spec
"""

RST = """Guide
=====

Intro paragraph.

Usage
-----

.. code-block:: bash

   make build
   make test

Closing words.
"""


def _zip(parts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return buf.getvalue()


RELS = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'
REL = '<Relationship Id="{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{}" Target="{}"{}/>'
CORE = ('<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
        '<dc:title>Payments handbook</dc:title><dc:creator>Alice Ng</dc:creator><cp:lastModifiedBy>Bob Kumar</cp:lastModifiedBy>'
        '<dcterms:created>2026-01-05T09:00:00Z</dcterms:created><dcterms:modified>2026-02-01T10:30:00+02:00</dcterms:modified>'
        '</cp:coreProperties>')
APP = ('<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
       '<Application>Microsoft Office Word</Application><Company>ACME Corp</Company></Properties>')
ROOT_RELS = RELS.format(REL.format("rId1", "officeDocument", "{}", ""))


def make_docx() -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    p = lambda text, ppr="": f"<w:p>{('<w:pPr>' + ppr + '</w:pPr>') if ppr else ''}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"
    cell = lambda text, span="": f"<w:tc>{span}{p(text) if text else '<w:p/>'}</w:tc>"
    body = (
        p("Payments handbook", '<w:pStyle w:val="Title"/>')
        + p("Refund rules", '<w:pStyle w:val="Heading1"/>')
        + '<w:p><w:r><w:t xml:space="preserve">Read </w:t></w:r><w:hyperlink r:id="rId9"><w:r><w:t>the policy</w:t></w:r></w:hyperlink>'
          '<w:del><w:r><w:delText>DELETED WORDS</w:delText></w:r></w:del><w:ins><w:r><w:t> carefully</w:t></w:r></w:ins></w:p>'
        + p("first bullet", '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        + p("step one", '<w:numPr><w:ilvl w:val="1"/><w:numId w:val="2"/></w:numPr>')
        + p("Limits", '<w:pStyle w:val="Custom2"/>')
        + '<w:tbl><w:tblPr><w:tblLook w:firstRow="1"/></w:tblPr>'
          f'<w:tr>{cell("Tier")}{cell("Limit")}{cell("Owner")}</w:tr>'
          f'<w:tr>{cell("gold")}{cell("")}{cell("bob")}</w:tr>'
          f'<w:tr>{cell("merged", "<w:tcPr><w:gridSpan w:val=\"2\"/></w:tcPr>")}{cell("carol")}</w:tr></w:tbl>'
        + p("Closing words about ENG-1."))
    styles = (f'<w:styles {w}><w:style w:styleId="Title"><w:name w:val="Title"/></w:style>'
              '<w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
              '<w:style w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
              '<w:style w:styleId="Custom2"><w:name w:val="Section title"/><w:basedOn w:val="Heading2"/></w:style></w:styles>')
    numbering = (f'<w:numbering {w}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
                 '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl><w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
                 '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>')
    comments = (f'<w:comments {w}><w:comment w:id="0" w:author="Dave Lee" w:date="2026-01-20T08:00:00Z">'
                '<w:p><w:r><w:t>Is the gold limit right?</w:t></w:r></w:p></w:comment></w:comments>')
    return _zip({
        "_rels/.rels": ROOT_RELS.format("word/document.xml"),
        "word/document.xml": f"<w:document {w}><w:body>{body}</w:body></w:document>",
        "word/_rels/document.xml.rels": RELS.format(
            REL.format("rId1", "styles", "styles.xml", "") + REL.format("rId2", "numbering", "numbering.xml", "")
            + REL.format("rId3", "comments", "comments.xml", "")
            + REL.format("rId9", "hyperlink", "https://example.org/policy", ' TargetMode="External"')),
        "word/styles.xml": styles, "word/numbering.xml": numbering, "word/comments.xml": comments,
        "docProps/core.xml": CORE, "docProps/app.xml": APP})


def make_pptx() -> bytes:
    ns = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
    sp = lambda ph, paras: (f'<p:sp><p:nvSpPr><p:nvPr>{ph}</p:nvPr></p:nvSpPr><p:txBody>'
                            + "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paras) + "</p:txBody></p:sp>")
    tc = lambda t: f"<a:tc><a:txBody><a:p><a:r><a:t>{t}</a:t></a:r></a:p></a:txBody></a:tc>"
    table = ('<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tblPr firstRow="1"/>'
             f'<a:tr>{tc("Quarter")}{tc("Revenue")}</a:tr><a:tr>{tc("Q1")}{tc("120")}</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    slide = lambda shapes: f"<p:sld {ns}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"
    notes = f'<p:notes {ns}><p:cSld><p:spTree>{sp("<p:ph type=\"body\"/>", ["Remember to thank the team."])}</p:spTree></p:cSld></p:notes>'
    return _zip({
        "_rels/.rels": ROOT_RELS.format("ppt/presentation.xml"),
        "ppt/presentation.xml": f'<p:presentation {ns}><p:sldIdLst><p:sldId id="256" r:id="rId7"/><p:sldId id="257" r:id="rId3"/></p:sldIdLst></p:presentation>',
        # the deck's first slide is stored as slide2.xml: order comes from the relationships, not the file names
        "ppt/_rels/presentation.xml.rels": RELS.format(REL.format("rId3", "slide", "slides/slide1.xml", "")
                                                       + REL.format("rId7", "slide", "slides/slide2.xml", "")),
        "ppt/slides/slide2.xml": slide(sp('<p:ph type="title"/>', ["Opening"]) + sp('<p:ph type="body" idx="1"/>', ["Why now", "What changes"])),
        "ppt/slides/slide1.xml": slide(sp('<p:ph type="title"/>', ["Numbers"]) + table),
        "ppt/slides/_rels/slide1.xml.rels": RELS.format(REL.format("rId1", "notesSlide", "../notesSlides/notesSlide1.xml", "")),
        "ppt/notesSlides/notesSlide1.xml": notes, "docProps/core.xml": CORE, "docProps/app.xml": APP})


DAY = (dt.date(2026, 3, 15) - dt.date(1899, 12, 30)).days


def make_xlsx(date1904=False) -> bytes:
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    shared = (f'<sst {ns}><si><t>Item</t></si><si><t>Owner</t></si><si><t>Due</t></si>'
              '<si><r><t>Dynamic </t></r><r><rPr><b/></rPr><t>shapes</t></r><rPh><t>PHONETIC</t></rPh></si><si><t>carol</t></si></sst>')
    styles = (f'<styleSheet {ns}><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm"/></numFmts>'
              '<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs></styleSheet>')
    plan = (f'<worksheet {ns}><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="inlineStr"><is><t>Cost</t></is></c></row>'
            # row 2 has no B cell: the owner is unknown, and the date must stay under "Due"
            f'<row r="2"><c r="A2" t="s"><v>3</v></c><c r="C2" s="1"><v>{DAY}</v></c><c r="D2"><f>SUM(E1:E9)</f><v>42</v></c></row>'
            f'<row r="4"><c r="A4" t="str"><v>#99 follow-up</v></c><c r="B4" t="s"><v>4</v></c><c r="C4" s="2"><v>{DAY}.75</v></c><c r="D4" t="b"><v>1</v></c></row>'
            '</sheetData><mergeCells count="1"><mergeCell ref="A6:B6"/></mergeCells></worksheet>')
    notes = f'<worksheet {ns}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Only a note</t></is></c><c r="B1"><v>0.30000000000000004</v></c></row></sheetData></worksheet>'
    return _zip({
        "_rels/.rels": ROOT_RELS.format("xl/workbook.xml"),
        # "Plan" is the first tab but lives in sheet2.xml: only the workbook relationships say so
        "xl/workbook.xml": f'<workbook {ns}>{"<workbookPr date1904=\"1\"/>" if date1904 else ""}<sheets><sheet name="Plan" sheetId="2" r:id="rId2"/>'
                           '<sheet name="Notes" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": RELS.format(
            REL.format("rId1", "worksheet", "worksheets/sheet1.xml", "") + REL.format("rId2", "worksheet", "worksheets/sheet2.xml", "")
            + REL.format("rId3", "sharedStrings", "sharedStrings.xml", "") + REL.format("rId4", "styles", "styles.xml", "")),
        "xl/worksheets/sheet1.xml": notes, "xl/worksheets/sheet2.xml": plan,
        "xl/sharedStrings.xml": shared, "xl/styles.xml": styles, "docProps/core.xml": CORE, "docProps/app.xml": APP})


def blank_pdf(pages=1) -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def text_pdf(pages) -> bytes:
    """A PDF written by hand: `pages` is a list of pages, each a list of (y, size, bold, text) lines set at x=50
    in Helvetica (Helvetica-Bold when bold) on a 612 x 792 page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"]
    kids = []
    for lines in pages:
        esc = lambda t: t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        body = "".join(f"BT /{'F2' if bold else 'F1'} {size} Tf 50 {y} Td ({esc(t)}) Tj ET\n" for y, size, bold, t in lines)
        objs.append(f"<< /Length {len(body.encode('latin-1'))} >>\nstream\n{body}endstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def flowing_pdf() -> bytes:
    """Three pages with a running header and footer (the footer numbered), headings in bold at two sizes, a
    paragraph that runs from page 1 onto page 2, and one that ends just before page 3 starts a new one."""
    full = "flowing words that fill this whole line of the page from margin to margin, and more of them"
    head = lambda n: [(760, 8, False, "Handbook of the example team"), (30, 8, False, f"Example handbook | page {n}")]
    p1 = head(1) + [(700, 16, True, "Overview"), (676, 10, False, "The opening paragraph starts here."),
                    (650, 10, False, full), (636, 10, False, full), (622, 10, False, full)]
    p2 = head(2) + [(720, 10, False, "and the sentence ends on the next page."),
                    (690, 13, True, "Details"), (666, 10, False, full), (652, 10, False, "A short last line.")]
    p3 = head(3) + [(720, 10, False, "A new paragraph on page three."), (690, 13, True, "More details"),
                    (666, 10, False, "Text under the second sub-heading.")]
    return text_pdf([p1, p2, p3])


def new_graph():
    g = Graph(observed_at="2026-11-01T00:00:00Z")
    g.ctx["settings"] = {"issue_tracker": None, "public": True}
    g.ctx["ticket_prefixes"] = {"ENG"}
    return g


def tiles(doc, pieces):
    """The pieces' spans, laid end to end, are the version's text; and each piece's text is what its span holds."""
    spans = [tuple(int(x) for x in p["props"]["span"].split("-")) for p in pieces]
    joined = "".join(doc.text[s:e] for s, e in spans)
    in_order = all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))
    return joined == doc.text and in_order and (not spans or spans[0][0] == 0) \
        and [p["props"]["order"] for p in pieces] == list(range(len(pieces)))


def reader_checks(check, tmp: Path):
    # ---- Confluence storage format
    c = read_confluence(CONFLUENCE, users={"u-bob": "Bob Kumar"})
    code = [b for b in c.blocks if b.kind == "code"]
    check("confluence: a CDATA code block is kept whole, with its language (and a split ']]>' rejoined)",
          len(code) == 1 and code[0].lang == "java" and code[0].text == "int[] a = b[c[0]]>;\nretry.count = 3")
    refs = {(r.kind, r.target) for r in c.refs()}
    check("confluence: page link, attachment, image, user mention, ticket macro, address and email are refs",
          {("page", "Rollout plan"), ("attachment", "budget.xlsx"), ("image", "flow.png"), ("user", "u-bob"),
           ("ticket", "ENG-1"), ("url", "https://example.org/spec"), ("email", "carol@acme.io")} <= refs)
    page = next(r for r in c.refs("page"))
    check("confluence: a page link keeps its space and its words, and the text says where it points",
          page.extra.get("space") == "ENG" and page.text == "the plan" and "the plan <page: Rollout plan>" in c.text)
    check("confluence: a mention shows the person's name, an attachment its file name, an image its file",
          "@Bob Kumar" in c.text and "budget.xlsx" in c.text and "[image: flow.png]" in c.text)
    table = next(b for b in c.blocks if b.kind == "table")
    check("confluence: table cells are separated, the header row is known, an empty cell keeps its place",
          table.header and table.rows[0] == ["Name", "Default", "Notes"] and table.rows[2] == ["timeout", "", "none"]
          and "Name | Default | Notes" in c.text and "timeout |  | none" in c.text)
    check("confluence: paragraphs inside one cell stay in that cell", table.rows[1][2] == "first line / second line")
    check("confluence: macro settings are not text (toc, colour, server, linenumbers, an unknown macro's)",
          not any(w in c.text for w in ("maxLevel", "Green", "abc-123-server", "Tracker", "true", "hidden-setting"))
          and c.quality.dropped["macro parameter"] >= 6 and c.quality.dropped["macro toc"] == 1)
    check("confluence: parameters that are content are kept (status word, ticket key, panel title)",
          "Current state: Accepted" in c.text and "track ENG-1" in c.text and "Heads up" in c.text
          and "Restart after changing it." in c.text)
    check("confluence: an unknown macro keeps its body and is counted",
          "Body of an unknown macro." in c.text and c.quality.unknown["macro shiny-new-thing"] == 1)
    items = [b for b in c.blocks if b.kind == "list_item"]
    check("confluence: task list with its ticks, nested list with its depth",
          [(b.text, b.meta.get("checked")) for b in items[:2]] == [("ship it", True), ("write docs", False)]
          and [(b.text, b.level) for b in items[2:]] == [("one", 1), ("nested", 2), ("two", 1)]
          and "[x] ship it" in c.text and "  - nested" in c.text)
    check("confluence: headings keep their level", [(b.text, b.level) for b in c.blocks if b.kind == "heading"]
          == [("Design", 1), ("Config", 2)])
    check("confluence: every block's offsets point at its own rendering",
          all(c.text[b.start:b.end] == b.render() for b in c.blocks) and c.quality.ratio == 1.0)
    nested = read_confluence("<table><tr><td><h2>Left</h2><ac:structured-macro ac:name=\"code\"><ac:plain-text-body>"
                             "<![CDATA[x = 1\ny = 2]]></ac:plain-text-body></ac:structured-macro></td><td><p>Right side</p></td></tr></table>")
    check("confluence: a layout table (cells holding headings or code) is read as flow, code intact",
          [b.kind for b in nested.blocks] == ["heading", "code", "paragraph"] and nested.blocks[1].text == "x = 1\ny = 2")

    # ---- HTML
    h = read_html(b"<html><head><title>Guide &amp; notes</title><style>p{color:red}</style></head><body><h1>Top</h1>"
                  b"<p>Go <a href='https://example.org/x'>here</a> or <a href='other.html'>there</a>.<br>Next line</p>"
                  b"<pre><code class='language-python'>a = 1\n  b = 2</code></pre><script>var hidden = 1;</script>"
                  b"<ol><li>first<li>second</ol></body></html>")
    check("html: title, heading, links (outside and relative), code with its language, script and style left out",
          h.title == "Guide & notes" and h.blocks[0].kind == "heading"
          and {("url", "https://example.org/x"), ("file", "other.html")} <= {(r.kind, r.target) for r in h.refs()}
          and any(b.kind == "code" and b.lang == "python" and b.text == "a = 1\n  b = 2" for b in h.blocks)
          and "hidden" not in h.text and "color" not in h.text and "1. first" in h.text and "2. second" in h.text)

    # ---- Markdown
    m = read_markdown(MARKDOWN.encode("utf-8"))
    fm = m.meta.get("front_matter", {})
    check("markdown: front matter is parsed into meta, not into the text",
          fm.get("owner") == "Bob Kumar" and fm.get("status") == "Draft" and fm.get("date") == "2026-03-02"
          and fm.get("tags") == ["payments", "retries"] and fm.get("reviewers") == ["alice@acme.io"]
          and m.title == "Retry handbook" and "owner:" not in m.text)
    check("markdown: headings (ATX and setext) with levels",
          [(b.text, b.level) for b in m.blocks if b.kind == "heading"] == [("Retries", 1), ("Settings", 2), ("Setext title", 2)])
    mt = next(b for b in m.blocks if b.kind == "table")
    check("markdown: a table with empty cells keeps its columns",
          mt.header and mt.rows == [["key", "default", "note"], ["retries", "3", ""], ["timeout", "", "seconds"]])
    check("markdown: fenced code with its language, indentation intact",
          any(b.kind == "code" and b.lang == "python" and b.text == "def retry(n):\n    return n + 1" for b in m.blocks))
    mrefs = {(r.kind, r.target) for r in m.refs()}
    check("markdown: links, images, autolinks and reference links are refs",
          {("file", "runbook.md"), ("url", "https://example.org/a"), ("image", "img/flow.png"),
           ("url", "https://example.org/spec")} <= mrefs)
    check("markdown: task items, a nested ordered item, a quote",
          [(b.text, b.meta.get("checked")) for b in m.blocks if b.kind == "list_item"][:2] == [("done item", True), ("open item", False)]
          and any(b.kind == "list_item" and b.level == 2 and b.meta.get("ordered") for b in m.blocks)
          and any(b.kind == "quote" and b.text == "quoted line" for b in m.blocks))

    # ---- plain text and rst
    r = read_text(RST, fmt="rst")
    check("rst: underlined headings by level, a code-block directive as code",
          [(b.text, b.level) for b in r.blocks if b.kind == "heading"] == [("Guide", 1), ("Usage", 2)]
          and any(b.kind == "code" and b.lang == "bash" and b.text == "make build\nmake test" for b in r.blocks)
          and "Closing words." in r.text)
    t = read("first paragraph\nsame paragraph\n\nsecond", name="notes.txt")
    check("text: paragraphs split on blank lines, nothing else changed",
          [b.text for b in t.blocks] == ["first paragraph\nsame paragraph", "second"])

    # ---- docx
    d = read_docx(make_docx())
    check("docx: headings by style name (and through basedOn), not by guessing",
          [(b.text, b.level) for b in d.blocks if b.kind == "heading"] == [("Payments handbook", 1), ("Refund rules", 1), ("Limits", 2)])
    check("docx: a hyperlink is a ref and shown; tracked deletions are left out, insertions kept",
          ("url", "https://example.org/policy") in {(x.kind, x.target) for x in d.refs()}
          and "Read the policy <https://example.org/policy> carefully" in d.text and "DELETED" not in d.text
          and d.quality.dropped["tracked deletion"] == 1)
    check("docx: bullets and numbered steps with their depth",
          [(b.text, b.level, bool(b.meta.get("ordered"))) for b in d.blocks if b.kind == "list_item"]
          == [("first bullet", 1, False), ("step one", 2, True)])
    dtab = next(b for b in d.blocks if b.kind == "table")
    check("docx: table with a header row, an empty cell and a merged cell, columns aligned",
          dtab.header and dtab.rows == [["Tier", "Limit", "Owner"], ["gold", "", "bob"], ["merged", "", "carol"]])
    check("docx: core and app properties (creator, last modified by, dates in UTC, application, company)",
          d.meta == {"creator": "Alice Ng", "last_modified_by": "Bob Kumar", "created_at": "2026-01-05T09:00:00Z",
                     "modified_at": "2026-02-01T08:30:00Z", "application": "Microsoft Office Word", "company": "ACME Corp"}
          and d.title == "Payments handbook")
    check("docx: review comments with author and time",
          d.comments == [{"id": "0", "author": "Dave Lee", "at": "2026-01-20T08:00:00Z", "text": "Is the gold limit right?"}])

    # ---- pptx
    p = read_pptx(make_pptx())
    slides = [b for b in p.blocks if b.kind == "container"]
    check("pptx: one container per slide, in the deck's order (not the file names'), with titles",
          [(b.container, b.meta["index"], b.text) for b in slides] == [("slide", 1, "Opening"), ("slide", 2, "Numbers")])
    check("pptx: body text as list items, a table as a table, speaker notes under their own heading",
          [b.text for b in p.blocks if b.kind == "list_item"] == ["Why now", "What changes"]
          and next(b for b in p.blocks if b.kind == "table").rows == [["Quarter", "Revenue"], ["Q1", "120"]]
          and any(b.kind == "heading" and b.text == "Speaker notes" for b in p.blocks)
          and "Remember to thank the team." in p.text and p.quality.slides == 2 and p.meta.get("creator") == "Alice Ng")

    # ---- xlsx
    x = read_xlsx(make_xlsx())
    sheets = [b.text for b in x.blocks if b.kind == "container"]
    tabs = [b for b in x.blocks if b.kind == "table"]
    check("xlsx: sheet names matched to sheet files through the workbook relationships (reordered tabs)",
          sheets == ["Plan", "Notes"] and tabs[0].rows[0] == ["Item", "Owner", "Due", "Cost"] and tabs[1].rows[0][0] == "Only a note")
    check("xlsx: a missing cell leaves its column empty instead of shifting the row left",
          tabs[0].rows[1] == ["Dynamic shapes", "", "2026-03-15", "42"])
    check("xlsx: date-formatted cells are ISO dates (built-in and custom formats), not serial numbers",
          tabs[0].rows[1][2] == "2026-03-15" and tabs[0].rows[2][2] == "2026-03-15T18:00:00" and str(DAY) not in x.text)
    check("xlsx: rich-text shared strings joined (phonetic guide left out), formula's stored value, booleans, float noise",
          tabs[0].rows[1][0] == "Dynamic shapes" and "PHONETIC" not in x.text and tabs[0].rows[1][3] == "42"
          and tabs[0].rows[2] == ["#99 follow-up", "carol", "2026-03-15T18:00:00", "TRUE"] and tabs[1].rows[0][1] == "0.3")
    check("xlsx: merged ranges are noted", tabs[0].meta.get("merged") == ["A6:B6"] and x.quality.sheets == 2)
    x04 = read_xlsx(make_xlsx(date1904=True))
    check("xlsx: the 1904 date system is honoured",
          next(b for b in x04.blocks if b.kind == "table").rows[1][2] == (dt.date(1904, 1, 1) + dt.timedelta(days=DAY)).isoformat())

    # ---- csv / tsv
    cs = read_csv("name;team;note\nBob;payments;\"likes; semicolons\"\nCarol;;new\n".encode("utf-8-sig"))
    check("csv: the delimiter is sniffed, a byte-order mark is not text, empty cells keep their place",
          cs.blocks[0].rows == [["name", "team", "note"], ["Bob", "payments", "likes; semicolons"], ["Carol", "", "new"]]
          and "Carol |  | new" in cs.text)
    latin = read("name,city\nJos\xe9,M\xfcnchen\n".encode("cp1252"), name="people.csv")
    check("csv: bytes that are not UTF-8 are read as Windows-1252, and that is counted",
          latin.blocks[0].rows[1] == ["José", "München"] and latin.quality.dropped["not UTF-8: read as Windows-1252"] == 1)
    ts = read(b"a\tb\n1\t2\n", name="x.tsv")
    check("tsv: tabs", ts.format == "tsv" and ts.blocks[0].rows == [["a", "b"], ["1", "2"]])

    # ---- dispatch
    (tmp / "note.md").write_text("# T\n\nbody", encoding="utf-8")
    try:
        read(b"\x00\x01", name="blob.bin")
        unsupported = False
    except Unsupported:
        unsupported = True
    check("read(): a path or bytes with a name; a format with no reader says so",
          read(tmp / "note.md").format == "markdown" and read(make_docx(), "a.docx").format == "docx"
          and read(make_xlsx(), mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").format == "xlsx"
          and unsupported)

    # ---- pdf
    pdfs = sorted(PDF_DIR.glob("*.pdf")) if PDF_DIR.is_dir() else []
    if pdfs:
        import pypdf
        total_tables = 0
        for path in pdfs:
            data = path.read_bytes()
            doc = read(data, path.name)
            pages = {b.meta.get("page") for b in doc.blocks}
            n_pages = len(pypdf.PdfReader(io.BytesIO(data)).pages)
            # pypdf's words are all there, but for the running headers and footers: a word left out is on every page
            texts = [pg.extract_text() or "" for pg in pypdf.PdfReader(io.BytesIO(data)).pages]
            words = [w for w in " ".join(texts).split() if len(w) > 3]
            flat = "".join(doc.text.split())
            found = sum(1 for w in words if w in flat or all(w in t for t in texts))
            ok = (pages == set(range(1, n_pages + 1)) and doc.quality.pages == n_pages and doc.quality.empty_pages == 0
                  and not any(b.kind == "container" for b in doc.blocks)
                  and found >= 0.99 * len(words) and doc.meta.get("creator") and doc.meta.get("created_at") and doc.title)
            total_tables += doc.quality.tables
            check(f"pdf: {path.name}: every block says its page, no page is a section, {found}/{len(words)} of pypdf's "
                  f"words present, {doc.quality.tables} tables, author and dates read", ok)
        tabled = read(next(p for p in pdfs if "GitHub_Engineering" in p.name))
        tb = next(b for b in tabled.blocks if b.kind == "table")
        check("pdf: a ruled table is a table (rows of cells), rendered a | b | c",
              total_tables > 0 and len(tb.rows) >= 2 and len(tb.rows[0]) >= 2 and " | ".join(tb.rows[0]) in tabled.text)
    else:
        check(f"pdf: real PDFs found in {PDF_DIR}", False)
    ldap = HERE.parent / "ekg-eval-raw" / "acme-docs" / "docs" / "ACME_LDAP_Employee_Directory.pdf"
    if ldap.exists():
        # a table ruled between its rows only, each cell written on two lines: the columns come from the white
        # strips, and a cell keeps its own two lines ("E003 Lionel" over "eng-orders"): two values, not one name
        ldoc = read(ldap.read_bytes(), ldap.name)
        tb = next(b for b in ldoc.blocks if b.kind == "table")
        row = next(r for r in tb.rows if r[0].startswith("E003"))
        check("pdf: a cell written on two lines keeps its line break (id and name over the team), as a ruled cell does",
              row[0] == "E003 Lionel\neng-orders" and row[1] == "Software Engineer\nOrder Management" and row[2] == "Mahesh"
              and row[3] == "@lionel\nlionel@acme.com" and "E003 Lionel / eng-orders | Software Engineer / Order Management" in ldoc.text)
    flow = read_pdf(flowing_pdf())
    fb = flow.blocks
    joined = next((b for b in fb if b.text.startswith("flowing words")), None)
    check("pdf: a running header and a numbered footer repeated on every page are left out, and counted",
          "Handbook of the example team" not in flow.text and "Example handbook" not in flow.text
          and sum(flow.quality.dropped.values()) == 6 and flow.quality.ratio == 1.0)
    check("pdf: headings come from the type (bold and larger than the body), levelled by size",
          [(b.text, b.level) for b in fb if b.kind == "heading"] == [("Overview", 1), ("Details", 2), ("More details", 2)])
    check("pdf: a paragraph whose last line reaches the margin runs on across the page break (one block, pages 1-2)",
          joined is not None and joined.text.endswith("and the sentence ends on the next page.")
          and joined.meta == {"page": 1, "page_end": 2})
    check("pdf: a paragraph whose last line stops short ends at the page break",
          any(b.text == "A new paragraph on page three." and b.meta == {"page": 3} for b in fb)
          and any(b.text.endswith("A short last line.") and "page_end" not in b.meta for b in fb))
    chapters = read_pdf(text_pdf([[(760, 8, False, "Running title"), (700, 16, True, f"Chapter {n}"),
                                   (684, 10, False, "Same closing words on every page.")] for n in range(1, 4)]))
    check("pdf: a numbered heading at the same place on every page, and a body line repeated next to the text, are kept",
          [b.text for b in chapters.blocks if b.kind == "heading"] == ["Chapter 1", "Chapter 2", "Chapter 3"]
          and chapters.text.count("Same closing words on every page.") == 3 and "Running title" not in chapters.text)
    sections, pieces = docgraph.split_pieces(flow, 4000)
    check("pdf: sections are the headings, not the pages; a piece says the pages it is on",
          [s["path"] for s in sections] == ["Overview", "Overview > Details", "Overview > More details"]
          and [p["pages"] for p in pieces] == [(1, 2), (2, 3), (3, 3)])
    scan = read_pdf(blank_pdf(2))
    check("pdf: pages with no text are counted and warned about, never silently dropped",
          scan.quality.pages == 2 and scan.quality.empty_pages == 2 and scan.quality.ratio == 0.0 and scan.quality.warnings
          and scan.blocks == [])
    seen = []
    ocr = read_pdf(blank_pdf(2), ocr=lambda data, i: seen.append(i) or (f"recognised page {i + 1}" if i == 0 else ""))
    check("pdf: the ocr hook is asked for exactly the empty pages, and its text is used",
          seen == [0, 1] and "recognised page 1" in ocr.text and ocr.quality.ocr_pages == 1 and ocr.quality.empty_pages == 1)
    return {"confluence": c, "markdown": m, "docx": d, "pptx": p, "xlsx": x, "csv": cs, "rst": r, "html": h,
            **({"pdf": tabled} if pdfs else {})}


def graph_checks(check, docs_by_format):
    common.bind(QM)
    common.set_tenant("doctest")
    reg = common._reg

    # ---- every format through the emitter: pieces cover the text, everything validates
    g = new_graph()
    scope, _ = place(g, "docs", "drive_folder", "t")
    for fmt, doc in docs_by_format.items():
        f = g.rec("File", "docs", f"f.{fmt}", {"name": f"f.{fmt}", "path": f"f.{fmt}", "kind": "doc"}, scope)
        cv = g.rec("ContentVersion", "docs", f"f.{fmt}:v", {"version": "v", "text": doc.text}, scope, acl_from=f)
        g.link("VERSION_OF", cv, f, "field", kind="revision", order=1)
        out = docgraph.emit(g, f, doc, cv, scope, "docs", f"f.{fmt}:v", at="2026-03-01T00:00:00Z", piece_chars=300)
        by_id = {r["id"]: r for r in g.records.values()}
        parents = [by_id[lk["b"]]["type"] for p in out["pieces"] for lk, _, _ in g.links.values()
                   if lk["type"] == "PART_OF" and lk["a"] == p["id"]]
        nexts = sum(1 for lk, _, _ in g.links.values() if lk["type"] == "NEXT" and lk["a"] in {p["id"] for p in out["pieces"]})
        check(f"emit {fmt}: {len(out['pieces'])} pieces tile the text ({len(doc.text):,} chars), each PART_OF a section or the "
              f"file, chained by NEXT", tiles(doc, out["pieces"]) and len(parents) == len(out["pieces"])
              and set(parents) <= {"DocSection", "File"} and nexts == len(out["pieces"]) - 1
              and all(p["props"]["kind"] in ("text", "table", "code", "list") for p in out["pieces"]))
    rec_report, link_report, _ = g.validate()
    bad = {k: dict(v["errors"]) for k, v in {**rec_report, **link_report}.items() if v["checked"] != v["valid"]}
    check(f"emit: every record and link validates ({sum(v['checked'] for v in rec_report.values())} records, "
          f"{sum(v['checked'] for v in link_report.values())} links){' ' + str(bad) if bad else ''}", not bad)
    secs = [r for r in g.records.values() if r["type"] == "DocSection"]
    check("emit: sections carry heading, kind, level, order and a 'Top > Sub' path; sheets, slides and pages are sections",
          any(s["props"]["path"] == "Retries > Settings" and s["props"]["level"] == 2 for s in secs)
          and {"heading", "sheet", "slide"} <= {s["props"]["kind"] for s in secs}
          and any(s["props"]["path"] == "slide 2: Numbers > Speaker notes" for s in secs))

    # ---- long blocks: a table is cut between rows with the header repeated, code between lines
    g = new_graph()
    scope, _ = place(g, "docs", "drive_folder", "t")
    rows = [["id", "name", "note"]] + [[str(i), f"name {i}", "x" * 20] for i in range(60)]
    code = "\n".join(f"line_{i} = {i}" for i in range(80))
    big = DocText(blocks=[Block("heading", "Data", level=1), Block("table", rows=rows, header=True),
                          Block("code", code, lang="python"), Block("paragraph", "word " * 400)]).finish()
    f = g.rec("File", "docs", "big", {"name": "big"}, scope)
    cv = g.rec("ContentVersion", "docs", "big:v", {"version": "v", "text": big.text}, scope, acl_from=f)
    out = docgraph.emit(g, f, big, cv, scope, "docs", "big:v", piece_chars=500)
    tables = [p for p in out["pieces"] if p["props"]["kind"] == "table"]
    codes = [p for p in out["pieces"] if p["props"]["kind"] == "code"]
    all_rows = [ln for p in tables for ln in p["props"]["text"].split("\n") if ln != "id | name | note"]
    check("emit: a long table is split by rows, every part starts with the header row, no row is lost or cut",
          len(tables) > 2 and all(p["props"]["text"].startswith("id | name | note\n") for p in tables)
          and all_rows == [" | ".join(r) for r in rows[1:]] and all(len(p["props"]["text"]) <= 500 + 20 for p in tables))
    check("emit: a long code block is split between lines, a long paragraph between words; spans still tile",
          len(codes) > 2 and "\n".join(p["props"]["text"] for p in codes).replace("```python\n", "").replace("\n```", "") == code
          and tiles(big, out["pieces"]) and max(len(p["props"]["text"]) for p in out["pieces"]) <= 520)

    # ---- a wiki page with two versions: sections are dated, changed sections are EDITED_BY the editor
    g = new_graph()
    scope, _ = place(g, "confluence", "space", "ENG")
    page = g.rec("Document", "confluence", "100", {"title": "Design", "kind": "wiki_page"}, scope)
    other = g.rec("Document", "confluence", "200", {"title": "Rollout plan", "kind": "wiki_page"}, scope)
    g.ctx["documents_by_title"] = {("confluence", "eng", "rollout plan"): other}
    bob = g.rec("Account", "confluence", "u-bob", {"login": "bob", "display_name": "Bob Kumar"}, scope)
    g.ctx["confluence_accounts"] = {"u-bob": bob}
    ticket = g.rec("Ticket", "linear", "ENG-1", {"identifier": "ENG-1", "title": "t"}, scope)
    g.ctx["tickets"] = {"ENG-1": ticket}
    addr = g.rec("EmailAddress", "email", "carol@acme.io", {"address": "carol@acme.io"}, scope)
    v1 = read_confluence("<h1>Design</h1><p>old words</p><h2>Risks</h2><p>none yet</p><h2>Gone later</h2><p>temporary</p>")
    v2 = read_confluence(CONFLUENCE.replace("<h2>Config</h2>", "<h2>Risks</h2><p>none yet</p><h2>Config</h2>"), users={"u-bob": "Bob Kumar"})
    cvs = []
    for n, (doc, at) in enumerate(((v1, "2026-01-01T00:00:00Z"), (v2, "2026-02-01T00:00:00Z")), 1):
        cv = g.rec("ContentVersion", "confluence", f"100:v{n}", {"version": n, "valid_from": at, "text": doc.text}, scope, acl_from=page)
        g.link("VERSION_OF", cv, page, "field", kind="revision", order=n)
        cvs.append(docgraph.emit(g, page, doc, cv, scope, "confluence", f"100:v{n}", at=at, current=n == 2, editor=bob,
                                 attribute=True, space="ENG"))
    linked = linker.resolve(g)
    by_id = {r["id"]: r for r in g.records.values()}
    links = [lk for lk, _, _ in g.links.values()]
    sec = {r["props"]["path"]: r for r in g.records.values() if r["type"] == "DocSection"}
    sec_link = lambda path: next(lk for lk in links if lk["a"] == sec[path]["id"] and lk["type"] in ("SECTION_OF", "PART_OF"))
    check("emit (wiki): a Document's top sections are PART_OF it (SECTION_OF is not allowed there), subsections SECTION_OF their parent",
          not reg.link_allows("SECTION_OF", "DocSection", "Document") and sec_link("Design")["type"] == "PART_OF"
          and sec_link("Design > Risks")["type"] == "SECTION_OF")
    check("emit (wiki): a section is one record across versions, dated from the version it appeared in to the one it left",
          set(sec) == {"Design", "Design > Risks", "Design > Gone later", "Design > Config"}
          and sec_link("Design > Risks")["props"]["valid_from"] == "2026-01-01T00:00:00Z" and "valid_to" not in sec_link("Design > Risks")["props"]
          and sec_link("Design > Gone later")["props"].get("valid_to") == "2026-02-01T00:00:00Z"
          and sec_link("Design > Config")["props"]["valid_from"] == "2026-02-01T00:00:00Z")
    edited = {by_id[lk["a"]]["props"]["path"]: lk["props"] for lk in links if lk["type"] == "EDITED_BY"}
    check("emit (wiki): the sections a version changed are EDITED_BY its editor, with first_at, last_at and count",
          edited["Design"]["count"] == 2 and edited["Design"]["first_at"] == "2026-01-01T00:00:00Z"
          and edited["Design"]["last_at"] == "2026-02-01T00:00:00Z" and edited["Design > Risks"]["count"] == 1
          and edited["Design > Config"]["count"] == 1)
    check("emit (wiki): both versions have pieces, each OF_VERSION its own version",
          all(tiles(d, out["pieces"]) for d, out in zip((v1, v2), cvs)) and len(cvs[0]["pieces"]) >= 3
          and len([lk for lk in links if lk["type"] == "OF_VERSION"]) == len(cvs[0]["pieces"]) + len(cvs[1]["pieces"]))
    piece_ids = {p["id"] for p in cvs[1]["pieces"]}
    out_of = lambda ltype, target: [lk for lk in links if lk["type"] == ltype and lk["a"] in piece_ids and lk["b"] == target["id"]]
    atts = {r["props"]["title"]: r for r in g.records.values() if r["type"] == "Attachment"}
    check("links: a page link resolves by title to the Document; attachments are records the page HAS_ATTACHMENT",
          out_of("LINKS_TO", other) and set(atts) == {"budget.xlsx", "flow.png"}
          and all(any(lk["type"] == "HAS_ATTACHMENT" and lk["a"] == page["id"] and lk["b"] == a["id"] for lk in links) for a in atts.values())
          and out_of("LINKS_TO", atts["budget.xlsx"]))
    check("links: the ticket macro, the user mention and the email address become MENTIONS",
          out_of("MENTIONS", ticket) and out_of("MENTIONS", ticket)[0]["props"]["method"] == "integration"
          and out_of("MENTIONS", bob) and out_of("MENTIONS", bob)[0]["props"]["kind"] == "mention" and out_of("MENTIONS", addr))
    check("links: an address that names nothing in the graph is counted as outside, not as a failure",
          linked["outside_addresses"] == {"Piece (confluence)": 1} and not linked["unresolved"])
    rec_report, link_report, _ = g.validate()
    bad = {k: dict(v["errors"]) for k, v in {**rec_report, **link_report}.items() if v["checked"] != v["valid"]}
    check(f"emit (wiki): every record and link validates{' ' + str(bad) if bad else ''}", not bad)

    # ---- stored files: hash, size, same content, metadata, front matter, quality
    g = new_graph()
    scope, _ = place(g, "docs", "drive_folder", "t")
    g.ctx["tickets"] = {"ENG-1": g.rec("Ticket", "linear", "ENG-1", {"identifier": "ENG-1", "title": "t"}, scope)}
    person = g.rec("Person", "directory", "bob", {"full_name": "Bob Kumar", "status": "active"}, scope)
    md = MARKDOWN.encode("utf-8")
    f1 = docs.map_file(g, "handbook/retries.md", md, {"modified_at": "2026-03-05T10:00:00+05:30"}, scope)
    f2 = docs.map_file(g, "copies/retries-copy.md", md, {}, scope)
    f3 = docs.map_file(g, "handbook/payments.docx", make_docx(), {"modified_at": "2026-02-02T00:00:00Z"}, scope)
    f4 = docs.map_file(g, "scan.pdf", blank_pdf(1), {}, scope)
    f5 = docs.map_file(g, "logo.png", b"\x89PNG....", {}, scope)
    f6 = docs.map_file(g, "broken.xlsx", b"not a zip", {}, scope)
    half = DocText(format="pdf", blocks=[Block("container", container="page", meta={"index": 1}),
                                         Block("paragraph", "This page mentions ENG-1."),
                                         Block("container", container="page", meta={"index": 2})])
    half.quality.pages, half.quality.empty_pages, half.quality.source_chars = 2, 1, 20
    half.finish()
    f7 = g.rec("File", "docs", "half.pdf", {"name": "half.pdf"}, scope)
    cv7 = g.rec("ContentVersion", "docs", "half.pdf:v", {"version": "v", "text": half.text}, scope, acl_from=f7)
    docgraph.emit(g, f7, half, cv7, scope, "docs", "half.pdf:v")
    linker.resolve(g)
    docgraph.finish(g)
    links = [lk for lk, _, _ in g.links.values()]
    by_id = {r["id"]: r for r in g.records.values()}
    import hashlib
    cv1 = next(r for r in g.records.values() if r["type"] == "ContentVersion" and r["props"]["native_id"].startswith("handbook/retries.md:"))
    check("docs: a File has sha256, size and mime type; its version is keyed by the content hash and dated by the file's time",
          f1["props"]["sha256"] == hashlib.sha256(md).hexdigest() and f1["props"]["size"] == len(md)
          and f1["props"]["mime_type"] == "text/markdown" and cv1["props"]["version"] == f1["props"]["sha256"][:12]
          and cv1["props"]["valid_from"] == "2026-03-05T04:30:00Z" and cv1["props"]["version"] != "current")
    check("docs: two files with the same bytes are SAME_CONTENT_AS (basis hash)",
          any(lk["type"] == "SAME_CONTENT_AS" and {lk["a"], lk["b"]} == {f1["id"], f2["id"]} and lk["props"]["basis"] == "hash" for lk in links))
    check("docs: front matter sets status and doc_date on the File, each cited in _stated to the version that says it",
          f1["props"].get("status") == "draft" and f1["props"].get("doc_date") == "2026-03-02T00:00:00Z"
          and f1["props"]["_stated"]["status"]["by"] == {cv1["id"]: {"value": "status: Draft"}}
          and f1["props"]["_stated"]["doc_date"]["by"] == {cv1["id"]: {"day": "date: 2026-03-02"}} and f1["props"]["title"] == "Retry handbook")
    owned = [lk for lk in links if lk["type"] == "MAINTAINED_BY" and lk["a"] == f1["id"]]
    check("docs: the front-matter owner becomes MAINTAINED_BY the person (source front_matter)",
          len(owned) == 1 and owned[0]["b"] == person["id"] and owned[0]["props"]["source"] == "front_matter")
    meta = [by_id[lk["b"]] for lk in links if lk["type"] == "HAS_METADATA" and lk["a"] == f3["id"]]
    alias_of = lambda ltype: [by_id[lk["b"]]["props"]["value"] for lk in links if lk["type"] == ltype and meta and lk["a"] == meta[0]["id"]]
    check("docs: a .docx gives FileMetadata with METADATA_CREATOR and METADATA_LAST_MODIFIED_BY aliases, created_at and doc_date",
          len(meta) == 1 and meta[0]["props"]["application"] == "Microsoft Office Word" and meta[0]["props"]["company"] == "ACME Corp"
          and alias_of("METADATA_CREATOR") == ["Alice Ng"] and alias_of("METADATA_LAST_MODIFIED_BY") == ["Bob Kumar"]
          and f3["props"]["created_at"] == "2026-01-05T09:00:00Z" and f3["props"]["doc_date"] == "2026-02-01T08:30:00Z")
    check("docs: a .docx review comment is a Comment ON the file",
          any(r["type"] == "Comment" and r["props"]["text"] == "Is the gold limit right?"
              and any(lk["type"] == "ON" and lk["a"] == r["id"] and lk["b"] == f3["id"] for lk in links) for r in g.records.values()))
    q = g.ctx["doc_quality"]
    check("docs: unreadable and unsupported files keep their File (with hash) and are counted with the reason",
          f5["props"]["sha256"] and g.notes["docs: not read, no reader for .png"] == 1 and q[f6["id"]]["ratio"] == 0.0
          and any("unreadable .xlsx" in k for k in g.notes))
    check("docs: a scanned PDF is in the quality record with ratio 0 and the reason",
          q[f4["id"]]["ratio"] == 0.0 and any("no text" in r for r in q[f4["id"]]["reasons"]))
    half_piece = next(r for r in g.records.values() if r["type"] == "Piece" and r["props"]["native_id"].startswith("half.pdf"))
    mention = next(lk for lk in links if lk["type"] == "MENTIONS" and lk["a"] == half_piece["id"])
    good = next(lk for lk in links if lk["type"] == "MENTIONS" and by_id[lk["a"]]["props"]["native_id"].startswith("handbook/retries.md"))
    check("quality: a text-rule link from a half-read document has its confidence scaled by the kept ratio (0.8 -> 0.4)",
          q[f7["id"]]["ratio"] == 0.5 and mention["props"]["confidence"] == 0.4 and good["props"]["confidence"] == 0.8)
    rep = docgraph.report(g)
    check("report: per-source counts, the kept-ratio spread and the worst documents with reasons",
          rep["by_source"]["docs (markdown)"]["documents"] == 2 and rep["kept_ratio"].get("under 0.5") == 2
          and rep["kept_ratio"].get("0.5 - 0.8") == 1 and rep["worst"][0]["kept_ratio"] == 0.0 and rep["worst"][0]["reasons"])
    rec_report, link_report, _ = g.validate()
    bad = {k: dict(v["errors"]) for k, v in {**rec_report, **link_report}.items() if v["checked"] != v["valid"]}
    check(f"docs: every record and link validates{' ' + str(bad) if bad else ''}", not bad)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as tmp:
        by_format = reader_checks(check, Path(tmp))
    graph_checks(check, by_format)
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
