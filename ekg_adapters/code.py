"""Code pillar, the code tree: a snapshot of the repository -> Folder, File, ContentVersion (docs), CodeSymbol.

Reads code/, in either form:
  - a listing: code/tree.jsonl or code/tree.json, rows {path, size?, sha?}
  - the files themselves: everything else under code/ (a checkout, or a copy of one), read as text
Every path becomes a File IN its Folder, and every folder IN its parent, so the tree is complete
even for files no commit touched. A File has the same key as the git adapter's, so the two merge.
README and documentation files are read like any other document (sections and pieces). Source files
of every language there is a grammar for are parsed by symbols.py: types, functions and methods become
CodeSymbols DEFINED_IN their file, with IMPORTS, EXTENDS and CALLS between symbols of the same repository.
"""

import json
from pathlib import Path

from . import docgraph
from .common import Graph, file_props
from .doctext import format_of, read as read_doc
from .git import ensure_repo
from .symbols import has_symbols, map_symbols

DOC_NAMES = ("readme", "contributing", "changelog", "license", "notice", "security", "code_of_conduct")
MAX_TEXT = 200_000  # characters of one file kept as text
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "build", "dist"}


def _listing(root: Path):
    """[(path, absolute path or None)], from tree.json(l) or by walking the folder."""
    for name in ("tree.jsonl", "tree.json"):
        f = root / name
        if f.exists():
            text = f.read_text(encoding="utf-8")
            rows = [json.loads(line) for line in text.splitlines() if line.strip()] if name.endswith("l") else json.loads(text)
            return [((r["path"] if isinstance(r, dict) else r).strip("/"), None) for r in rows]
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not any(part in SKIP_DIRS for part in rel.parts):
            out.append((rel.as_posix(), p))
    return out


def _is_doc(path):
    name = path.rsplit("/", 1)[-1].lower()
    # README and friends, anything under docs/ there is a reader for, and every file the tree calls a document
    # (file_props kind "doc"): each of them gets sections and pieces, so no document has text but no pieces
    return (name.split(".")[0] in DOC_NAMES or path.lower().startswith(("docs/", "doc/")) and format_of(name) is not None
            or file_props(path)["kind"] == "doc")


def map_code(g: Graph, d: Path):
    repo = ensure_repo(g, g.ctx.get("repo_name", ""))
    scope, rid = repo["scope"], repo["props"]["native_id"]
    files, folders = g.ctx.setdefault("files", {}), {}

    def folder(path):
        if path in ("", "."):
            return None
        if path not in folders:
            folders[path] = g.rec("Folder", "git", f"{rid}:{path}/", {"name": path.rsplit("/", 1)[-1], "path": path,
                                  "kind": "directory"}, scope, acl_from=repo)
            parent = folder(path.rsplit("/", 1)[0] if "/" in path else "")
            g.link("IN", folders[path], parent or repo, "field")
        return folders[path]

    listing = _listing(d / "code")
    sources = {}  # path -> source text of the files a grammar can read
    for path, abs_path in listing:
        if path in ("tree.json", "tree.jsonl"):
            continue
        if path not in files:
            files[path] = g.rec("File", "git", f"{rid}:{path}", file_props(path), scope, acl_from=repo)
        f = files[path]
        container = folder(path.rsplit("/", 1)[0] if "/" in path else "")
        g.link("IN", f, container or repo, "field")
        if abs_path is None:
            continue
        if _is_doc(path):
            try:
                source = abs_path.read_bytes()   # the whole file: a long document becomes many pieces, never a cut one
            except OSError:
                source = None
            if source and _doc(g, f, rid, path, source, scope, "repository document"):
                g.notes["code: documentation files with text"] += 1
        if has_symbols(path):
            try:  # the whole file: a cut file would lose the symbols in its tail
                sources[path] = abs_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                g.notes["code: source files not readable as text"] += 1
    # a listing without the files (a blobless clone): the text of the files whose content is already in the local
    # repo.git (contributor guides, templates, CI workflows, build files ...), read without fetching anything
    if listing and all(a is None for _, a in listing):
        texts = _local_texts(d, d / "code")
        fetched = d / "code" / "files"  # files fetched apart, checked against the listing's blob sha (code/files/)
        if fetched.is_dir():
            for path in files:
                p = fetched / path
                if path not in texts and p.is_file():
                    t = _read(p)
                    if t and "\0" not in t[:8000]:
                        texts[path] = t
        for path, text in texts.items():
            f = files.get(path)
            if f is None or not text:
                continue
            if _is_doc(path) and _doc(g, f, rid, path, text, scope, "repository file (trunk snapshot)"):
                continue
            cv = g.rec("ContentVersion", "git", f"{rid}:{path}:snapshot", {"version": "snapshot", "text": text,
                       "note": "repository file (trunk snapshot)"}, scope, acl_from=f)
            g.link("VERSION_OF", cv, f, "field", kind="revision", order=1)
        g.notes["code: files with text from the local repository"] = len(texts)
        for path, text in texts.items():
            if has_symbols(path):
                p = fetched / path  # the fetched copy uncut when there is one (texts are capped at MAX_TEXT)
                try:
                    sources[path] = p.read_text(encoding="utf-8") if p.is_file() else text
                except (UnicodeDecodeError, OSError):
                    sources[path] = text
    map_symbols(g, repo, scope, rid, files, sources)
    g.notes["code: files in the tree"] = len(listing)
    g.notes["code: folders in the tree"] = len(folders)


