"""Lay one of our downloads (test_data_sorce_graph/<name>) out the way ekg-eval-data's adapters read it.

Layout only, as shim_flink.py: every file is our raw API response, linked or regrouped per PR; nothing is
parsed into schema shape. Per-PR commits, files and timeline were never downloaded, so they are [].

    python shim_all.py <test_data_sorce_graph/name> <out dir> <people dir or ->
"""
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

SRC, OUT, PEOPLE = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
raw, name = SRC / "raw", SRC.name


def pages(folder):
    return [x for p in sorted(folder.glob("page_*.json")) for x in json.load(open(p))]


def link(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    dst.symlink_to(Path(src).resolve())


gm = json.load(open(raw / "git/manifest.json"))
repo = json.load(open(raw / "github/repo.json"))
since, fetched = gm["since"], gm["fetched_at"]
has_jira = (raw / "jira/search").is_dir()
ASF_PROJECT = {"iceberg-python": "iceberg"}  # the roster names the project, not the repo
manifest = {"project": ASF_PROJECT.get(name, name), "repo": repo["full_name"], "tenant": "apache", "fetched_at": fetched,
            "window": {"since": since, "until": fetched[:10]},
            "lists": sorted(d.name for d in (raw / "mail").iterdir() if d.is_dir()),
            # public data: the committer roster is only part of who works on the project, so contributors are
            # still inferred from commit and mail authorship (inferred: they name a person, grant nothing)
            "settings": {"infer_people": True}}
if has_jira:
    manifest["jira"] = json.load(open(raw / "jira/project.json"))["key"]
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))

if PEOPLE != "-":  # the ASF rosters are foundation-wide: the same files serve every Apache project
    for f in Path(PEOPLE).glob("*.json"):
        link(f, OUT / "people" / f.name)

if has_jira:
    for p in sorted((raw / "jira/search").glob("page_*.json")):
        link(p, OUT / "jira/issues" / p.name)
    for f in ("versions.json", "components.json"):
        if (raw / "jira" / f).exists():
            link(raw / "jira" / f, OUT / "jira" / f)
    statuses = Path(PEOPLE).parent / "jira/statuses.json" if PEOPLE != "-" else None  # _shared/jira
    if statuses and statuses.exists():  # fetched once for Flink's Jira, the ASF Jira's status list
        link(statuses, OUT / "jira/statuses.json")

for d in sorted((raw / "mail").iterdir()):
    for mb in d.glob("*.mbox") if d.is_dir() else ():
        link(mb, OUT / "mail" / d.name / mb.name)

# github: repo, one bundle per PR; issues: the REST issue pages as they are (the adapter leaves PRs out)
gh = raw / "github"
link(gh / "repo.json", OUT / "github/repo.json")
ic, rc = defaultdict(list), defaultdict(list)
for c in pages(gh / "issue_comments"):
    ic[int(c["issue_url"].rsplit("/", 1)[1])].append(c)
for c in pages(gh / "review_comments"):
    rc[int(c["pull_request_url"].rsplit("/", 1)[1])].append(c)
n = 0
for p in pages(gh / "pulls"):
    rv = gh / "reviews" / f"{p['number']}.json"
    bundle = {"pull": p, "reviews": json.load(open(rv)) if rv.exists() else [], "review_comments": rc[p["number"]],
              "issue_comments": ic[p["number"]], "commits": [], "files": [], "timeline": []}
    (OUT / "github/pulls").mkdir(parents=True, exist_ok=True)
    (OUT / "github/pulls" / f"pr-{p['number']}.json").write_text(json.dumps(bundle))
    n += 1
issues = 0
for p in sorted((gh / "issues").glob("page_*.json")):
    link(p, OUT / "issues" / p.name)
    issues += sum(1 for i in json.load(open(p)) if "pull_request" not in i)
# an issue's comments sit in issue_comments; give each issue its own list as the REST shape allows
if issues:
    for p in sorted((OUT / "issues").glob("page_*.json")):
        rows = json.load(open(p))
        for i in rows:
            if "pull_request" not in i:
                i["comments"] = ic.get(i["number"], [])
        p.unlink()
        p.write_text(json.dumps(rows))

# git: the same log export fetch_apache.py writes, from our clone, for the download's window
start, sep, head_end = "\x1e", "\x1f", "\x1d"
fmt = start + sep.join(["%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%D", "%B"]) + head_end
text = subprocess.run(["git", "-C", str(SRC / "codebase"), "log", gm.get("head", "HEAD"), f"--since={since} 00:00:00 +0000",
                       f"--until={fetched}", "--name-status", "-M", f"--format={fmt}"],
                      check=True, capture_output=True).stdout.decode("utf-8", "replace")
(OUT / "git").mkdir(exist_ok=True)
c = 0
with open(OUT / "git/commits.jsonl", "w", encoding="utf-8") as f:
    for record in text.split(start)[1:]:
        header, _, block = record.partition(head_end)
        x = header.split(sep)
        files = [{"status": q[0], "path": q[-1], **({"from": q[1]} if len(q) == 3 else {})}
                 for q in (ln.split("\t") for ln in block.strip("\n").split("\n")) if len(q) >= 2 and q[0][:1] in "AMDRCT"]
        f.write(json.dumps({"sha": x[0], "parents": x[1].split(), "author_name": x[2], "author_email": x[3],
                            "authored_at": x[4], "committer_name": x[5], "committer_email": x[6], "committed_at": x[7],
                            "refs": x[8], "message": x[9].rstrip("\n"), "files": files}, ensure_ascii=False) + "\n")
        c += 1

link(SRC / "codebase", OUT / "code")  # the checkout as it is; the code adapter skips .git
print(json.dumps({"dataset": name, "repo": repo["full_name"], "jira": manifest.get("jira"), "prs": n, "issues": issues,
                  "commits": c, "mail_lists": manifest["lists"]}))
