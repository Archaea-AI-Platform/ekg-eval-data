"""Code named in a knowledge item's own words -> EXPLAINS that File or CodeSymbol, in code (no model call).

The schema's EXPLAINS starts only at a Knowledge record: "knowledge item A explains B (why a piece of code ... is
as it is)". Knowledge records come from the model stage (fill.py, or an earlier run's <graph>/llm/), which never
writes EXPLAINS itself; what code an item is about is written in its text, so it is read here, after that stage:

  Type#member, Type.member, Type::member   (any depth: pkg.Type.member)  -> the CodeSymbol whose qualified name
                                            ends in those parts; a member the parse has no symbol for falls back
                                            to its type. The symbol's File is explained too.
  a path (dir/name.ext) or a file name     -> the one File whose path is it, or ends in "/" + it

A name that resolves to more than one symbol or file (two types of one name in different packages, a README in
every folder) is counted, never guessed. Nothing here knows a language: names are matched against the symbols
the code parse made. method text_rule, confidence 0.7 (a naming convention); kind rationale when the item is one.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path

from . import common, extract_schema
from .common import Graph

# a dotted / hashed / ::-joined name whose last type part is capitalised, then a member: Type#run, pkg.Type.run()
IDENT = re.compile(r"(?<![\w.#:/$-])((?:[A-Za-z_]\w*(?:\.|::))*[A-Z]\w*)(#|\.|::)([A-Za-z_]\w*)(?![\w$])")
# a path with at least one folder, or a bare file name with a short extension
PATH = re.compile(r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]\w{0,9}|[A-Za-z_][\w-]*\.[A-Za-z]\w{0,9})(?![\w/])")
TEXT_FIELDS = ("title", "statement", "steps", "applies_when", "instead", "avoid", "consequence")
SYMBOL_KINDS_SKIPPED = ("module", "file", "package")   # a file's own symbol: the File says it
TYPE_KINDS = ("class", "interface", "enum", "struct", "trait", "record", "object", "type")


class CodeIndex:
    """Files by path and by name; symbols by the last two parts of their qualified names; each symbol's file."""

    def __init__(self):
        self.file_by_path, self.files_by_name = {}, collections.defaultdict(list)
        self.sym_by_tail = collections.defaultdict(list)   # "Type.member" (last two parts) -> [symbol]
        self.sym_by_last = collections.defaultdict(list)   # "Type" (a one-part tail) -> [symbol]
        self.file_of = {}                                  # symbol id -> file record id
        self.rec = {}

    def add(self, r: dict) -> None:
        p = r["props"]
        if p.get("_stub"):
            return
        if r["type"] == "File" and p.get("path"):
            self.rec[r["id"]] = r
            self.file_by_path[extract_schema.repo_path(p["path"])] = r
            self.files_by_name[p["path"].rsplit("/", 1)[-1]].append(r)
        elif r["type"] == "CodeSymbol" and p.get("qualified_name") and p.get("kind") not in SYMBOL_KINDS_SKIPPED:
            self.rec[r["id"]] = r
            parts = p["qualified_name"].split(".")
            self.sym_by_last[parts[-1]].append(r)
            if len(parts) > 1:
                self.sym_by_tail[".".join(parts[-2:])].append(r)

    def defined_in(self, sym_id: str, file_id: str) -> None:
        self.file_of[sym_id] = file_id

    def __bool__(self):
        return bool(self.rec)

    def _symbols(self, parts: list, kinds=None) -> list:
        """The symbols (of these kinds, when given) whose qualified name ends with these parts: [] for none, None
        when they are more than one qualified name (overloads share one)."""
        pool = self.sym_by_tail.get(".".join(parts[-2:]), []) if len(parts) > 1 else self.sym_by_last.get(parts[0], [])
        want = ".".join(parts)
        hits = [s for s in pool if (s["props"]["qualified_name"] == want or s["props"]["qualified_name"].endswith("." + want))
                and (kinds is None or s["props"].get("kind") in kinds)]
        return hits if len({s["props"]["qualified_name"] for s in hits}) == 1 else ([] if not hits else None)

    def identifier(self, owner: str, member: str):
        """(targets, how): the symbol(s) and file a Type#member names; targets None when the name is ambiguous."""
        parts = re.split(r"\.|::", owner) + [member]
        hits = self._symbols(parts)
        how = "member"
        if not hits:   # a member the parse made no symbol for (a field, a generated method): its type
            if hits is None:
                return None, "ambiguous"
            hits, how = self._symbols(parts[:-1], TYPE_KINDS), "type of an unparsed member"
        if hits is None:
            return None, "ambiguous"
        if not hits:
            return [], "unknown"
        out = [hits[0]] if len(hits) == 1 else []     # overloads: the name does not say which one
        files = {self.file_of.get(s["id"]) for s in hits}
        if len(files) == 1 and None not in files:
            out.append(self.rec[files.pop()])
        return out, how

    def path(self, text: str):
        text = extract_schema.repo_path(text)   # "/src/a.py": the repository's root, the same file as "src/a.py"
        if "/" in text:
            if text in self.file_by_path:
                return [self.file_by_path[text]], "path"
            hits = [f for p, f in self.file_by_path.items() if p.endswith("/" + text)] if len(text) > 4 else []
        else:
            hits = self.files_by_name.get(text, [])
        if len(hits) > 1:
            return None, "ambiguous"
        return hits, "path" if hits else "unknown"