def _doc(g, f, rid, path, source, scope, note):
    """A repository document (README, a file under docs/): read like any other document, so it has sections and
    pieces as well as its text. `source` is the file's bytes, or its text. Returns the ContentVersion, or None
    when there is no reader for it or it holds no text (the caller then keeps what it kept before)."""
    name = path.rsplit("/", 1)[-1]
    if isinstance(source, bytes) and format_of(name) is None and b"\0" in source[:8000]:
        return None   # a binary file with no reader (an image under docs/): not text
    try:
        doc = read_doc(source, name if format_of(name) else name + ".txt")   # README, LICENSE ...: plain text
    except Exception as e:
        g.notes[f"code: documentation file not readable ({type(e).__name__})"] += 1
        return None
    if not doc.text:
        return None
    native = f"{rid}:{path}:snapshot"
    cv = g.rec("ContentVersion", "git", native, {"version": "snapshot", "text": doc.text, "note": note}, scope, acl_from=f)
    g.link("VERSION_OF", cv, f, "field", kind="revision", order=1)
    docgraph.emit(g, f, doc, cv, scope, "git", native, current=True, base_path=path)
    return cv


def _local_texts(d: Path, code_dir: Path) -> dict:
    """{path: text} for the listed files whose blob is already in repo.git. Only objects the clone holds are read
    (listed first with --batch-all-objects), lazy fetching is off, and nothing is written to the repository."""
    import os
    import subprocess
    from .common import GIT_READ
    repo = d / "git" / "repo.git"
    tree = code_dir / "tree.jsonl"
    if not repo.is_dir() or not tree.exists():
        return {}
    sha_of = {}
    for line in tree.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("sha"):
                sha_of[r["path"].strip("/")] = r["sha"]
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    base = ["git", "--git-dir", str(repo), *GIT_READ, "cat-file"]
    try:
        listed = subprocess.run([*base, "--batch-check=%(objectname) %(objecttype) %(objectsize)", "--batch-all-objects"],
                                capture_output=True, env=env, timeout=600, check=True).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return {}
    local = {}
    for row in listed.splitlines():
        sha, kind, size = (row.split() + ["", "", "0"])[:3]
        if kind == "blob" and size.isdigit() and int(size) <= MAX_TEXT:
            local[sha] = int(size)
    want = {p: s for p, s in sha_of.items() if s in local}
    if not want:
        return {}
    shas = list(dict.fromkeys(want.values()))
    try:
        raw = subprocess.run([*base, "--batch"], input=("\n".join(shas) + "\n").encode(), capture_output=True, env=env,
                             timeout=600, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    blobs, pos = {}, 0
    for sha in shas:  # each object: "<sha> blob <size>\n<content>\n"
        nl = raw.index(b"\n", pos)
        head = raw[pos:nl].decode().split()
        pos = nl + 1
        if len(head) < 3 or head[1] != "blob":
            continue
        size = int(head[2])
        body = raw[pos:pos + size]
        pos += size + 1
        if b"\0" in body[:8000]:
            continue  # binary
        try:
            blobs[sha] = body.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return {p: blobs[s] for p, s in want.items() if s in blobs}


def _read(p: Path):
    try:
        return p.read_text(encoding="utf-8")[:MAX_TEXT]
    except (UnicodeDecodeError, OSError):
        return None
