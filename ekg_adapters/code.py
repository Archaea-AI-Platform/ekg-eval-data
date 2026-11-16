"""Code pillar, the code tree: a snapshot of the repository -> Folder, File, ContentVersion (docs), CodeSymbol.

Reads code/, in either form:
  - a listing: code/tree.jsonl or code/tree.json, rows {path, size?, sha?}
  - the files themselves: everything else under code/ (a checkout, or a copy of one), read as text
Every path becomes a File IN its Folder, and every folder IN its parent, so the tree is complete
even for files no commit touched. A File has the same key as the git adapter's, so the two merge.
README and documentation files are read like any other document (sections and pieces). Source files
of every language there is a grammar for are parsed by symbols.py: types, functions and methods become
CodeSymbols DEFINED_IN their file, with IMPORTS, EXTENDS and CALLS between symbols of the same repository.
The code host's standard path maps in the repository are read too: a labeler config (label -> path globs: the
folders and files it names HAS_LABEL that label) and a code-owners file (CodeOwnersRule, MATCHES, OWNED_BY).
Every File, also one only history names, is IN its Folder, so the changes under a folder are found by walking
Folder <-IN- (Folder <-IN-)* File <-CHANGES- Commit / PullRequest.
"""

import json
import re
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
    texts_all = dict(texts) if listing and all(a is None for _, a in listing) else {}
    for path, abs_path in listing:  # a checkout: the config files are read from it
        if abs_path is not None and (path in CODEOWNERS_PATHS or path.startswith(".github/")):
            texts_all[path] = _read(abs_path)
    # a file that history names but the snapshot no longer holds (deleted, moved) is IN its folder too, so a change to
    # it is found by walking Folder <-IN- Folder ... <-IN- File <-CHANGES- Commit / PullRequest like any other
    listed, in_tree = {p for p, _ in listing}, len(folders)
    for path, f in list(files.items()):
        if path not in listed and "/" in path:
            g.link("IN", f, folder(path.rsplit("/", 1)[0]), "field")
            g.notes["code: files no longer in the snapshot, put IN their folder"] += 1
    g.notes["code: folders only history names (no longer in the snapshot)"] = len(folders) - in_tree
    _path_configs(g, repo, scope, files, folders, {p: t for p, t in texts_all.items() if t})


# where the code hosts look for a code-owners file (GitHub and GitLab: the root, .github/ or .gitlab/, docs/)
CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", ".gitlab/CODEOWNERS", "docs/CODEOWNERS")
LABELER_DEFAULT = ".github/labeler.yml"   # actions/labeler's default configuration-path


def glob_rx(pattern: str):
    """A path glob (minimatch / gitignore style: ** any depth, * and ? within one part, {a,b} alternatives) -> regex."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "{" and "}" in pattern[i:]:
            j = pattern.index("}", i)
            out += "(?:" + "|".join(re.escape(x) for x in pattern[i + 1:j].split(",")) + ")"
            i = j + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _targets(pattern, files, folders, anchored=True):
    """The Folders and Files a path pattern names: "dir/**", "dir/**/*" or "dir/" is that folder (its contents are IN
    it); any other pattern is the files it matches, and the folders too when it names no extension-like file part."""
    p = pattern.strip()
    prefix = "" if anchored else "(?:.*/)?"
    m = re.fullmatch(r"(.+?)/(?:\*\*(?:/\*)?)?", p)
    if m and (p.endswith("/") or p.endswith("/**") or p.endswith("/**/*")):
        rx = re.compile(prefix + glob_rx(m.group(1)).pattern)
        return [folders[f] for f in folders if rx.match(f)], []
    rx = re.compile(prefix + glob_rx(p).pattern)
    hit_files = [files[f] for f in files if rx.match(f)]
    hit_folders = [folders[f] for f in folders if rx.match(f)] if not anchored or "." not in p.rsplit("/", 1)[-1] else []
    return hit_folders, hit_files


def _strings(node, skip_keys=("head-branch", "base-branch", "branch")):
    """Every string in a labeler rule, except those under the branch matchers (they name branches, not paths)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for x in node:
            yield from _strings(x, skip_keys)
    elif isinstance(node, dict):
        for k, v in node.items():
            if str(k) not in skip_keys:
                yield from _strings(v, skip_keys)


