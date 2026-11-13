"""Code pillar, the code tree: a snapshot of the repository -> Folder, File, ContentVersion (docs), CodeSymbol.

Reads code/, in either form:
  - a listing: code/tree.jsonl or code/tree.json, rows {path, size?, sha?}
  - the files themselves: everything else under code/ (a checkout, or a copy of one), read as text
Every path becomes a File IN its Folder, and every folder IN its parent, so the tree is complete
even for files no commit touched. A File has the same key as the git adapter's, so the two merge.
README and documentation files keep their text as a ContentVersion. Python files are parsed with
the standard library: classes and functions become CodeSymbols DEFINED_IN their file, and a module
that imports another module of the same repository IMPORTS it.
"""

import ast
import json
from pathlib import Path

from .common import Graph, file_props
from .git import ensure_repo

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
    return name.split(".")[0] in DOC_NAMES or path.lower().startswith(("docs/", "doc/")) and name.endswith((".md", ".rst", ".txt"))


def map_code(g: Graph, d: Path):
    repo = ensure_repo(g, g.ctx.get("repo_name", ""))
    scope, rid = repo["scope"], repo["props"]["native_id"]
    files, folders = g.ctx.setdefault("files", {}), {}
    modules = {}  # dotted python module -> (its module symbol, file record)

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
    py = []
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
            text = _read(abs_path)
            if text:
                cv = g.rec("ContentVersion", "git", f"{rid}:{path}:snapshot", {"version": "snapshot", "text": text,
                           "note": "repository document"}, scope, acl_from=f)
                g.link("VERSION_OF", cv, f, "field", kind="revision", order=1)
                g.notes["code: documentation files with text"] += 1
        if path.endswith(".py"):
            py.append((path, abs_path, f))
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
            cv = g.rec("ContentVersion", "git", f"{rid}:{path}:snapshot", {"version": "snapshot", "text": text,
                       "note": "repository file (trunk snapshot)"}, scope, acl_from=f)
            g.link("VERSION_OF", cv, f, "field", kind="revision", order=1)
        g.notes["code: files with text from the local repository"] = len(texts)
    for path, abs_path, f in py:
        mod = path[:-3].replace("/", ".").removesuffix(".__init__")
        sym = g.rec("CodeSymbol", "git", f"{rid}:{mod}", {"qualified_name": mod, "kind": "module"}, scope, acl_from=f)
        g.link("DEFINED_IN", sym, f, "field")
        modules[mod] = sym
    # an import names a module from its source root (python/tvm/x.py is imported as tvm.x): index each
    # module under every dotted suffix that is unique
    suffixes = {}
    for mod in modules:
        parts = mod.split(".")
        for i in range(len(parts)):
            suffixes.setdefault(".".join(parts[i:]), []).append(mod)
    for suffix, mods in suffixes.items():
        if len(mods) == 1 and suffix not in modules:
            modules.setdefault(f"\0{suffix}", modules[mods[0]])
    for path, abs_path, f in py:
        _python(g, path, abs_path, f, modules, scope, rid)
    g.notes["code: files in the tree"] = len(listing)
    g.notes["code: folders in the tree"] = len(folders)


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


def _python(g, path, abs_path, f, modules, scope, rid):
    text = _read(abs_path)
    if text is None:
        return
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        g.notes["code: python files that do not parse (skipped)"] += 1
        return
    mod = path[:-3].replace("/", ".").removesuffix(".__init__")
    me = modules[mod]
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    symbols = []  # (qualified name, kind, node): top-level functions and classes, and each class's methods
    for node in tree.body:
        if isinstance(node, funcs):
            symbols.append((f"{mod}.{node.name}", "function", node))
        elif isinstance(node, ast.ClassDef):
            symbols.append((f"{mod}.{node.name}", "class", node))
            symbols += [(f"{mod}.{node.name}.{n.name}", "method", n) for n in node.body if isinstance(n, funcs)]
    for qual, kind, n in symbols:
        sig = None if kind == "class" else f"{n.name}({', '.join(a.arg for a in n.args.args)})"
        sym = g.rec("CodeSymbol", "git", f"{rid}:{qual}", {"qualified_name": qual, "kind": kind, "signature": sig,
                    "line_start": n.lineno, "line_end": getattr(n, "end_lineno", None)}, scope, acl_from=f)
        g.link("DEFINED_IN", sym, f, "field")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if node.level == 0 else []
            for name in names:
                target = next((modules.get(c) or modules.get(f"\0{c}") for c in _prefixes(name)
                               if (modules.get(c) or modules.get(f"\0{c}")) not in (None, me)), None)
                if target:
                    g.link("IMPORTS", me, target, "field", kind="import")


def _prefixes(name):
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]
