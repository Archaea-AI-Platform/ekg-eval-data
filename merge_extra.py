"""Merge an extra-data drop (kafka-eval-shared/extra-data) into a fetch_apache.py download, in its own layout.

Safe to run again: nothing already in the download is replaced, items are matched by their own ids
(PR number, Jira key, page id, version number, Message-ID file hash), and every decision is in
<raw>/extra/merge_report.json.

  mail-history/, conversations/, knowledge/*.mbox  -> mail/<list>@<project>.apache.org/YYYY-MM.mbox
  */pulls/<n>/{pulls_n,..._reviews,..._comments,issues_n_comments,..._commits,..._files}.json
                                                    -> github/pulls/pr-<n>.json (the downloader's shape)
  cross/pull_<n>_*.json                             -> github/pulls/pr-<n>.json (no reviews or files)
  github-search/*, cross/search_*                   -> github/pulls/pr-<n>.json, partial ("_partial": "search"):
                                                       PRs known only from a search hit (no branch, files, reviews)
  code/run_jobs/<run>.json                          -> github/run_jobs/<run>.json (CI jobs per workflow run)
  tickets/*, cross/jira_*, cross/xpd_issues, knowledge/jira_kip_text_search
                                                    -> jira/issues/extra-NNNN.json (issues not already present)
  knowledge/wiki_kips, cross/wiki_kip*              -> wiki/pages/extra-NNNN.json (pages not already present)
  documents/versions/<page>.json, documents/comments.json
                                                    -> wiki/history/<page>.json (version bodies and comments merged in)
  everything else (rosters, board minutes, remote tags, fixVersion totals, restrictions, logs)
                                                    -> extra/<same path>, kept for reference

With --full-git, the commit export is extended from git/repo.git to the whole history of HEAD (rows
already in git/commits.jsonl are kept as they are; older commits are exported without rename detection,
which a blobless clone cannot do offline), and code/tree.jsonl lists every file at HEAD.

    python -I ekg-eval-data/merge_extra.py --extra <kafka-eval-shared>/extra-data --raw ekg-eval-raw/apache-kafka --full-git
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def J(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def merge_mail(extra, raw, project, rep):
    seen_hash = {}
    for src in sorted(extra.glob("*/*.mbox")):
        m = re.fullmatch(r"([a-z]+)-(\d{4}-\d{2})\.mbox", src.name)
        if not m or src.stat().st_size == 0:
            rep["mail"]["skipped (empty or unnamed)"].append(str(src.relative_to(extra)))
            continue
        h = sha(src)
        if h in seen_hash:
            rep["mail"]["skipped (same file twice in the drop)"].append(f"{src.relative_to(extra)} = {seen_hash[h]}")
            continue
        seen_hash[h] = str(src.relative_to(extra))
        dest = raw / "mail" / f"{m.group(1)}@{project}.apache.org" / f"{m.group(2)}.mbox"
        if dest.exists():
            if sha(dest) == h:
                rep["mail"]["already present"].append(dest.name)
                continue
            dest = dest.with_name(f"{m.group(2)}.extra.mbox")  # a different copy of a month we have: both are read,
            if dest.exists() and sha(dest) == h:                # duplicates are kept once by Message-ID
                rep["mail"]["already present"].append(dest.name)
                continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        rep["mail"]["added"].append(f"{dest.parent.name}/{dest.name}")


def merge_pulls(extra, raw, rep):
    pulls = raw / "github" / "pulls"
    have = {int(p.stem[3:]) for p in pulls.glob("pr-*.json")}
    for d in sorted(extra.glob("*/pulls/*")):
        if not d.is_dir() or not d.name.isdigit():
            continue
        n = int(d.name)
        if n in have:
            rep["github"]["pull already present"].append(n)
            continue
        part = lambda name: J(d / name) if (d / name).exists() else []
        write_json(pulls / f"pr-{n}.json", {
            "pull": J(d / f"pulls_{n}.json"), "reviews": part(f"pulls_{n}_reviews.json"),
            "review_comments": part(f"pulls_{n}_comments.json"), "issue_comments": part(f"issues_{n}_comments.json"),
            "commits": part(f"pulls_{n}_commits.json"), "files": part(f"pulls_{n}_files.json"), "timeline": [],
            "_source": f"extra-data/{d.relative_to(extra).as_posix()}"})
        have.add(n)
        rep["github"]["pull added (full detail)"].append(n)
    for f in sorted(extra.glob("cross/pull_*.json")):
        p = J(f)
        if p.get("number") in have:
            rep["github"]["pull already present"].append(p["number"])
            continue
        write_json(pulls / f"pr-{p['number']}.json", {"pull": p, "reviews": [], "review_comments": [], "issue_comments": [],
                                                       "commits": [], "files": [], "timeline": [],
                                                       "_source": f"extra-data/{f.relative_to(extra).as_posix()}"})
        have.add(p["number"])
        rep["github"]["pull added (no reviews or files)"].append(p["number"])
    hits = {}
    for f in sorted(extra.glob("github-search/*.json")) + sorted(extra.glob("cross/search_*.json")):
        for it in J(f).get("items", []):
            if "pull_request" in it:
                hits.setdefault(it["number"], (it, f))
    for n, (it, f) in sorted(hits.items()):
        if n in have:
            continue
        pr = it["pull_request"]
        write_json(pulls / f"pr-{n}.json", {
            "pull": {"id": f"number:{n}", "number": n, "title": it["title"], "state": it["state"],
                     "draft": bool(it.get("draft")), "created_at": it["created_at"], "closed_at": it.get("closed_at"),
                     "merged_at": pr.get("merged_at"), "user": it["user"], "body": it.get("body"),
                     "labels": it.get("labels") or [], "html_url": it.get("html_url")},
            "reviews": [], "review_comments": [], "issue_comments": [], "commits": [], "files": [], "timeline": [],
            "_partial": "search", "_source": f"extra-data/{f.relative_to(extra).as_posix()}"})
        have.add(n)
        rep["github"]["pull added (partial, from a search hit)"].append(n)
    jobs = sorted(extra.glob("code/run_jobs/*.json"))
    for f in jobs:
        dest = raw / "github" / "run_jobs" / f.name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
    rep["github"]["workflow runs with job lists"] = len(jobs)


def merge_jira(extra, raw, rep):
    have = set()
    for p in (raw / "jira" / "issues").glob("*.json"):
        have |= {i["key"] for i in J(p)["issues"]}
    found = {}
    for f in sorted(extra.glob("tickets/*.json")) + sorted(extra.glob("cross/jira_*.json")) + \
            sorted(extra.glob("cross/xpd_issues.json")) + sorted(extra.glob("knowledge/jira_*.json")):
        obj = J(f)
        groups = [obj["issues"]] if isinstance(obj, dict) and "issues" in obj else \
            [v for v in obj.values() if isinstance(v, list)] if isinstance(obj, dict) else []
        for issues in groups:
            for i in issues:
                if isinstance(i, dict) and i.get("key") and i.get("fields"):
                    found.setdefault(i["key"], i)
    new = [found[k] for k in sorted(found) if k not in have]
    rep["jira"]["issues in the drop"] = len(found)
    rep["jira"]["already present"] = len(found) - len(new)
    for i in range(0, len(new), 100):
        write_json(raw / "jira" / "issues" / f"extra-{i // 100:04d}.json", {"issues": new[i:i + 100], "_source": "extra-data"})
    rep["jira"]["added"] = len(new)


def merge_wiki(extra, raw, rep):
    have = set()
    for p in (raw / "wiki" / "pages").glob("*.json"):
        have |= {r["id"] for r in J(p)["results"]}
    found = {}
    for f in sorted(extra.glob("knowledge/wiki_*.json")) + sorted(extra.glob("cross/wiki_*.json")):
        obj = J(f)
        groups = [obj["results"]] if "results" in obj else [v for v in obj.values() if isinstance(v, list)]
        for pages in groups:
            for pg in pages:
                if isinstance(pg, dict) and pg.get("id") and pg.get("body"):
                    found.setdefault(pg["id"], pg)
    new = [found[k] for k in sorted(found) if k not in have]
    if new:
        write_json(raw / "wiki" / "pages" / "extra-0000.json", {"results": new, "_source": "extra-data"})
    rep["wiki"]["pages in the drop"], rep["wiki"]["pages added"] = len(found), len(new)

    comments = J(extra / "documents" / "comments.json") if (extra / "documents" / "comments.json").exists() else {}
    added_v = added_c = created = 0
    for f in sorted((extra / "documents" / "versions").glob("*.json")):
        pid, versions = f.stem, J(f)
        hp = raw / "wiki" / "history" / f"{pid}.json"
        hist = J(hp) if hp.exists() else None
        if hist is None:
            hist = {"id": pid, "versions": {"results": []}, "version_bodies": [], "comments": {"results": []}}
            created += 1
        listed = {v["number"] for v in hist.setdefault("versions", {}).setdefault("results", [])}
        bodies = {b["version"]["number"] for b in hist.setdefault("version_bodies", [])}
        for num, body in sorted(versions.items(), key=lambda kv: int(kv[0])):
            v = body.get("version") or {}
            if v.get("number") is None:
                continue
            if v["number"] not in bodies and (body.get("body") or {}).get("storage"):
                hist["version_bodies"].append(body)
                bodies.add(v["number"])
                added_v += 1
            if v["number"] not in listed:
                hist["versions"]["results"].append(v)
                listed.add(v["number"])
        hist["versions"]["results"].sort(key=lambda v: v["number"])
        cm = (comments.get(pid) or {}).get("results") or []
        if cm and not (hist.get("comments") or {}).get("results"):
            hist["comments"] = {"results": cm}
            added_c += len(cm)
        write_json(hp, hist)
    rep["wiki"]["version bodies added"], rep["wiki"]["history files created"] = added_v, created
    rep["wiki"]["page comments added"] = added_c


KEEP = ["people/committees.json", "people/board_minutes_kafka.html", "knowledge/remote_tags_*.txt",
        "tickets/fixversion_totals.json", "documents/restrictions.json", "documents_fetch.log", "failed_run_ids.txt",
        "github-search/*.json", "cross/search_*.json"]


def keep_rest(extra, raw, rep):
    for pat in KEEP:
        for f in sorted(extra.glob(pat)):
            dest = raw / "extra" / f.relative_to(extra)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
            rep["kept for reference"].append(f.relative_to(extra).as_posix())


# Reading a blobless clone must never fetch or repack: no lazy fetch, no automatic gc or maintenance.
NO_FETCH = ["-c", "core.quotePath=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
NO_FETCH_ENV = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}


def full_git(raw, rep):
    """The whole history of HEAD: rows already exported stay; older commits added without rename detection."""
    git = raw / "git" / "repo.git"
    path = raw / "git" / "commits.jsonl"
    have = {}
    for line in open(path, encoding="utf-8"):
        c = json.loads(line)
        have[c["sha"]] = line
    start, sep, head_end = "\x1e", "\x1f", "\x1d"
    fmt = start + sep.join(["%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%D", "%B"]) + head_end
    out = subprocess.run(["git", "--git-dir", str(git), *NO_FETCH, "log", "HEAD", "--name-status",
                          "--no-renames", f"--format={fmt}"], check=True, capture_output=True,
                         env=NO_FETCH_ENV).stdout.decode("utf-8", "replace")
    rows, added = [], 0
    for record in out.split(start)[1:]:
        header, _, files_block = record.partition(head_end)
        f = header.split(sep)
        if f[0] in have:
            rows.append(have.pop(f[0]).rstrip("\n"))
            continue
        files = [{"status": p[0], "path": p[-1]} for p in (ln.split("\t") for ln in files_block.strip("\n").split("\n"))
                 if len(p) >= 2 and p[0][:1] in "AMDRCT"]
        rows.append(json.dumps({"sha": f[0], "parents": f[1].split(), "author_name": f[2], "author_email": f[3],
                                "authored_at": f[4], "committer_name": f[5], "committer_email": f[6], "committed_at": f[7],
                                "refs": f[8], "message": f[9].rstrip("\n"), "files": files}, ensure_ascii=False))
        added += 1
    rows += [ln.rstrip("\n") for ln in have.values()]  # anything exported before that HEAD no longer reaches
    tmp = path.with_suffix(".part")
    tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
    tmp.replace(path)
    rep["git"] = {"commits": len(rows), "added from repo.git": added}
    # no --long: a blob's size needs the blob itself, which a blobless clone would go and download
    tree = subprocess.run(["git", "--git-dir", str(git), *NO_FETCH, "ls-tree", "-r", "HEAD"],
                          check=True, capture_output=True, env=NO_FETCH_ENV).stdout.decode("utf-8", "replace")
    (raw / "code").mkdir(exist_ok=True)
    n = 0
    with open(raw / "code" / "tree.jsonl", "w", encoding="utf-8") as fh:
        for line in tree.splitlines():
            meta, _, p = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == "blob":
                fh.write(json.dumps({"path": p, "sha": parts[2]}) + "\n")
                n += 1
    rep["code"] = {"files at HEAD": n}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--extra", required=True, help="the extra-data folder")
    ap.add_argument("--raw", required=True, help="the fetch_apache.py download to merge into")
    ap.add_argument("--full-git", action="store_true", help="export the whole history of HEAD and the file tree")
    a = ap.parse_args()
    extra, raw = Path(a.extra), Path(a.raw)
    manifest = J(raw / "manifest.json")
    project = manifest.get("project", "kafka")
    from collections import defaultdict
    rep = {"mail": defaultdict(list), "github": defaultdict(list), "jira": {}, "wiki": {}, "kept for reference": []}
    merge_mail(extra, raw, project, rep)
    merge_pulls(extra, raw, rep)
    merge_jira(extra, raw, rep)
    merge_wiki(extra, raw, rep)
    keep_rest(extra, raw, rep)
    if a.full_git:
        full_git(raw, rep)
    summary = {k: ({kk: (len(vv) if isinstance(vv, list) else vv) for kk, vv in v.items()} if isinstance(v, dict) else len(v))
               for k, v in rep.items()}
    rep["merged_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(raw / "extra" / "merge_report.json", rep)
    manifest["extra"] = {"merged_at": rep["merged_at"], "from": "kafka-eval-shared/extra-data", "summary": summary,
                         "git_history": "full history of HEAD" if a.full_git else "window only"}
    write_json(raw / "manifest.json", manifest)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