def _path_configs(g, repo, scope, files, folders, texts):
    """The code host's standard path maps, read from the repository's own files:
      - a labeler config (actions/labeler: label -> path globs; its path is the configuration-path a workflow passes,
        else .github/labeler.yml): each Folder or File a label's globs name HAS_LABEL that code Label (the config
        states the mapping; a negated glob takes its targets out again)
      - a code-owners file: one CodeOwnersRule per line, MATCHES the folders and files its pattern names, and each
        of those is OWNED_BY the accounts it names when this line is the last one matching it (source codeowners)."""
    import yaml
    label = g.ctx.get("code_label")
    configs = set()
    for path, text in texts.items():
        if path.startswith(".github/workflows/") and "actions/labeler" in text:
            try:
                wf = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                continue
            for job in (wf.get("jobs") or {}).values() if isinstance(wf, dict) else []:
                for step in (job or {}).get("steps") or []:
                    if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/labeler"):
                        configs.add(str((step.get("with") or {}).get("configuration-path") or LABELER_DEFAULT))
    if LABELER_DEFAULT in texts:
        configs.add(LABELER_DEFAULT)
    for path in sorted(configs):
        if path not in texts:
            g.notes["code: labeler config named by a workflow but its text is not local"] += 1
            continue
        if label is None:
            g.notes["code: labeler config with no code host labels to link (no github/ folder)"] += 1
            continue
        try:
            rules = yaml.safe_load(texts[path]) or {}
        except yaml.YAMLError:
            g.notes["code: labeler config not readable as YAML"] += 1
            continue
        for name, rule in rules.items() if isinstance(rules, dict) else []:
            keep, drop = set(), set()
            for glob in _strings(rule):
                neg = glob.startswith("!")
                fo, fi = _targets(glob.lstrip("!"), files, folders)
                (drop if neg else keep).update(r["props"]["_key"] for r in fo + fi)
            target = label(name)  # the host's own label, its name compared without regard to case
            if target is None:   # the config names a label the code host does not have: counted, never made
                g.notes["code: labeler config labels the code host does not have (not linked)"] += 1
                continue
            for key in sorted(keep - drop):
                g.link("HAS_LABEL", g.records[key], target, "field", rule_id=f"labeler:{path}")
                g.notes["code: folders and files carrying a label by the labeler config"] += 1
    for path in CODEOWNERS_PATHS:
        if path in texts:
            _codeowners(g, repo, scope, path, texts[path], files, folders)
            break  # the code host reads the first one it finds


def _codeowners(g, repo, scope, path, text, files, folders):
    by_login = {a["props"]["login"].casefold(): a for a in g.ctx.get("github_accounts", {}).values()
                if a["props"].get("login")}
    owner_of = {}  # target key -> (rule record, owners): the last matching line wins
    order = 0
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or (line.startswith("[") and "]" in line):  # comment, GitLab section
            continue
        pattern, *owners = line.split()
        order += 1
        rule = g.rec("CodeOwnersRule", "git", f"{repo['props']['native_id']}:{path}:{order}", {
            "pattern": pattern, "owners": owners, "order": order, "file_location": path}, scope, acl_from=repo)
        anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
        fo, fi = _targets(pattern.lstrip("/"), files, folders, anchored=anchored)
        for r in fo + fi:
            g.link("MATCHES", rule, r, "field")
            owner_of[r["props"]["_key"]] = (rule, owners)
        g.notes["code: code-owners lines"] += 1
    for key, (rule, owners) in owner_of.items():
        rule["props"]["effective"] = True  # it decides ownership of at least one path
        for o in owners:
            acct = by_login.get(o.lstrip("@").casefold()) if o.startswith("@") and "/" not in o else None
            if acct is None:
                g.notes["code: code owners not linked (a team, an address or an unknown login)"] += 1
                continue
            g.link("OWNED_BY", g.records[key], acct, "field", source="codeowners", rule_id=rule["props"]["native_id"])


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
