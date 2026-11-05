"""Shared by every adapter: the graph being built, the record and link envelope, time and text helpers.

The Graph builds records and links in the spec's two shapes. It fills what the
registry already knows (a record type's cores, a link type's canonical predicate),
merges a repeated record by `_key` and a repeated fact by `claim_id` (R24), and
validates everything with query-master's own validator at the end.
"""

from __future__ import annotations

import collections
import datetime as dt
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

TENANT = "apache"
SECRET = "ekg-eval-secret"  # ids only need to be stable within one tenant's run

# Which pillar a record belongs to, by the tool that produced it (for the cross-pillar counts).
PILLAR_OF_TOOL = {"asf": "People", "email": "People", "jira": "Tickets", "linear": "Tickets", "github": "Code",
                  "git": "Code", "mail": "Conversations", "slack": "Conversations", "forum": "Conversations",
                  "confluence": "Documents", "docs": "Documents",
                  "derived": "Knowledge", "ekg": "Access"}

_reg = _ids = _vrec = _vlink = None


def bind(query_master: Path):
    """Import the registry, id rules and validator from a query-master checkout."""
    global _reg, _ids, _vrec, _vlink
    import sys
    sys.path.insert(0, str(Path(query_master).resolve()))
    from app.ekg.registry import get_registry
    from app.ekg.schema import ids
    from app.ekg.schema.validate import validate_link, validate_record
    _reg, _ids, _vrec, _vlink = get_registry(), ids, validate_record, validate_link
    return _reg


# ---------------------------------------------------------------- time

def utc(value):
    """An ISO time with an offset and/or milliseconds -> YYYY-MM-DDTHH:MM:SSZ."""
    if not value:
        return None
    s = re.sub(r"\.\d+", "", value.replace("Z", "+00:00"))
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)  # Jira's +0000
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(value):
    """A date given to the day: that day at UTC midnight (store with precision day)."""
    return f"{value[:10]}T00:00:00Z" if value else None


