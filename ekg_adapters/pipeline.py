"""Run every step on one fetch_apache.py download and report on it.

  1. adapters, one per source present (people, jira, linear, github, git, mail, slack, wiki + KIP decisions)
  2. linker: resolve cross-source references
  3. identity: attach accounts and addresses to Persons
  4. coverage: how well the pillars ended up joined
  5. validate every record and link with query-master's validator; optionally write the graph
"""

from __future__ import annotations

import collections
import json
import time
from pathlib import Path

from . import common
from .common import Graph, J
from .git import map_git
from .github import map_github
from .identity import resolve_identity
from .jira import map_jira
from .linear import map_linear
from .linker import resolve
from .mail import map_mail
from .people import map_people
from .slack import map_slack
from .wiki import map_wiki
from .code import map_code
from .docs import map_docs
from .forum import map_forum
from .issues import map_issues
from .settings import check_inputs, load_manifest, project_settings


def raw_pass(d: Path):
    """Each tool's JSON dropped into a record as-is: shows the data can't be loaded without an adapter."""
    out = collections.defaultdict(lambda: {"checked": 0, "valid": 0, "errors": collections.Counter()})

    def check(label, rtype, tool, native, props):
        key = common._ids.record_key(common.TENANT, tool, rtype, str(native))
        res = common._vrec({"id": common._ids.record_id(common.SECRET, key), "type": rtype,
                            "cores": list(common._reg.record(rtype).cores), "scope": [["public"]],
                            "props": {"_key": key, "native_id": str(native), "tenant_id": common.TENANT,
                                      "source_tool": tool, **props}})
        row = out[label]
        row["checked"] += 1
        row["valid"] += res.ok
        for e in res.errors[:3]:
            row["errors"][e[:140]] += 1

    for p in sorted((d / "jira/issues").glob("*.json")) if (d / "jira").is_dir() else []:
        for i in J(p)["issues"]:
            check("Jira issue as Ticket", "Ticket", "jira", i["id"], i["fields"])
    for p in sorted((d / "github/pulls").glob("pr-*.json")) if (d / "github").is_dir() else []:
        pr = J(p)["pull"]
        check("GitHub PR as PullRequest", "PullRequest", "github", pr["id"], pr)
    for line in open(d / "git/commits.jsonl", encoding="utf-8") if (d / "git/commits.jsonl").exists() else []:
        c = json.loads(line)
        check("git commit as Commit", "Commit", "git", c["sha"], c)
    for p in sorted((d / "wiki/pages").glob("*.json")) if (d / "wiki").is_dir() else []:
        for page in J(p)["results"]:
            check("wiki page as Document", "Document", "confluence", page["id"],
                  {k: v for k, v in page.items() if not k.startswith("_")})
    return {k: {"checked": v["checked"], "valid": v["valid"], "top_errors": dict(v["errors"].most_common(3))}
            for k, v in out.items()}


def coverage(g: Graph):
    """How joined-up the result is, from the links (stubs count as 'outside the window').
    A measure is reported only when the data has items of that kind."""
    by_id = {r["id"]: r for r in g.records.values()}
    links = [lk for lk, _, _ in g.links.values()]
    ends = collections.defaultdict(set)
    on_ticket = {lk["a"]: lk["b"] for lk in links if lk["type"] == "ON" and by_id[lk["b"]]["type"] == "Ticket"}
    joining = {"IMPLEMENTS", "MERGED_AS", "LINKS_TO", "ENACTS", "ABOUT", "MADE_IN", "ANNOUNCES", "DISCUSSES",
               "MENTIONS", "CREATED_FROM", "DISCUSSED_IN"}
    for lk in links:
        ta, tb = by_id[lk["a"]]["type"], by_id[lk["b"]]["type"]
        if lk["type"] in joining:
            ends[lk["a"]].add((lk["type"], tb))
            ends[lk["b"]].add((lk["type"], ta))
        if lk["a"] in on_ticket and lk["type"] in ("LINKS_TO", "MENTIONS"):  # a comment's link counts for its ticket
            ends[on_ticket[lk["a"]]].add((lk["type"], tb))

    def share(t, pred, where=lambda r: True):
        ids = [rid for rid, r in by_id.items() if r["type"] == t and not r["props"].get("_stub") and where(r)]
        return {"of": len(ids), "linked": sum(1 for rid in ids if pred(ends.get(rid, set())))}

    has = lambda *pairs: (lambda e: any(p in e for p in pairs))
    to_type = lambda *types: (lambda e: any(t in types for _, t in e))
    human_msg = lambda r: r["props"].get("kind") == "message"
    measures = {
        "tickets linked to a PR or commit": share("Ticket", to_type("PullRequest", "Commit")),
        "tickets announced or discussed on a mailing list": share("Ticket", to_type("EmailMessage")),
        "tickets announced, mentioned or discussed in chat": share("Ticket", to_type("Message", "Thread")),
        "PRs linked to a ticket": share("PullRequest", lambda e: ("IMPLEMENTS", "Ticket") in e or ("LINKS_TO", "Ticket") in e),
        "merged PRs linked to their merge commit": share("PullRequest", has(("MERGED_AS", "Commit")),
                                                         lambda r: r["props"].get("state") == "merged"),
        "commits linked to a PR": share("Commit", has(("MERGED_AS", "PullRequest"))),
        "commits linked to a ticket": share("Commit", has(("IMPLEMENTS", "Ticket"))),
        "KIP decisions linked to a discussion email": share("Decision", has(("MADE_IN", "EmailMessage"))),
        "KIP decisions linked to a ticket": share("Decision", has(("ABOUT", "Ticket"))),
        "KIP decisions enacted by a PR or commit": share("Decision", has(("ENACTS", "PullRequest"), ("ENACTS", "Commit"))),
        "emails linked to a ticket or decision": share("EmailMessage", to_type("Ticket", "Decision")),
        "chat messages linked to a ticket or PR": share("Message", to_type("Ticket", "PullRequest"), human_msg),
    }
    # a measure needs both of its sides in the data: no chat source, no "tickets discussed in chat"
    needs = {"tickets announced or discussed on a mailing list": "EmailMessage",
             "tickets announced, mentioned or discussed in chat": "Message",
             "PRs linked to a ticket": "Ticket", "commits linked to a ticket": "Ticket",
             "tickets linked to a PR or commit": "PullRequest", "commits linked to a PR": "PullRequest"}
    present = {r["type"] for r in by_id.values()}
    return {k: v for k, v in measures.items() if v["of"] and needs.get(k, "Ticket") in present}


