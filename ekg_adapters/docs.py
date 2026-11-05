"""Documents pillar, stored files: docs/ -> File, ContentVersion (its text), Piece (per page or per sheet).

Reads every file under docs/ (any depth). The schema has no sheet or row type, and needs none: a
file is a File, its text a ContentVersion, and each PDF page or spreadsheet sheet a Piece of it, so
it is searchable and the LLM stage can read it.
  - .pdf     text per page, with pypdf when it is installed; otherwise the File alone, counted
  - .xlsx    each sheet's rows as tab-separated text (read with the standard library)
  - .csv / .tsv, .md / .txt / .rst / .html   as text
Ticket keys and #n references in the text become MENTIONS from the piece that holds them.
"""

import csv
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .access import place
from .common import Graph, ticket_rx, xhtml_text

MAX_TEXT = 400_000
GH_REF = re.compile(r"(?<![\w/&])#(\d{1,6})\b")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MIME = {".pdf": "application/pdf", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".md": "text/markdown", ".txt": "text/plain",
        ".rst": "text/x-rst", ".html": "text/html"}


def pdf_pages(p: Path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return [(page.extract_text() or "").strip() for page in PdfReader(str(p)).pages]
    except Exception:  # a damaged or encrypted PDF
        return []


def xlsx_sheets(p: Path):
    """[(sheet name, text)] from an .xlsx, with the standard library only."""
    with zipfile.ZipFile(p) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.find("m:sheets", NS)]
        out = []
        for i, name in enumerate(names, 1):
            path = f"xl/worksheets/sheet{i}.xml"
            if path not in z.namelist():
                continue
            rows = []
            for row in ET.fromstring(z.read(path)).iter(f"{{{NS['m']}}}row"):
                cells = []
                for c in row.findall("m:c", NS):
                    v = c.find("m:v", NS)
                    inline = c.find("m:is/m:t", NS)
                    val = inline.text if inline is not None else (v.text if v is not None else "")
                    cells.append(shared[int(val)] if c.get("t") == "s" and val and val.isdigit() else (val or ""))
                if any(cells):
                    rows.append("\t".join(cells))
            out.append((name, "\n".join(rows)))
        return out


def _parts(p: Path):
    """[(part name, text)] for one file, or None when the format can't be read here."""
    ext = p.suffix.lower()
    if ext == ".pdf":
        pages = pdf_pages(p)
        return None if pages is None else [(f"page {i}", t) for i, t in enumerate(pages, 1) if t]
    if ext == ".xlsx":
        return xlsx_sheets(p)
    if ext in (".csv", ".tsv"):
        text = p.read_text(encoding="utf-8", errors="replace")
        rows = csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ",")
        return [("rows", "\n".join("\t".join(r) for r in rows if any(r)))]
    if ext in (".md", ".txt", ".rst"):
        return [("text", p.read_text(encoding="utf-8", errors="replace"))]
    if ext in (".html", ".htm"):
        return [("text", xhtml_text(p.read_text(encoding="utf-8", errors="replace")))]
    return None


def map_docs(g: Graph, d: Path):
    s = g.ctx["settings"]
    scope, _ = place(g, "docs", "drive_folder", g.ctx.get("repo_name") or "docs", public=s.get("public", True))
    key_rx, github_issues = ticket_rx(g), s.get("issue_tracker") == "github"
    root = d / "docs"
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        kind = "pdf" if ext == ".pdf" else "spreadsheet" if ext in (".xlsx", ".csv", ".tsv") else "doc"
        f = g.rec("File", "docs", rel, {"name": p.name, "title": p.stem, "path": rel, "kind": kind,
                  "mime_type": MIME.get(ext), "size": p.stat().st_size}, scope)
        try:
            parts = _parts(p)
        except Exception as e:  # one unreadable file never stops the run
            g.notes[f"docs: unreadable {ext or 'file'} ({type(e).__name__}), File kept without text"] += 1
            continue
        if parts is None:
            g.notes[f"docs: {ext or 'no extension'} text not extracted (format or library not available)"] += 1
            continue
        text = "\n\n".join(f"[{name}]\n{t}" for name, t in parts)[:MAX_TEXT]
        cv = g.rec("ContentVersion", "docs", f"{rel}:current", {"version": "current", "text": text}, scope, acl_from=f)
        g.link("VERSION_OF", cv, f, "field", kind="revision", order=1)
        for order, (name, t) in enumerate(parts):
            piece = g.rec("Piece", "docs", f"{rel}:{order}", {"text": t[:20_000], "kind": kind, "heading": name,
                          "order": order}, scope, acl_from=f)
            g.link("PART_OF", piece, f, "field", order=order)
            g.link("OF_VERSION", piece, cv, "field")
            for k in sorted(set(key_rx.findall(t))):
                g.ref(piece, "MENTIONS", "Ticket", k, "text_rule", 0.8, kind="key")
            if github_issues:
                for n in sorted({int(x) for x in GH_REF.findall(t)}):
                    g.ref(piece, "MENTIONS", "GitHubRef", n, "text_rule", 0.6, kind="key")
        g.notes[f"docs: {kind} files with text"] += 1
