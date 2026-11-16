"""The format-neutral document model every reader produces.

A DocText is an ordered list of blocks plus the canonical text they render to. Every block knows
where it sits in that text (`start`, `end`), so anything cut from the text later (a search piece, a
quote) can point back at the block, and through the block at its section, page, slide or sheet.

Nothing here knows about the graph, and nothing here cuts text: a reader keeps everything it can
read and says, in `quality`, what it could not.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

BLOCK_KINDS = ("heading", "paragraph", "list_item", "table", "code", "quote", "container")
CONTAINER_KINDS = ("page", "slide", "sheet", "tab")
REF_KINDS = ("url", "page", "attachment", "image", "user", "ticket", "email", "file")

URL_RX = re.compile(r"https?://[^\s<>\"'`\]\[)(|\\^{}]+")
EMAIL_RX = re.compile(r"(?<![\w.+/-])[A-Za-z0-9][\w.+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w-])")
GARBAGE_RX = re.compile(r"�|\(cid:\d+\)")  # what a reader leaves behind for a character it could not decode


def nonws(s) -> int:
    """How much text a string holds: its characters that are not whitespace."""
    return len(s) - sum(1 for c in s if c.isspace()) if s else 0


@dataclass
class Ref:
    """Something a block points at.

    kind        target                                   extra
    url         the address                              -
    page        an in-tool page, by title                space, anchor, id
    attachment  a file attached to the document, by name page (when it hangs on another page), diagram
    image       an image: attachment name or address     attachment (True when it is an attachment), alt
    user        a user mention: key or username          name (display name when known), by (userkey|username|account_id)
    ticket      a ticket key from an issue macro         -
    email       an email address                         -
    file        a relative path to another file          -
    """
    kind: str
    target: str
    text: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Block:
    kind: str                      # one of BLOCK_KINDS
    text: str = ""                 # heading, paragraph, list item, quote, code: the words; container: its title
    level: int = 0                 # heading level 1..6; list item depth 1..
    lang: str | None = None        # code
    rows: list | None = None       # table: rows of cell strings, every row the same width
    header: bool = False           # table: the first row is a header row
    container: str | None = None   # container: page | slide | sheet | tab
    refs: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)   # ordered, number, checked, cont (list item); index (container); merged (table)
    start: int = 0                 # offsets of the rendered block in DocText.text
    end: int = 0

    def render(self) -> str:
        k = self.kind
        if k == "table":
            return "\n".join(render_row(r) for r in self.rows or [])
        if k == "code":
            return f"```{self.lang or ''}\n{self.text}\n```"
        if k == "quote":
            return "\n".join("> " + line for line in self.text.split("\n"))
        if k == "list_item":
            pad = "  " * max(self.level - 1, 0)
            if self.meta.get("cont"):       # a further paragraph of the same item
                mark = "  "
            elif self.meta.get("checked") is not None:
                mark = "[x] " if self.meta["checked"] else "[ ] "
            elif self.meta.get("ordered"):
                mark = f"{self.meta.get('number', 1)}. "
            else:
                mark = "- "
            return pad + mark + self.text.replace("\n", "\n" + pad + " " * len(mark))
        if k == "container":
            n = self.meta.get("index")
            label = f"{self.container} {n}" if n is not None else self.container
            return f"[{label}: {self.text}]" if self.text else f"[{label}]"
        return self.text


def cell_text(value) -> str:
    """One table cell on one line: a line break inside a cell becomes ' / '."""
    return " / ".join(p.strip() for p in str(value if value is not None else "").split("\n") if p.strip())


def render_row(cells) -> str:
    """A table row as `a | b | c`; an empty cell keeps its place, so columns stay aligned."""
    return " | ".join(cell_text(c) for c in cells).rstrip()


@dataclass
class Quality:
    """How well a document was read. Counted, never guessed: a reader adds to these as it goes."""
    source_chars: int = 0          # content characters the reader saw (not whitespace, not markup, not macro settings)
    kept_chars: int = 0            # of those, the ones that are in the blocks (set by DocText.finish)
    lost_chars: int = 0            # content characters a reader saw and could not place in a block
    pages: int = 0
    slides: int = 0
    sheets: int = 0
    empty_pages: int = 0           # pages with no text at all (a scan: needs OCR)
    failed_parts: int = 0          # pages, slides, sheets that could not be read
    ocr_pages: int = 0             # pages whose text came from the ocr hook
    tables: int = 0
    images: int = 0
    warnings: list = field(default_factory=list)
    unknown: collections.Counter = field(default_factory=collections.Counter)   # not understood, by reason
    dropped: collections.Counter = field(default_factory=collections.Counter)   # left out on purpose, by reason

    def warn(self, message: str):
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def ratio(self) -> float:
        """The share of the document that was read, 0..1: characters kept over characters seen, times the share
        of its pages / slides / sheets that gave any text. A scan with no text layer is 0; a clean read is 1."""
        chars = min(1.0, self.kept_chars / self.source_chars) if self.source_chars else 1.0
        units = self.pages + self.slides + self.sheets
        bad = self.empty_pages + self.failed_parts
        parts = max(0.0, 1.0 - bad / units) if units else 1.0
        return round(chars * parts, 4)

    def reasons(self) -> list:
        out = []
        if self.empty_pages:
            out.append(f"{self.empty_pages} of {self.pages} pages have no text (scanned: needs OCR)")
        if self.failed_parts:
            out.append(f"{self.failed_parts} parts could not be read")
        if self.source_chars > self.kept_chars:
            out.append(f"{self.source_chars - self.kept_chars:,} of {self.source_chars:,} characters not kept")
        out += [f"not understood: {k} x{v}" for k, v in self.unknown.most_common(5)]
        out += self.warnings[:5]
        return out

    def as_dict(self) -> dict:
        return {"source_chars": self.source_chars, "kept_chars": self.kept_chars, "ratio": self.ratio,
                "pages": self.pages, "slides": self.slides, "sheets": self.sheets, "empty_pages": self.empty_pages,
                "failed_parts": self.failed_parts, "ocr_pages": self.ocr_pages, "tables": self.tables,
                "images": self.images, "warnings": list(self.warnings), "unknown": dict(self.unknown),
                "dropped": dict(self.dropped)}


@dataclass
class DocText:
    title: str | None = None
    format: str = ""               # confluence | html | markdown | text | rst | pdf | docx | pptx | xlsx | csv | tsv
    meta: dict = field(default_factory=dict)       # creator, last_modified_by, created_at, modified_at, application,
    #                                                company, front_matter {..}; only the keys the source has
    blocks: list = field(default_factory=list)
    text: str = ""
    quality: Quality = field(default_factory=Quality)
    comments: list = field(default_factory=list)   # [{author, at, text}] when the file carries review comments

    def refs(self, kind=None):
        return [r for b in self.blocks for r in b.refs if kind is None or r.kind == kind]

    def finish(self):
        """Render the blocks to the canonical text, set every block's offsets, and pick up what plain text
        itself points at (addresses and email addresses written out in the words)."""
        parts, pos, prev = [], 0, None
        for b in self.blocks:
            if b.kind == "table":
                self.quality.tables += 1
            body = b.render()
            if prev is not None:
                sep = "\n" if prev.kind == "list_item" and b.kind == "list_item" else "\n\n"
                parts.append(sep)
                pos += len(sep)
            b.start, b.end = pos, pos + len(body)
            parts.append(body)
            pos = b.end
            prev = b
            if b.kind not in ("code", "container"):
                _text_refs(b, body)
            _dedupe(b)
        self.text = "".join(parts)
        q = self.quality
        garbage = sum(len(m) for m in GARBAGE_RX.findall(self.text))
        if garbage:
            q.unknown["characters that could not be decoded"] += garbage
        q.kept_chars = max(0, q.source_chars - q.lost_chars - garbage)
        return self


def _text_refs(block: Block, body: str):
    have = {(r.kind, r.target) for r in block.refs}
    for m in URL_RX.finditer(body):
        url = m.group(0).rstrip(".,;:!?*_'\"")
        if ("url", url) not in have and ("image", url) not in have:
            have.add(("url", url))
            block.refs.append(Ref("url", url))
    for m in EMAIL_RX.finditer(body):
        addr = m.group(0).rstrip(".").lower()
        if ("email", addr) not in have and not any(addr in u for k, u in have if k == "url"):
            have.add(("email", addr))
            block.refs.append(Ref("email", addr))


def _dedupe(block: Block):
    seen, out = set(), []
    for r in block.refs:
        key = (r.kind, r.target, r.extra.get("page"), r.extra.get("space"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    block.refs = out


class Unsupported(Exception):
    """No reader for this format (or the library a reader needs is not installed)."""


class Unreadable(Exception):
    """A reader exists but the bytes could not be read (damaged, encrypted, not what the name says)."""
