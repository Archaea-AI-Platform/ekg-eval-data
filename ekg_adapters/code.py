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
