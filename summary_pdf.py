"""Write the PDF summary: can this download be loaded into the EKG schema, which adapters it takes, and
what they produced when run.

Runs profile_raw.py and build_graph.py (the adapter pipeline, with the raw check) on a
fetch_apache.py download and builds the report from their output and the manifest, so
every number matches the data. The judgement parts (how each source compares with the
production APIs, the recommendations) are written here and should
be re-read when the schema or the dataset changes.

    python -I ekg-eval-data/summary_pdf.py                       # -> ~/Downloads/ekg-apache-data-summary.pdf
    python -I ekg-eval-data/summary_pdf.py --data ekg-eval-raw/apache-flink --out flink-summary.pdf

Needs reportlab, and the Segoe UI and Consolas fonts (Windows).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.platypus import CondPageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- look

FONTS = Path("C:/Windows/Fonts")
INK, MUTED, LINE, PANEL = (colors.HexColor(h) for h in ("#1d1f1b", "#5a5f56", "#d9d6cc", "#f2f0ea"))
ACCENT, WARN = colors.HexColor("#1f5f5b"), colors.HexColor("#9a5a00")


def register_fonts():
    for name, file in (("Body", "segoeui.ttf"), ("Body-Bold", "segoeuib.ttf"), ("Semi", "seguisb.ttf"),
                       ("Light", "segoeuisl.ttf"), ("Mono", "consola.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(FONTS / file)))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")


S = {
    "title": ParagraphStyle("title", fontName="Semi", fontSize=21, leading=26, textColor=INK, spaceAfter=4),
    "sub": ParagraphStyle("sub", fontName="Light", fontSize=11, leading=15, textColor=MUTED, spaceAfter=2),
    "h1": ParagraphStyle("h1", fontName="Semi", fontSize=14, leading=18, textColor=ACCENT, spaceBefore=14, spaceAfter=6),
    "h2": ParagraphStyle("h2", fontName="Body-Bold", fontSize=10.5, leading=14, textColor=INK, spaceBefore=8, spaceAfter=3),
    "p": ParagraphStyle("p", fontName="Body", fontSize=9.5, leading=13.5, textColor=INK, spaceAfter=5),
    "small": ParagraphStyle("small", fontName="Body", fontSize=8.3, leading=11, textColor=MUTED, spaceAfter=3),
    "cell": ParagraphStyle("cell", fontName="Body", fontSize=8.4, leading=11, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Body-Bold", fontSize=8.4, leading=11, textColor=INK),
    "head": ParagraphStyle("head", fontName="Body-Bold", fontSize=7.8, leading=10, textColor=MUTED),
    "bullet": ParagraphStyle("bullet", fontName="Body", fontSize=9.5, leading=13.5, textColor=INK, leftIndent=12,
                             bulletIndent=2, spaceAfter=3),
    "verdict": ParagraphStyle("verdict", fontName="Body", fontSize=10, leading=14.5, textColor=INK),
}


def c(t):
    return f'<font name="Mono" size="8.2">{t}</font>'


def P(t, s="p"):
    return Paragraph(t, S[s])


def n(x):
    return f"{x:,}"


def bullets(items):
    return [Paragraph(t, S["bullet"], bulletText="•") for t in items]


def table(header, rows, widths, num_cols=(), first_bold=True):
    data = [[P(h, "head") for h in header]]
    data += [[P(str(v), "cellb" if (i == 0 and first_bold) else "cell") for i, v in enumerate(r)] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, MUTED),
             ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
             ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    style += [("ALIGN", (col, 0), (col, -1), "RIGHT") for col in num_cols]
    t.setStyle(TableStyle(style))
    return t


def panel(flowables, color=ACCENT):
    t = Table([[flowables]], colWidths=[174 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PANEL), ("LINEBEFORE", (0, 0), (0, -1), 3, color),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


def h1(text):
    return [CondPageBreak(40 * mm), P(text, "h1")]


# ---------------------------------------------------------------- schema coverage

INTERNALS = {"RawRecord", "DerivationRun", "MintRecord", "MergeRecord", "SourceInstance", "SyncCursor", "RetentionPolicy",
             "ErasureRequest", "ContextTemplate", "ContextSlot", "AliasStoplist"}  # processing's own bookkeeping
# What would fill a record type that no source fills today. LLM extraction is the next step, not yet built.
FILLED_BY = [
    ("LLM extraction (knowledge layer)", "Next step: not built yet. An LLM reads messages, threads, comments, "
     "reviews, emails and documents and extracts these, each citing its sources.",
     {"Knowledge", "Guideline", "Requirement", "Question", "ActionItem", "Incident", "Regression", "Handover",
      "FeatureRequest", "Ask", "Convention", "ComplianceFinding", "Claim", "FactKey", "Conflict", "Topic"}),
    ("Rule-based processing", "Derived without an LLM: dated property history, secret and PII redaction, "
     "workflow tracking, per-org policies, the service catalog, rename aliases.",
     {"PropertyClaim", "Redaction", "WorkflowDefinition", "WorkflowStep", "WorkflowInstance", "AuthorityPolicy",
      "Service", "Alias"}),
    ("Sources not connected yet", "Calendars and calls, per-mailbox email (Gmail, Outlook), HR and the identity "
     "provider, file storage (Drive, SharePoint), tracker extras.",
     {"CalendarEvent", "Calendar", "MeetingSeries", "BusyTime", "Call", "Transcript", "Recording", "EmailCopy",
      "Mailbox", "Role", "Organization", "Domain", "LinkHolders", "FileMetadata", "Approval", "Template",
      "CustomerRequest", "WorkItem"}),
    ("GitHub organisation features", "Need the production GitHub connector with org access (public repos don't "
     "expose them, or they weren't fetched).",
     {"BranchRule", "Environment", "Deployment", "DeploymentStatus", "Alert", "CodeOwnersRule", "Check"}),
    ("Code parsing", "A parse of the repository's source files.", {"CodeSymbol", "Package", "TestCase", "StackTrace"}),
    ("Blocked by the schema", "A wiki page's sections can't attach to a Document; stored as Pieces for now.", {"DocSection"}),
]


# link types section 5 shows: its reference table and its example diagram
SECTION5_LINKS = {"LINKS_TO", "IMPLEMENTS", "MERGED_AS", "ANNOUNCES", "DISCUSSES", "MADE_IN", "ABOUT", "ENACTS", "CHILD_OF",
                  "RELATED_TO", "ASSIGNED_TO", "CREATED_BY", "AUTHORED_BY", "HAS_ALIAS", "HAS_ACCOUNT"}


# link types the endpoint-based rules would put in the wrong group
LINK_GROUP = {"KNOWS_ABOUT": "Needs the LLM step", "EXPLAINS": "Needs the LLM step", "HANDOVER_OF": "Needs the LLM step",
              "ASKED_BY": "Needs the LLM step", "MERGED_INTO": "Needs adapter additions or rule-based processing"}


def schema_coverage(query_master: Path, graphs: dict):
    """Coverage of the registry by the built graphs, which fields are filled, and real examples for the report."""
    sys.path.insert(0, str(Path(query_master).resolve()))
    from app.ekg.registry import get_registry
    reg = get_registry()
    all_rec = sorted(t for t in reg.records if t not in INTERNALS)
    all_link = sorted(name for name, spec in reg.links.items() if not spec.internal)
    core = lambda t: [f for f, v in reg.record(t).fields.items() if not v.computed and not v.extra]

    loaded = {}
    for name, d in graphs.items():
        recs = [json.loads(line) for line in open(Path(d) / "records.jsonl", encoding="utf-8")]
        links = [json.loads(line) for line in open(Path(d) / "links.jsonl", encoding="utf-8")]
        loaded[name] = (recs, links)
    recs = [r for rl in loaded.values() for r in rl[0]]
    links = [lk for rl in loaded.values() for lk in rl[1]]

    used_rec = {r["type"] for r in recs} & set(all_rec)
    used_link = {lk["type"] for lk in links} & set(all_link)
    seen = {}
    for r in recs:
        if r["type"] in used_rec:
            seen.setdefault(r["type"], set()).update(f for f in core(r["type"]) if r["props"].get(f) not in (None, "", []))
    total_fields = sum(len(core(t)) for t in seen)
    missing = set(all_rec) - used_rec
    groups = [(label, how, sorted(types & missing)) for label, how, types in FILLED_BY]
    llm_types = next(t for label, _, t in groups if label.startswith("LLM"))

    # fields filled today, for the main types; and what the LLM step would fill
    shown = ["Ticket", "PullRequest", "Commit", "EmailMessage", "Document", "Person", "Decision"]
    filled = {t: (sorted(seen.get(t, ())), sorted(set(core(t)) - seen.get(t, set()))) for t in shown if t in used_rec}
    llm_fields = {t: core(t) for t in ["Decision", "Knowledge", "Guideline", "Question", "ActionItem", "Incident", "Handover"]}

    # every link type the schema has that can join two pillars, by what produces it (or what would)
    pillar = lambda t: next((p for p in reg.record(t).pillars if p != "shared"), "shared") if t in reg.records else "shared"
    rtype = {r["id"]: r["type"] for r in recs}
    produced, cross_produced = set(), set()
    for lk in links:
        produced.add(lk["type"])
        pa, pb = pillar(rtype[lk["a"]]), pillar(rtype[lk["b"]])
        if pa != pb and "access" not in (pa, pb):
            cross_produced.add(lk["type"])
    schema_cross = sorted(l for l in all_link if any(
        pillar(a) != pillar(b) for a in reg.link(l).from_types if a in reg.records
        for b in reg.link(l).to_types if b in reg.records))
    group_types = {label: types for label, _, types in FILLED_BY}
    involves = lambda l, label: bool((set(reg.link(l).from_types) | set(reg.link(l).to_types)) & group_types[label])
    cross = {"Shown in this section": [], "Produced by our data, not shown above": [], "Needs the LLM step": [],
             "Needs a source not connected yet": [], "Needs GitHub organisation features or code parsing": [],
             "Needs adapter additions or rule-based processing": []}
    for l in schema_cross:
        if l in cross_produced:
            cross["Shown in this section" if l in SECTION5_LINKS else "Produced by our data, not shown above"].append(l)
        elif l in produced:
            cross["Produced by our data, not shown above"].append(l)  # produced, within one pillar in this data
        elif l in LINK_GROUP:  # named exceptions to the rules below
            cross[LINK_GROUP[l]].append(l)
        elif "knowledge" in (reg.link(l).pillars or ()):  # the spec's own knowledge-layer links
            cross["Needs the LLM step"].append(l)
        elif involves(l, "Sources not connected yet"):
            cross["Needs a source not connected yet"].append(l)
        elif involves(l, "GitHub organisation features") or involves(l, "Code parsing"):
            cross["Needs GitHub organisation features or code parsing"].append(l)
        elif involves(l, "Rule-based processing"):
            cross["Needs adapter additions or rule-based processing"].append(l)
        elif involves(l, "LLM extraction (knowledge layer)"):
            cross["Needs the LLM step"].append(l)
        else:
            cross["Needs adapter additions or rule-based processing"].append(l)

    kafka_recs, kafka_links = loaded["Kafka"]
    return {"cross_links": cross, "cross_total": len(schema_cross),"record_types": (len(used_rec), len(all_rec)), "link_types": (len(used_link), len(all_link)),
            "fields_ever": (sum(len(s) for s in seen.values()), total_fields), "missing": len(missing),
            "groups": groups, "after_llm": (len(used_rec) + len(llm_types), len(all_rec)),
            "filled": filled, "llm_fields": llm_fields,
            "graph_example": graph_example(kafka_recs, kafka_links), "vote_example": vote_example(kafka_recs)}


def graph_example(recs, links):
    """A real ticket's neighbourhood across tools, and the one person behind as many of its accounts as possible.

    Returns {role: record} plus "person_holds": the roles whose account or address that person really owns,
    so the picture never joins two different people's accounts."""
    by_id = {r["id"]: r for r in recs}
    out, into = {}, {}
    for lk in links:
        out.setdefault(lk["a"], []).append(lk)
        into.setdefault(lk["b"], []).append(lk)

    def one(items, ltype, side, rtype=None, **props):
        for lk in items or []:
            other = by_id[lk[side]]
            if lk["type"] == ltype and (rtype is None or other["type"] == rtype) and \
                    all(lk["props"].get(k) == v for k, v in props.items()):
                return other
        return None

    def owners(rec, ltype):  # people holding this account (HAS_ACCOUNT) or address (HAS_ALIAS)
        return {lk["a"] for lk in into.get(rec["id"], []) if lk["type"] == ltype and by_id[lk["a"]]["type"] == "Person"} if rec else set()

    best, best_score = None, -1
    for t in recs:
        if t["type"] != "Ticket" or not t["props"].get("title"):
            continue
        for lk in into.get(t["id"], []):
            pr = by_id[lk["a"]]
            if lk["type"] != "IMPLEMENTS" or pr["type"] != "PullRequest" or not pr["props"].get("title"):
                continue
            ex = {"ticket": t, "pr": pr}
            ex["commit"] = one(out.get(pr["id"]), "MERGED_AS", "b")
            ex["github"] = one(out.get(pr["id"]), "AUTHORED_BY", "b", "Account")
            ex["address"] = one(out.get(ex["commit"]["id"]) if ex["commit"] else [], "AUTHORED_BY", "b", "EmailAddress", role="author")
            ex["jira"] = one(out.get(t["id"]), "ASSIGNED_TO", "b", "Account") or one(out.get(t["id"]), "CREATED_BY", "b", "Account")
            ex["email"] = one(into.get(t["id"]), "ANNOUNCES", "a", "EmailMessage")
            ex["decision"] = one(into.get(t["id"]), "ABOUT", "a", "Decision")
            ex["page"] = one(out.get(ex["decision"]["id"]) if ex["decision"] else [], "MADE_IN", "b", "Document")
            ex["confluence"] = one(out.get(ex["page"]["id"]) if ex["page"] else [], "CREATED_BY", "b", "Account")
            holders = {}
            for role, ltype in (("github", "HAS_ACCOUNT"), ("jira", "HAS_ACCOUNT"), ("confluence", "HAS_ACCOUNT"),
                                ("address", "HAS_ALIAS")):
                for pid in owners(ex.get(role), ltype):
                    holders.setdefault(pid, []).append(role)
            pid, holds = max(holders.items(), key=lambda kv: len(kv[1]), default=(None, []))
            # the wiki author, when that is someone else: a second Person completes knowledge -> people
            other = next(iter(owners(ex["confluence"], "HAS_ACCOUNT") - {pid}), None) if "confluence" not in holds else None
            score = sum(ex[k] is not None for k in ex) + 2 * len(holds) + (2 if ex["email"] else 0) + (1 if other else 0)
            if score > best_score:
                best, best_score = {**ex, "person": by_id.get(pid), "person_holds": holds,
                                    "person2": by_id.get(other)}, score
    return best


