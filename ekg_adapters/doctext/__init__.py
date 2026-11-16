"""Document reading: any supported file or tool format -> one DocText (see model.py).

    from ekg_adapters.doctext import read
    doc = read(path)                       # a file on disk
    doc = read(data, "handbook.docx")      # bytes from a connector, with the name (or mime) to tell the format
    doc.text, doc.blocks, doc.meta, doc.quality

No graph knowledge lives here, and nothing is cut: size limits are met later, by splitting into pieces.
"""

from __future__ import annotations

from pathlib import Path

from .html import read_confluence, read_html
from .model import Block, DocText, Quality, Ref, Unreadable, Unsupported, render_row
from .ooxml import excel_date, read_docx, read_pptx, read_xlsx
from .pdf import read_pdf
from .tabular import read_csv
from .text import front_matter, read_markdown, read_text

FORMATS = {  # extension -> (format, mime type)
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".docm": ("docx", "application/vnd.ms-word.document.macroEnabled.12"),
    ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".ppsx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.slideshow"),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".xlsm": ("xlsx", "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ".csv": ("csv", "text/csv"), ".tsv": ("tsv", "text/tab-separated-values"),
    ".md": ("markdown", "text/markdown"), ".markdown": ("markdown", "text/markdown"), ".mdx": ("markdown", "text/markdown"),
    ".txt": ("text", "text/plain"), ".text": ("text", "text/plain"), ".rst": ("rst", "text/x-rst"),
    ".html": ("html", "text/html"), ".htm": ("html", "text/html"), ".xhtml": ("html", "application/xhtml+xml"),
}
BY_MIME = {mime: fmt for fmt, mime in FORMATS.values()}
BY_MIME.update({"application/vnd.atlassian.confluence.storage": "confluence", "application/x-pdf": "pdf",
                "text/x-markdown": "markdown", "text/comma-separated-values": "csv"})


def format_of(name=None, mime=None):
    """The reader format for a file name and/or mime type, or None when there is no reader for it."""
    ext = Path(name).suffix.lower() if name else ""
    if ext in FORMATS:
        return FORMATS[ext][0]
    return BY_MIME.get((mime or "").split(";")[0].strip().lower())


def mime_of(name):
    ext = Path(name).suffix.lower() if name else ""
    return FORMATS[ext][1] if ext in FORMATS else None


def read(source, name=None, mime=None, title=None, ocr=None, users=None) -> DocText:
    """Read a document. `source` is a path, or the file's bytes (or text) with `name` and/or `mime` given.

    Raises Unsupported when there is no reader for the format, Unreadable when the bytes are damaged."""
    if isinstance(source, Path) or (isinstance(source, str) and name is None and mime is None):
        path = Path(source)
        name, source = path.name, path.read_bytes()
    fmt = format_of(name, mime)
    if fmt is None:
        raise Unsupported(f"no reader for {Path(name).suffix.lower() if name and Path(name).suffix else (mime or 'a file with no extension')}")
    if fmt == "pdf":
        return read_pdf(_bytes(source), title, ocr=ocr)
    if fmt == "docx":
        return read_docx(_bytes(source), title)
    if fmt == "pptx":
        return read_pptx(_bytes(source), title)
    if fmt == "xlsx":
        return read_xlsx(_bytes(source), title)
    if fmt in ("csv", "tsv"):
        return read_csv(source, title, fmt=fmt)
    if fmt == "markdown":
        return read_markdown(source, title)
    if fmt in ("text", "rst"):
        return read_text(source, title, fmt=fmt)
    if fmt == "confluence":
        return read_confluence(source, title, users=users)
    return read_html(source, title)


def _bytes(source) -> bytes:
    if isinstance(source, str):
        raise Unreadable("a binary format was given as text")
    return bytes(source)


__all__ = ["Block", "DocText", "Quality", "Ref", "Unreadable", "Unsupported", "read", "read_confluence", "read_html",
           "read_markdown", "read_text", "read_pdf", "read_docx", "read_pptx", "read_xlsx", "read_csv", "format_of",
           "mime_of", "front_matter", "excel_date", "render_row", "FORMATS"]