ADAPTERS = [  # (source folder, step name, function, extra args from the manifest); trackers first, so later
              # adapters recognise their ticket keys, and the GitHub adapter before git so the repo record is its
    ("people", "people adapter", map_people, lambda m: [m["project"]]),
    ("jira", "jira adapter", map_jira, lambda m: [m["jira"], m.get("repo", "")]),
    ("linear", "linear adapter", map_linear, lambda m: []),
    ("github", "github adapter", map_github, lambda m: [m.get("repo", "")]),
    ("issues", "github issues adapter", map_issues, lambda m: []),
    ("git", "git adapter", map_git, lambda m: []),
    ("code", "code tree adapter", map_code, lambda m: []),
    ("mail", "mail adapter", map_mail, lambda m: []),
    ("slack", "slack adapter", map_slack, lambda m: []),
    ("forum", "forum adapter", map_forum, lambda m: []),
    ("wiki", "wiki adapter + decisions", map_wiki, lambda m: []),
    ("docs", "documents adapter", map_docs, lambda m: []),
]


def run(data: Path, query_master: Path, out: Path | None = None, raw_check: bool = False, log=print):
    """Run every adapter whose source folder is present in `data`, then link, resolve identity and validate."""
    common.bind(query_master)
    manifest, manifest_notes = load_manifest(data)
    common.set_tenant(manifest["tenant"])
    g = Graph(observed_at=manifest["fetched_at"])
    g.ctx["repo_name"] = manifest.get("repo", "")
    g.ctx["slack_workspace_name"] = manifest.get("slack_workspace")
    g.ctx["settings"] = settings = project_settings(manifest, data)
    timings, t0 = {}, time.time()
    for n in manifest_notes:
        log(f"  note: {n}")
    inputs = check_inputs(data, manifest)
    adapters = {}

    def step(name, fn, *a):
        t = time.time()
        log(f"  {name} ...")
        res = fn(*a)
        timings[name] = round(time.time() - t, 1)
        return res

    raw = step("raw pass", raw_pass, data) if raw_check else None
    for folder, name, fn, args in ADAPTERS:
        if folder not in inputs:
            continue
        check = inputs[folder]
        for p in check["problems"]:
            log(f"  {folder}: {p}")
        if not check["run"]:
            adapters[folder] = {"status": "skipped", "why": "; ".join(check["problems"])}
            log(f"  {name}: SKIPPED ({adapters[folder]['why']})")
            continue
        try:
            step(name, fn, g, data, *args(manifest))
            adapters[folder] = {"status": "ran", "problems": check["problems"]}
        except Exception as e:  # one adapter's failure is reported, and the others still run
            adapters[folder] = {"status": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
            log(f"  {name}: FAILED ({adapters[folder]['why']})")
    linked = step("cross-source linker", resolve, g)
    identity = step("identity resolution", resolve_identity, g)
    cov = coverage(g)
    rec_report, link_report, pillars = step("validation", g.validate)
    if out:
        step("write graph", g.write, out)

    rec_rows = {k: {"checked": v["checked"], "valid": v["valid"], "stubs": v["stubs"], "top_errors": dict(v["errors"].most_common(3))}
                for k, v in sorted(rec_report.items())}
    link_rows = {k: {"checked": v["checked"], "valid": v["valid"], "methods": dict(v["methods"]),
                     "top_errors": dict(v["errors"].most_common(3)),
                     "warnings": dict(v["warnings"].most_common(3))} for k, v in sorted(link_report.items())}
    return {
        "data": str(data), "window": manifest.get("window"), "project": manifest.get("project") or manifest.get("tenant"),
        "tenant": common.TENANT, "out": str(out) if out else None, "settings": settings, "adapters": adapters,
        "sources": [f for f, s in adapters.items() if s["status"] == "ran"],
        "totals": {"records": sum(v["checked"] for v in rec_rows.values()), "records_valid": sum(v["valid"] for v in rec_rows.values()),
                   "links": sum(v["checked"] for v in link_rows.values()), "links_valid": sum(v["valid"] for v in link_rows.values()),
                   "record_types": len({k.split(' (')[0] for k in rec_rows}), "link_types": len(link_rows)},
        "raw": raw, "records": rec_rows, "links": link_rows, "cross_pillar": dict(pillars.most_common()),
        "linker": linked, "identity": identity, "coverage": cov, "stubs": dict(g.stubs),
        "notes": dict(sorted(g.notes.items())), "timings_s": {**timings, "total": round(time.time() - t0, 1)},
    }