def ldap_time(value):
    return dt.datetime.strptime(value, "%Y%m%d%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def from_dt(value: dt.datetime):
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- text

def jira_text(markup):
    """Jira wiki markup -> plain text (good enough for search and extraction)."""
    if not markup:
        return None
    t = re.sub(r"\{(code|noformat)(:[^}]*)?\}", "", markup)
    t = re.sub(r"\{quote\}", "", t)
    t = re.sub(r"\[([^|\]]+)\|([^\]]+)\]", r"\1 (\2)", t)          # [text|url]
    t = re.sub(r"^h[1-6]\.\s*", "", t, flags=re.M)                   # headings
    t = re.sub(r"(?<!\w)[*_+-]([^*_+\n-]+)[*_+-](?!\w)", r"\1", t)   # *bold* _italic_
    t = re.sub(r"\{\{([^}]+)\}\}", r"\1", t)                         # {{monospace}}
    return t.strip()


class _Sections(HTMLParser):
    """Confluence storage XHTML -> [(level, heading, text)]; level 0 is text before the first heading."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections, self.level, self.heading, self.buf, self.in_h = [], 0, "", [], None

    def handle_starttag(self, tag, attrs):
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.in_h, self.heading = int(tag[1]), ""
        elif tag in ("p", "li", "tr", "br", "div"):
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if self.in_h and tag == f"h{self.in_h}":
            self.level, self.in_h = self.in_h, None

    def handle_data(self, data):
        if self.in_h:
            self.heading += data
        else:
            self.buf.append(data)

    def _flush(self):
        text = re.sub(r"\n\s*\n+", "\n", "".join(self.buf)).strip()
        if text or self.heading:
            self.sections.append((self.level, html.unescape(self.heading.strip()), text))
        self.buf = []

    def close(self):
        super().close()
        self._flush()


def xhtml_sections(body):
    p = _Sections()
    p.feed(body or "")
    p.close()
    return p.sections


def xhtml_text(body):
    return "\n".join(f"{h}\n{t}" if h else t for _, h, t in xhtml_sections(body)).strip()


EXT_LANG = {"java": "java", "scala": "scala", "py": "python", "kt": "kotlin", "js": "javascript", "ts": "typescript",
            "go": "go", "sh": "shell", "gradle": "gradle", "json": "json", "md": "markdown", "html": "html",
            "xml": "xml", "yml": "yaml", "yaml": "yaml", "properties": "properties"}


def file_props(path):
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    kind = ("test" if re.search(r"(^|/)(test|tests)/|Test\.\w+$|_test\.py$", path) else
            "doc" if ext in ("md", "html", "txt") or path.startswith("docs/") else
            "config" if ext in ("json", "yml", "yaml", "xml", "properties", "gradle", "toml") else "source")
    return {"path": path, "name": name, "language": EXT_LANG.get(ext), "kind": kind}


# ---------------------------------------------------------------- the graph

class Graph:
    def __init__(self, observed_at: str):
        self.observed_at = observed_at
        self.records: dict[str, dict] = {}
        self.links: dict[str, tuple[dict, str, str]] = {}
        self.notes = collections.Counter()
        self.refs = []          # cross-source references, resolved by linker.resolve()
        self.ctx = {}           # lookups the adapters share (tickets by key, PRs by number, ...)
        self.stubs = collections.Counter()

    # ---- records
    def rec(self, rtype, tool, native, props, scope=None, acl_from=None):
        key = _ids.record_key(TENANT, tool, rtype, str(native))
        clean = {k: v for k, v in props.items() if v is not None and v != [] and v != ""}
        if key in self.records:
            r = self.records[key]
            for k, v in clean.items():  # a later, fuller sighting fills gaps; it never blanks a field
                r["props"].setdefault(k, v)
            if r["props"].get("_stub") and not props.get("_stub"):
                r["props"].pop("_stub", None)
            return r
        r = {"id": _ids.record_id(SECRET, key), "type": rtype, "labels": [rtype],
             "cores": list(_reg.record(rtype).cores), "scope": scope or [["public"]],
             "props": {"_key": key, "native_id": str(native), "tenant_id": TENANT, "source_tool": tool, **clean}}
        if acl_from:
            r["acl_from"] = acl_from["id"]
        self.records[key] = r
        return r

    def stub(self, rtype, tool, native, props, scope):
        """A record named by a reference but not in the download (outside the window or the cap)."""
        key = _ids.record_key(TENANT, tool, rtype, str(native))
        if key not in self.records:
            self.stubs[rtype] += 1
            return self.rec(rtype, tool, native, {**props, "_stub": True}, scope)
        return self.records[key]

    # ---- links
    def link(self, ltype, a, b, method="field", confidence=1.0, evidence=None, **own):
        """method "event" is shorthand for a field read from the tool's event history (at_source: event)."""
        at_source = "event" if method == "event" else "snapshot" if method == "snapshot" else "tool"
        method = "field" if method == "event" else method
        spec = _reg.link(ltype)
        fk = _ids.fact_key(a["id"], ltype, b["id"], role=own.get("role"), kind=own.get("kind"),
                           aspect=own.get("aspect"), valid_from=own.get("valid_from"))
        cid = _ids.claim_id(SECRET, TENANT, fk)
        ev = [e["id"] if isinstance(e, dict) else e for e in (evidence or [a])]
        if cid in self.links:  # same fact again: add the evidence (R24)
            lk = self.links[cid][0]
            lk["props"]["evidence"] = sorted(set(lk["props"]["evidence"]) | set(ev))
            lk["props"]["support_count"] = lk["props"].get("support_count", 1) + 1
            return lk
        acl = []
        for clause in a["scope"] + b["scope"]:
            if clause not in acl:
                acl.append(clause)
        lk = {"a": a["id"], "b": b["id"], "type": ltype, "acl": acl,
              "props": {"claim_id": cid, "predicate": "internal" if spec.internal else spec.relation,
                        "canon_dir": "forward", "method": method, "confidence": confidence, "evidence": ev,
                        "support": "any", "at_source": at_source, "observed_at": self.observed_at,
                        **{k: v for k, v in own.items() if v is not None}}}
        self.links[cid] = (lk, a["props"]["_key"], b["props"]["_key"])
        return lk

    def ref(self, source, ltype, target_type, target_key, method, confidence=1.0, stub=False, **own):
        """Queue a reference to something another adapter may have produced (a ticket key, a PR number, a KIP)."""
        self.refs.append((source, ltype, target_type, target_key, method, confidence, stub, own))

    # ---- finish
    def validate(self):
        by_key = self.records
        rec_report = collections.defaultdict(lambda: {"checked": 0, "valid": 0, "stubs": 0, "errors": collections.Counter()})
        for r in by_key.values():
            stub = r["props"].pop("_stub", False)  # a working flag, not a schema field
            res = _vrec(r)
            row = rec_report[f"{r['type']} ({r['props']['source_tool']})"]
            row["checked"] += 1
            row["valid"] += res.ok
            row["stubs"] += bool(stub)
            for e in res.errors:
                row["errors"][re.sub(r"'[^']*'", "'…'", e)[:140]] += 1
        link_report = collections.defaultdict(lambda: {"checked": 0, "valid": 0, "methods": collections.Counter(),
                                                       "errors": collections.Counter(), "warnings": collections.Counter()})
        pillars = collections.Counter()
        for lk, ka, kb in self.links.values():
            a, b = by_key[ka], by_key[kb]
            res = _vlink(lk, a["type"], b["type"])
            row = link_report[lk["type"]]
            row["checked"] += 1
            row["valid"] += res.ok
            row["methods"][lk["props"]["method"]] += 1
            for e in res.errors:
                row["errors"][e[:140]] += 1
            for w in res.warnings:
                row["warnings"][w[:140]] += 1
            pa = PILLAR_OF_TOOL[a["props"]["source_tool"]]
            pb = PILLAR_OF_TOOL[b["props"]["source_tool"]]
            if pa != pb:
                pillars[" ↔ ".join(sorted((pa, pb)))] += 1
        return rec_report, link_report, pillars

    def write(self, out: Path):
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "records.jsonl", "w", encoding="utf-8") as f:
            for r in self.records.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(out / "links.jsonl", "w", encoding="utf-8") as f:
            for lk, _, _ in self.links.values():
                f.write(json.dumps(lk, ensure_ascii=False) + "\n")


def J(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def set_tenant(tenant: str):
    global TENANT
    TENANT = tenant


def ticket_rx(g: "Graph"):
    """Matches a ticket key of any tracker seen so far (Jira project keys, Linear team keys)."""
    prefixes = sorted(g.ctx.get("ticket_prefixes", ()), key=len, reverse=True)
    if not prefixes:
        return re.compile(r"(?!x)x")  # matches nothing
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in prefixes) + r")-\d+\b")