def _text(r: dict) -> str:
    p = r["props"]
    parts = []
    for f in TEXT_FIELDS:
        v = p.get(f)
        parts.extend(str(x) for x in v) if isinstance(v, list) else parts.append(str(v)) if v else None
    return "\n".join(parts)


def _sources(r: dict) -> list:
    """The real sources a derived record cites (its _stated quotes), never itself (R51)."""
    return sorted({src for v in (r["props"].get("_stated") or {}).values() if isinstance(v, dict)
                   for src in (v.get("by") or {})})


def explains(g: Graph, knowledge, index: CodeIndex, notes: collections.Counter | None = None) -> int:
    """EXPLAINS from each Knowledge record to the code its own text names; returns how many links were made."""
    notes = notes if notes is not None else g.notes
    made = 0
    for k in knowledge:
        text = _text(k)
        if not text:
            continue
        targets = {}
        spans = []
        for m in IDENT.finditer(text):
            if m.group(2) == "." and re.split(r"\.|::", m.group(1))[-1] + "." + m.group(3) in index.files_by_name:
                continue   # Name.ext is a file's name: read as a path below
            found, how = index.identifier(m.group(1), m.group(3))
            notes[f"code names in knowledge: {how}"] += 1
            for t in found or []:
                targets[t["id"]] = t
            spans.append(m.span())
        for m in PATH.finditer(text):
            if any(a <= m.start() < b for a, b in spans) and "/" not in m.group(1):
                continue   # Type.member, read above
            found, how = index.path(m.group(1).rstrip("."))
            if found or how == "ambiguous":
                notes[f"file names in knowledge: {how}"] += 1
            for t in found or []:
                targets[t["id"]] = t
        evidence = _sources(k) or None
        for t in targets.values():
            g.link("EXPLAINS", k, t, "text_rule", 0.7, evidence=evidence,
                   kind="rationale" if k["props"].get("kind") == "rationale" else None)
            notes[f"EXPLAINS -> {t['type']}"] += 1
            made += 1
    return made


# ---------------------------------------------------------------- over a built graph folder

TYPES_READ = ('"type": "File"', '"type": "CodeSymbol"', '"type": "Knowledge"')


def _lines(path: Path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            yield from f


def derive(graph: Path, query_master: Path, log=print) -> dict:
    """Read a graph folder (the adapters' records/links.jsonl and the model stage's llm/), write the links derived
    here to <graph>/derived/links.jsonl (sqlite_store.save reads them as a stage of their own) and a report."""
    common.bind(query_master)
    graph = Path(graph)
    secret = None
    try:   # the ids are keyed with the download's id_secret, when its manifest has one
        data = json.loads((graph / "report.json").read_text(encoding="utf-8")).get("data")
        from .settings import load_manifest
        secret = load_manifest(Path(data))[0].get("id_secret") if data else None
    except (OSError, ValueError, TypeError, KeyError):
        pass
    index, knowledge, tenant, observed = CodeIndex(), {}, None, None
    for name in ("records.jsonl", "llm/records.jsonl"):
        for line in _lines(graph / name):
            if any(t in line[:300] for t in TYPES_READ):
                r = json.loads(line)
                tenant = tenant or r["props"].get("tenant_id")
                if r["type"] == "Knowledge":
                    knowledge[r["id"]] = r   # the model stage's copy replaces the adapters'
                else:
                    index.add(r)
    for name in ("links.jsonl", "llm/links.jsonl"):
        for line in _lines(graph / name):
            if observed is None:
                observed = json.loads(line)["props"].get("observed_at")
            if '"type": "DEFINED_IN"' in line[:300]:
                lk = json.loads(line)
                index.defined_in(lk["a"], lk["b"])
    if tenant:
        common.set_tenant(tenant, secret)
    g = Graph(observed_at=observed or "1970-01-01T00:00:00Z")
    made = explains(g, knowledge.values(), index) if index else 0
    out = graph / "derived"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "links.jsonl", "w", encoding="utf-8") as f:
        for lk, _, _ in g.links.values():
            f.write(json.dumps(lk, ensure_ascii=False) + "\n")
    report = {"knowledge_read": len(knowledge), "files": len(index.file_by_path), "links": made,
              "notes": dict(sorted(g.notes.items()))}
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"  derived: {made:,} EXPLAINS links from {len(knowledge):,} knowledge items -> {out}")
    return report