def vote_example(recs):
    """A real KIP vote result email, as the input of the LLM extraction example."""
    import re
    for pattern in (r"^\[RESULT\]\s*\[VOTE\].*KIP-\d+", r"^\[VOTE\].*KIP-\d+"):
        for r in recs:
            if r["type"] == "EmailMessage" and re.search(pattern, r["props"].get("subject", ""), re.I):
                return {"subject": r["props"]["subject"], "sent_at": r["props"]["sent_at"]}
    return None


# ---------------------------------------------------------------- inputs

def run_inputs(data: Path, query_master: Path):
    """Run the profiler, the adapter pipeline, the Linear/Slack fixture build and the adapter tests.
    Returns (facts, build report, fixture build report, [(test name, passed)], schema coverage)."""
    with tempfile.TemporaryDirectory() as tmp:
        facts_path, report_path, fx_path = Path(tmp) / "facts.json", Path(tmp) / "build.json", Path(tmp) / "fixtures.json"
        print("profiling the download ...", flush=True)
        subprocess.run([sys.executable, "-I", str(HERE / "profile_raw.py"), str(data), "--json", str(facts_path)],
                       check=True, stdout=subprocess.DEVNULL)
        print("running the adapters (build_graph.py) ...", flush=True)
        res = subprocess.run([sys.executable, "-I", str(HERE / "build_graph.py"), "--data", str(data), "--raw-check",
                              "--query-master", str(query_master), "--json", str(report_path)], stdout=subprocess.DEVNULL)
        if res.returncode not in (0, 1) or not report_path.exists():  # 1 = something failed validation: report it
            raise SystemExit("build_graph.py failed; run it directly to see why")
        print("running the Linear and Slack adapters on the acme fixtures, and the adapter tests ...", flush=True)
        res = subprocess.run([sys.executable, "-I", str(HERE / "build_graph.py"), "--data", str(HERE / "fixtures" / "acme"),
                              "--out", str(Path(tmp) / "acme"), "--query-master", str(query_master), "--json", str(fx_path)],
                             stdout=subprocess.DEVNULL)
        if res.returncode not in (0, 1) or not fx_path.exists():
            raise SystemExit("build_graph.py on the fixtures failed; run it directly to see why")
        test = subprocess.run([sys.executable, "-I", str(HERE / "test_adapters.py")], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        tests = [(line.strip()[6:].strip(), line.strip().startswith("PASS")) for line in test.stdout.splitlines()
                 if line.strip().startswith(("PASS", "FAIL"))]
        if not tests:
            raise SystemExit("test_adapters.py printed no results; run it directly to see why")
        print("measuring schema coverage ...", flush=True)
        coverage = schema_coverage(query_master, {"Kafka": HERE.parent / "ekg-eval-out" / data.name,
                                                  "Acme": Path(tmp) / "acme"})
        return (json.loads(facts_path.read_text(encoding="utf-8")), json.loads(report_path.read_text(encoding="utf-8")),
                json.loads(fx_path.read_text(encoding="utf-8")), tests, coverage)


# ---------------------------------------------------------------- the report

def short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def graph_drawing(ex):
    """The ticket's neighbourhood in layers: knowledge, work, code, accounts, people. Every arrow runs between
    rows, so the link names sit in the row gaps and never collide with a box."""
    cols, w = 4, 174 * mm
    bw, bh, gap_y = 38 * mm, 13 * mm, 11 * mm
    gap_x = (w - cols * bw) / (cols - 1)
    rows = 6
    h = rows * bh + (rows - 1) * gap_y + 2 * mm
    d = Drawing(w, h)
    colours = {"Tickets": colors.HexColor("#9a4f1a"), "Code": colors.HexColor("#3b4f9a"),
               "Conversations": colors.HexColor("#8a2f52"), "Knowledge": colors.HexColor("#4a5a3a"),
               "Documents": colors.HexColor("#6b6220"), "People": colors.HexColor("#1f5f5b")}
    # (column, row): row 0 at the top
    grid = {"email": (0, 0), "decision": (2, 0), "ticket": (1, 1), "page": (3, 1), "pr": (0, 2), "commit": (0, 3),
            "github": (1, 3), "jira": (2, 3), "confluence": (3, 3), "address": (0, 4), "person": (1, 5),
            "person2": (3, 5)}
    xy = lambda k: (grid[k][0] * (bw + gap_x), h - 1 * mm - (grid[k][1] + 1) * bh - grid[k][1] * gap_y)

    def mask(addr):  # keep the domain, hide the person's address
        local, _, domain = (addr or "").partition("@")
        return f"{local[:1]}…@{domain}" if domain else addr

    p = lambda k, f: (ex.get(k) or {}).get("props", {}).get(f) or ""
    nodes = {
        "ticket": ("Tickets", "Jira ticket", f"{p('ticket', 'identifier')}: {p('ticket', 'title')}"),
        "pr": ("Code", "GitHub pull request", f"#{p('pr', 'number')}: {p('pr', 'title')}"),
        "commit": ("Code", "git commit", p("commit", "sha")[:10]),
        "github": ("People", "GitHub account", p("github", "login")),
        "jira": ("People", "Jira account", p("jira", "login")),
        "confluence": ("People", "Wiki account", p("confluence", "login")),
        "address": ("People", "Email address", mask(p("address", "address"))),
        "person": ("People", "Person", p("person", "full_name")),
        "person2": ("People", "Person", p("person2", "full_name")),
        "email": ("Conversations", "Mailing-list email", p("email", "subject")),
        "decision": ("Knowledge", "Decision (KIP)", p("decision", "statement")),
        "page": ("Documents", "Wiki page", p("page", "title")),
    }
    nodes = {k: v for k, v in nodes.items() if ex.get(k)}
    holds = set(ex.get("person_holds") or ())
    edges = [("email", "ticket", "ANNOUNCES"), ("decision", "ticket", "ABOUT"), ("decision", "page", "MADE_IN"),
             ("pr", "ticket", "IMPLEMENTS"), ("ticket", "jira", "ASSIGNED_TO"), ("page", "confluence", "CREATED_BY"),
             ("pr", "commit", "MERGED_AS"), ("pr", "github", "AUTHORED_BY"), ("commit", "address", "AUTHORED_BY")]
    edges += [("person", role, "HAS_ALIAS" if role == "address" else "HAS_ACCOUNT") for role in
              ("github", "jira", "confluence", "address") if role in holds]
    edges += [("person2", "confluence", "HAS_ACCOUNT")]
    centre = lambda k: (xy(k)[0] + bw / 2, xy(k)[1] + bh / 2)

    def leave(k, towards):  # where a line from k's centre towards a point crosses k's border
        (cx, cy), (tx, ty) = centre(k), towards
        dx, dy = tx - cx, ty - cy
        s = min((bw / 2) / abs(dx) if dx else 1e9, (bh / 2) / abs(dy) if dy else 1e9)
        return cx + dx * s, cy + dy * s

    for a, b, label in edges:
        if a not in nodes or b not in nodes:
            continue
        x1, y1 = leave(a, centre(b))
        x2, y2 = leave(b, centre(a))
        d.add(Line(x1, y1, x2, y2, strokeColor=MUTED, strokeWidth=0.8))
        ux, uy = x2 - x1, y2 - y1
        norm = (ux * ux + uy * uy) ** 0.5 or 1
        ux, uy, ah = ux / norm, uy / norm, 2.3 * mm
        d.add(Polygon([x2, y2, x2 - ah * ux + ah * 0.45 * uy, y2 - ah * uy - ah * 0.45 * ux,
                       x2 - ah * ux - ah * 0.45 * uy, y2 - ah * uy + ah * 0.45 * ux],
                      fillColor=MUTED, strokeColor=MUTED, strokeWidth=0.5))
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        lw = len(label) * 3.9 + 4
        d.add(Rect(mx - lw / 2, my - 1.6 * mm, lw, 4.2 * mm, fillColor=colors.white, strokeColor=None))
        d.add(String(mx, my - 0.2 * mm, label, fontName="Mono", fontSize=6.3, fillColor=INK, textAnchor="middle"))
    for k, (pillar, kind, label) in nodes.items():
        x, y = xy(k)
        c = colours[pillar]
        d.add(Rect(x, y, bw, bh, rx=2 * mm, ry=2 * mm, fillColor=colors.white, strokeColor=c, strokeWidth=1.2))
        d.add(Rect(x, y + bh - 4.4 * mm, bw, 4.4 * mm, rx=2 * mm, ry=2 * mm, fillColor=c, strokeColor=c))
        d.add(String(x + 2 * mm, y + bh - 3.2 * mm, f"{kind}  ·  {pillar}", fontName="Body-Bold", fontSize=6.2,
                     fillColor=colors.white))
        words, lines = label.split(), [""]
        for wd in words:
            if len(lines[-1]) + len(wd) + 1 > 28:
                lines.append("")
            lines[-1] = (lines[-1] + " " + wd).strip()
        for i, line in enumerate(lines[:2]):
            text = short(line + " …", 30) if i == 1 and len(lines) > 2 else line
            d.add(String(x + 2 * mm, y + bh - 7.8 * mm - i * 3.3 * mm, text, fontName="Body", fontSize=6.6, fillColor=INK))
    return d


def flow_drawing():
    """The plan, start to finish: what is done and what comes next."""
    steps = [("Collected an open-source dataset", "Apache Kafka: code, tickets, mailing lists, wiki, people", "done"),
             ("Designed the schema for every source", "one generic schema: people, code, tickets, documents, "
              "conversations, knowledge", "done"),
             ("Loaded the raw data into the schema", "it did not fit as-is: every source needed pre-processing", "done"),
             ("Built the adapters: schema partly filled", "all mapped data validates; about 43% of the schema's "
              "record types filled", "done"),
             ("LLM extraction to fill the rest", "decisions, guidelines, how-tos, questions, action items, who knows "
              "what", "next"),
             ("Build the knowledge graph", "one graph across all tools, answering questions with citations", "next"),
             ("Evaluate the knowledge graph", "run a set of test queries against the graph and check the answers "
              "are right and complete", "next")]
    w, bw, bh, gap = 174 * mm, 120 * mm, 13 * mm, 5 * mm
    h = len(steps) * bh + (len(steps) - 1) * gap + 2 * mm
    d = Drawing(w, h)
    x = (w - bw) / 2
    for i, (title, detail, state) in enumerate(steps):
        y = h - (i + 1) * bh - i * gap - 1 * mm
        c = ACCENT if state == "done" else WARN
        d.add(Rect(x, y, bw, bh, rx=2 * mm, ry=2 * mm, fillColor=PANEL, strokeColor=c, strokeWidth=1.2))
        d.add(Rect(x, y, 3 * mm, bh, fillColor=c, strokeColor=c))
        d.add(String(x + 6 * mm, y + bh - 5 * mm, f"{i + 1}. {title}", fontName="Body-Bold", fontSize=9, fillColor=INK))
        d.add(String(x + 6 * mm, y + 3 * mm, detail, fontName="Body", fontSize=7.6, fillColor=MUTED))
        d.add(String(x + bw - 3 * mm, y + bh - 5 * mm, "DONE" if state == "done" else "NEXT", fontName="Body-Bold",
                     fontSize=7.5, fillColor=c, textAnchor="end"))
        if i < len(steps) - 1:
            ax = w / 2
            d.add(Line(ax, y, ax, y - gap + 1.6 * mm, strokeColor=MUTED, strokeWidth=1))
            d.add(Polygon([ax, y - gap + 0.2 * mm, ax - 1.4 * mm, y - gap + 2 * mm, ax + 1.4 * mm, y - gap + 2 * mm],
                          fillColor=MUTED, strokeColor=MUTED))
    return d


def build(out: Path, manifest: dict, F: dict, B: dict, X: dict, tests: list, C: dict):
    rec = lambda label: B["records"].get(label, {}).get("checked", 0)
    lnk = lambda t: B["links"].get(t, {}).get("checked", 0)
    resolved, unresolved = B["linker"]["resolved"], B["linker"]["unresolved"]
    T = B["totals"]
    stubs = B["stubs"]
    project, jkey = manifest["project"].capitalize(), manifest["jira"]
    pct = lambda pair: f"{100 * pair[0] / pair[1]:.0f}%" if pair[1] else "–"
    frac = lambda pair: f"{n(pair[0])} of {n(pair[1])} ({pct(pair)})"
    story = []

    # ---- title and what the document covers
    story += [P("Engineering knowledge graph plan", "title"),
              P(f"Apache {project} open-source data: what the connectors must do, the adapters we built, and what they "
                "produced; plus Linear and Slack adapters", "sub"),
              Spacer(1, 6),
              P("<b>What this document covers.</b> We downloaded one real open-source project, Apache "
                f"{project}, from every tool it uses (code, tickets, mailing lists, wiki, people) and checked whether "
                "that data fits the engineering knowledge graph schema. It covers what was tested, the pre-processing "
                "every connector needs, what each source maps to, the adapters we built and ran, how they interlink "
                "the sources, how much of the schema the data fills and how LLM extraction will fill more of it as the "
                "next step, the Linear and Slack adapters, and the overall plan.", "p"),
              Spacer(1, 4)]

    # ---- 1. what was tested
    story += h1("1. What was tested")
    story += [table(["Pillar", "Source", "Downloaded"],
                    [["Code", "GitHub (PRs, reviews, comments, commits, files, CI runs, tags) and a git clone",
                      f"{n(F['prs'])} PRs, {n(rec('Review (github)'))} reviews, {n(F['commits'])} commits, "
                      f"{n(F['ci_runs'])} CI runs, {n(F['tags'])} tags"],
                     ["Tickets", "Jira, with each issue's full change history",
                      f"{n(F['jira_issues'])} issues, {n(rec('Comment (jira)'))} comments, {n(rec('Release (jira)'))} versions"],
                     ["Conversations", "the project's mailing lists", f"{n(F['emails'])} emails"],
                     ["Documents", "the Confluence wiki, with version history",
                      f"{n(F['wiki_pages'])} pages, {n(F['kip_pages'])} of them design proposals (KIPs)"],
                     ["People", "Apache's public rosters", f"{n(rec('Person (asf)'))} people, {n(F['pmc'])} on the "
                      "project's management committee"]],
                    [26 * mm, 84 * mm, 64 * mm])]

    # ---- 2. pre-processing
    story += h1("2. Pre-processing every connector needs")
    story += [P("Raw data from the tools does not fit the schema as it is: none of it passed the schema check before "
                "pre-processing. These are the steps every connector has to do, and why:"),
              table(["Step", "What we do", "Why"],
                    [["Rename fields", "Map each tool's field names onto the schema's names, and drop what the schema "
                      "doesn't need.", "Every tool names the same thing differently; the schema has one name for it."],
                     ["Give every item an ID", "Build a stable key and ID for each item from the tool's own permanent id.",
                      "So the same item is stored once, however often it is synced, and links can point at it."],
                     ["Put times in one format", "Convert every date and time to UTC, and note when a date is only known "
                      "to the day.", "Tools use different time zones and formats; questions like 'was it open on the "
                      "3rd?' need one clock."],
                     ["Mark who may see it", "Attach each item to the place it lives (repo, project, channel, space) and "
                      "who can open that place.", "Nobody must learn anything here that they couldn't open in the "
                      "tool itself."],
                     ["Clean up text", "Turn wiki markup, HTML and email formats into plain text, and keep descriptions "
                      "and page bodies as versioned text.", "So the text can be searched, quoted and compared over time."],
                     ["Keep the history", "Turn each change of status, assignee or label into a dated record, not just "
                      "the latest value.", "So we can answer what was true at any point in time."],
                     ["Find references", "Spot ticket keys, PR numbers, links and mentions that point from one tool to "
                      "another.", "These references are what connect the tools into one graph."],
                     ["Separate the noise", "Recognise bot posts, notification emails and duplicate copies.",
                      "An automatic notice about a ticket is not a new fact and must not be counted as one."],
                     ["Match people", "Link each tool's account to the one person behind it.",
                      "The same person has a different account in every tool."]],
                    [30 * mm, 76 * mm, 68 * mm])]

    # ---- 3. source by source
    story += h1("3. Source by source: what each one maps to")
    story += [table(["Source", "Its objects map to", "Links it provides"],
                    [["Jira", "issue → Ticket; status → TicketState (dated); comment → Comment; change → Activity; "
                      "fix version → Release; component and label → Label",
                      "ticket to ticket (blocks, duplicate, related, parent); ticket to PR"],
                     ["GitHub", "pull request → PullRequest; review → Review; review thread → Thread; comment → Comment; "
                      "CI run → PipelineRun; tag → Tag", "author, merger, approver; PR to ticket"],
                     ["git", "commit → Commit; changed file → File; author → EmailAddress",
                      "commit to parent commit, to the files it changed, to its PR"],
                     ["Mailing lists", "email → EmailMessage; thread → EmailThread; list → MailingList; sender → EmailAddress",
                      "sender, reply, the ticket a notification is about"],
                     ["Wiki", "page → Document; version → ContentVersion; section → Piece; space → Folder; "
                      "design proposal → Decision", "page tree, versions, where a decision was made"],
                     ["People directory", "person → Person; account → Account; committee → Group; chair → Position",
                      "person to their accounts, account to its groups"],
                     ["Linear", "issue → Ticket; team → Group; cycle → Iteration; project → Project; update → StatusUpdate",
                      "issue to PR, issue to the Slack message it came from"],
                     ["Slack", "message → Message; channel → Channel; thread → Thread; file → File",
                      "mentions of people, tickets and PRs; channel membership"]],
                    [26 * mm, 92 * mm, 56 * mm])]

    # ---- 4. the adapters
    count_of = lambda tool: sum(v["checked"] for k, v in B["records"].items() if k.endswith(f"({tool})"))
    story += h1("4. The adapters we built and ran")
    story += [P("One adapter per source does the pre-processing and mapping above. A shared linker then connects the "
                "sources, identity matching links accounts to people, and every record and link is checked against "
                "the schema."),
              table(["Step", "What it does", "Records out"],
                    [["Shared core", "IDs and keys, scopes, UTC times, text clean-up, merging repeated items", ""],
                     ["People adapter", "Directory → Person, Account, Group, Position", n(count_of("asf"))],
                     ["Jira adapter", "Issues, states and their history, versions, labels, comments, ticket links",
                      n(count_of("jira"))],
                     ["GitHub adapter", "Repo, PRs, reviews, threads, comments, branches, labels, files, CI, tags",
                      n(count_of("github"))],
                     ["git adapter", "Commits, parents, authors, files changed", n(count_of("git"))],
                     ["Mail adapter", "Lists, messages, threads, senders; notifications told apart from discussion",
                      n(count_of("mail"))],
                     ["Wiki adapter", "Space, pages, versions, sections, labels, comments; design proposals → Decision",
                      n(count_of("confluence") + count_of("derived"))],
                     ["Shared addresses", "One email address record shared by the directory, git and mail", n(count_of("email"))],
                     ["Cross-source linker", "Resolves references between tools; placeholders for items outside the window",
                      n(sum(stubs.values())) + " placeholders"],
                     ["Identity matching", "Links accounts and addresses to people", ""],
                     ["Schema check", "Every record and link against the schema",
                      f"{n(T['records_valid'])} of {n(T['records'])} records, {n(T['links_valid'])} of {n(T['links'])} links pass"]],
                    [36 * mm, 92 * mm, 46 * mm])]

    # ---- 5. interlinking
    def ref_row(label, how):
        return [label.split(":")[1].split("(")[0].strip() + " · " + label.split(":")[0], how,
                n(resolved.get(label, 0)), n(unresolved.get(label, 0)) if unresolved.get(label) else "–"]
    story += h1("5. How the sources are interlinked")
    story += [table(["Link · from → to", "How it is found", "Linked", "Unresolved"],
                    [ref_row("Ticket → PullRequest: LINKS_TO (integration)", "Jira's link to a GitHub pull request"),
                     ref_row("Comment → PullRequest: LINKS_TO (field)", "a PR link in a Jira comment"),
                     ref_row("PullRequest → Ticket: IMPLEMENTS (convention)", f"the {jkey} key in the PR title"),
                     ref_row("Commit → Ticket: IMPLEMENTS (convention)", f"the {jkey} key in the commit message"),
                     ref_row("PullRequest → Commit: MERGED_AS (convention)", "the PR number in the commit message"),
                     ref_row("EmailMessage → Ticket: ANNOUNCES (integration)", "a Jira notification email"),
                     ref_row("EmailMessage → Ticket: DISCUSSES (text_rule)", "a ticket key in an email's subject"),
                     ref_row("Decision → EmailMessage: MADE_IN (text_rule)", "the discussion or vote thread of a design proposal"),
                     ref_row("Decision → Ticket: ABOUT (text_rule)", "ticket keys in a design proposal"),
                     ref_row("PullRequest → Decision: ENACTS (convention)", "a KIP number in the PR title"),
                     ref_row("Ticket → Ticket: CHILD_OF (field)", "sub-task and parent"),
                     ref_row("Ticket → Ticket: RELATED_TO (field)", "Jira's issue links")],
                    [64 * mm, 72 * mm, 18 * mm, 20 * mm], num_cols=(2, 3)),
              P("Unresolved references point at items outside the downloaded window.", "small")]
    ex = C.get("graph_example")
    if ex:
        story += [CondPageBreak(150 * mm),
                  P("Example: one real ticket and what it connects to across tools", "h2"),
                  graph_drawing(ex),
                  P("Each box is a record (its tool and pillar in the header); each arrow is a link, named by its type. "
                    "Each Person is linked only to the accounts that really are theirs. Knowledge reaches people through "
                    "the wiki page a decision was made in and the account that wrote it; after the LLM step, a "
                    "decision will also link straight to the people who made it (DECIDED_BY).", "small")]

    # ---- 6. schema fill
    story += h1("6. How much of the schema the data fills")
    story += [table(["Measure", "Result"],
                    [["Mapped data that passes the schema", "100%"],
                     ["Record types the data fills", frac(C["record_types"])],
                     ["Link types the data fills", frac(C["link_types"])],
                     ["Fields filled, in the record types used", pct(C["fields_ever"])]],
                    [110 * mm, 64 * mm], num_cols=(1,)),
              P("These figures combine the Kafka data and the synthetic Linear and Slack sample.", "small"),
              CondPageBreak(70 * mm),  # keep the heading with its table
              P("Fields filled today, for the main record types:", "h2"),
              table(["Record type", "Filled from the data", "Not filled yet"],
                    [[t, ", ".join(f) or "–", ", ".join(e) or "–"] for t, (f, e) in C["filled"].items()],
                    [28 * mm, 86 * mm, 60 * mm]),
              CondPageBreak(60 * mm),
              P("Fields that need the LLM extraction step:", "h2"),
              P("<b>LLM extraction is not done yet; it is the next step.</b> The knowledge layer (decisions, guidelines, "
                "how-tos, questions, action items, incidents, hand-overs) is the biggest unfilled part of the schema. Its "
                "content sits in free text (email threads, chat, comments, wiki pages), so an LLM has to read the text "
                "and fill these fields, citing the sources it took them from:"),
              table(["Record type", "Fields the LLM will fill"],
                    [[t, ", ".join(f)] for t, f in C["llm_fields"].items()], [28 * mm, 146 * mm])]

    # ---- 7. Linear and Slack
    story += h1("7. Linear and Slack adapters")
    story += [P("We don't have live Linear or Slack data yet. So we built the Linear and Slack adapters against "
                "synthetic data created in each tool's real format, to check that they map correctly onto the schema. "
                "They will be run on the real data once it is available.")]

    # ---- 8. the plan
    story += h1("8. The plan, start to finish")
    story += [flow_drawing()]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Body", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, "Engineering knowledge graph plan")
        canvas.drawRightString(192 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title="Engineering knowledge graph plan",
                            subject="Fitting real tool data to the EKG schema, and the plan to fill it")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=str(HERE.parent / "ekg-eval-raw" / "apache-kafka"), help="a fetch_apache.py output folder")
    ap.add_argument("--query-master", default=str(HERE.parent / "query-master"), help="query-master checkout")
    ap.add_argument("--out", default=str(Path.home() / "Downloads" / "ekg-apache-data-summary.pdf"), help="PDF to write")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    data = Path(args.data)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    facts, report, fixtures, tests, coverage = run_inputs(data, Path(args.query_master))
    register_fonts()
    build(Path(args.out), manifest, facts, report, fixtures, tests, coverage)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
