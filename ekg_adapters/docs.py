"""Documents pillar, stored files: docs/ -> File, ContentVersion (its text), DocSection, Piece, FileMetadata.

Every file under docs/ (any depth) goes through map_file(), which takes the file as a connector would
hand it over: a name, its bytes, and what the storage knows about it (modified time, created time,
mime type, title). The folder walk at the bottom is just one caller.

  .pdf .docx .pptx .xlsx .csv .tsv .md .txt .rst .html   read by ekg_adapters.doctext (see there)
  anything else                                           the File alone, counted by reason

A File carries its sha256 and size; two files with the same bytes are SAME_CONTENT_AS each other. The
ContentVersion is keyed by the content hash, so the same bytes are the same version on every run, and
dated by when the file was last modified (docs/manifest.json can say; otherwise the file's own time).
Nothing is cut: a long document is many pieces (docgraph.emit).

docs/manifest.json (optional): {"<path under docs/>": {"modified_at", "created_at", "title", "mime_type"}}.
"""

import datetime as dt
import hashlib
import json
from pathlib import Path

from . import docgraph
from .access import place
from .common import Graph, from_dt, utc
from .doctext import Unreadable, Unsupported, format_of, mime_of, read

KIND_OF_FORMAT = {"pdf": "pdf", "xlsx": "spreadsheet", "csv": "spreadsheet", "tsv": "spreadsheet", "pptx": "presentation"}
MANIFEST_NAMES = ("manifest.json", "_manifest.json")


def map_file(g: Graph, name: str, data: bytes, meta: dict, scope, tool: str = "docs"):
    """One stored file -> its File record and, when it can be read, its version, sections and pieces.

    name   the file's path inside its store (forward slashes): its identity
    data   the file's bytes
    meta   what the store says about it; all optional: modified_at, created_at (ISO times), mime_type, title"""
    meta = meta or {}
    base = name.rsplit("/", 1)[-1]
    ext = ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""
    fmt = format_of(base, meta.get("mime_type"))
    kind = KIND_OF_FORMAT.get(fmt, "doc")
    sha = hashlib.sha256(data).hexdigest()
    modified, created = _time(meta.get("modified_at")), _time(meta.get("created_at"))
    f = g.rec("File", tool, name, {"name": base, "title": meta.get("title") or (base.rsplit(".", 1)[0] if ext else base),
              "path": name, "kind": kind, "mime_type": meta.get("mime_type") or mime_of(base), "size": len(data),
              "sha256": sha, "created_at": created}, scope)
    g.ctx.setdefault("doc_files", {})[name] = f
    docgraph.same_content(g, f, sha)
    try:
        doc = read(data, base, mime=meta.get("mime_type"))
    except Unsupported as e:
        g.notes[f"docs: not read, {e}"] += 1
        return f
    except Exception as e:  # one unreadable file never stops the run; it is counted and listed in the report
        why = str(e) if isinstance(e, Unreadable) else f"{type(e).__name__}: {e}"[:160]
        g.notes[f"docs: unreadable {ext or 'file'} ({why if isinstance(e, Unreadable) else type(e).__name__}), File kept without text"] += 1
        g.ctx.setdefault("doc_quality", {})[f["id"]] = {
            "source": tool, "format": fmt, "name": name, "type": "File", "ratio": 0.0, "source_chars": 0, "kept_chars": 0,
            "text_chars": 0, "reasons": [f"unreadable: {why}"],
            "quality": {"unknown": {"unreadable file": 1}, "dropped": {}, "warnings": [f"unreadable: {why}"]}}
        return f
    if doc.title and not meta.get("title"):
        f["props"]["title"] = doc.title
    native = f"{name}:{sha[:16]}"
    cv = g.rec("ContentVersion", tool, native, {"version": sha[:12], "valid_from": modified, "text": doc.text}, scope, acl_from=f)
    g.link("VERSION_OF", cv, f, "field", kind="revision", order=1)
    docgraph.emit(g, f, doc, cv, scope, tool, native, at=modified, current=True, base_path=name)
    g.notes[f"docs: {kind} files with text" if doc.text else f"docs: {kind} files read but holding no text"] += 1
    return f


def _time(value):
    """An ISO time from a manifest (any offset), or a datetime -> the graph's UTC form; None when absent or bad."""
    if isinstance(value, dt.datetime):
        return from_dt(value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc))
    try:
        return utc(value) if value else None
    except (ValueError, TypeError):
        return None


def map_docs(g: Graph, d: Path):
    s = g.ctx["settings"]
    scope, _ = place(g, "docs", "drive_folder", g.ctx.get("repo_name") or "docs", public=s.get("public", True))
    root = d / "docs"
    listed = {}
    for mname in MANIFEST_NAMES:
        mpath = root / mname
        if mpath.is_file():
            try:
                loaded = json.loads(mpath.read_text(encoding="utf-8"))
                listed = loaded.get("files", loaded) if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                g.notes["docs: manifest.json not readable (file times used)"] += 1
            break
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        if rel in MANIFEST_NAMES:
            continue
        meta = dict(listed.get(rel) or {}) if isinstance(listed.get(rel), dict) else {}
        if not meta.get("modified_at"):  # the file's own time: the best the folder can say
            meta["modified_at"] = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc)
        try:
            data = p.read_bytes()
        except OSError as e:
            g.notes[f"docs: file could not be opened ({type(e).__name__})"] += 1
            continue
        map_file(g, rel, data, meta, scope)
