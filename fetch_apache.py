"""Download one Apache project across all five EKG pillars, for schema evaluation.

One project, one date window, the same people throughout:

  code           GitHub: pull requests with reviews, review comments, conversation
                 comments and commits; releases, tags, workflow runs; plus a blobless
                 git clone whose log (parents, files touched) is written as JSONL
  tickets        Apache Jira: issues with full changelog and comments, plus the
                 project's statuses, versions (releases) and components
  conversations  Apache mailing lists: one mbox per list per month
  documents      Apache Confluence: every page in the space (KIPs are decision
                 records); for pages edited in the window, version history,
                 recent bodies and comments
  people         whimsy.apache.org public rosters: people, LDAP groups, projects,
                 committees (PMC members with join dates and chairs)

Everything is written raw, exactly as each API returned it, so adapters can be
tested against real payloads. Re-running skips files that already exist, so an
interrupted run resumes where it stopped.

Stdlib only. Usage (from the project root):

    python ekg-eval-data/fetch_apache.py                         # Kafka, last 90 days
    python ekg-eval-data/fetch_apache.py --since 2026-07-01 --until 2026-09-30
    python ekg-eval-data/fetch_apache.py --only jira,mail,people # skip GitHub
    python ekg-eval-data/fetch_apache.py --project flink --repo apache/flink \
        --jira FLINK --wiki-space FLINK --lists dev,user

Set EKG_GITHUB_TOKEN (its own name, so it never reaches other tools that read GITHUB_TOKEN; any classic or fine-grained token, no scopes needed for public
repos). Without it GitHub allows 60 calls an hour, enough for the PR list only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Paste a GitHub token between the quotes to use it without setting anything else.
# Use a fine-grained token with "Public repositories (read-only)" access only, and
# remove it before sharing or committing this file.
GITHUB_TOKEN_HERE = ""

USER_AGENT = "kyro-ekg-eval-fetch/1.0 (schema evaluation; contact mamata@kantham.ai)"
JIRA = "https://issues.apache.org/jira/rest/api/2"
WIKI = "https://cwiki.apache.org/confluence/rest"
MAIL = "https://lists.apache.org/api/mbox.lua"
WHIMSY = "https://whimsy.apache.org/public"
GITHUB = "https://api.github.com"
WHIMSY_FILES = [
    "public_ldap_people.json",
    "public_ldap_groups.json",
    "public_ldap_projects.json",
    "public_ldap_services.json",
    "committee-info.json",
    "member-info.json",
]
STEPS = ("people", "jira", "mail", "wiki", "github", "git")


# ---------------------------------------------------------------- http

class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


def http_get(url: str, headers: dict | None = None, *, retries: int = 5, timeout: int = 120):
    """GET with retries on 429/5xx and on GitHub rate-limit exhaustion. Returns (bytes, headers)."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            remaining = e.headers.get("X-RateLimit-Remaining")
            if e.status in (403, 429) and remaining == "0":
                reset = int(e.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset - time.time(), 0) + 5
                log(f"  GitHub rate limit reached; sleeping {int(wait)}s until reset")
                time.sleep(wait)
                continue
            if e.status in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(e.headers.get("Retry-After", 0) or 2 ** attempt * 5)
                log(f"  HTTP {e.status}, retrying in {wait}s: {url}")
                time.sleep(wait)
                continue
            raise HttpError(e.status, url, body) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                wait = 2 ** attempt * 5
                log(f"  {type(e).__name__}: {e}; retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"gave up on {url}")


def get_json(url: str, headers: dict | None = None):
    body, hdrs = http_get(url, headers)
    return json.loads(body), hdrs


def q(params: dict) -> str:
    return urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


# ---------------------------------------------------------------- files

def log(msg: str) -> None:
    print(msg, flush=True)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_json(path: Path, obj) -> None:
    write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- people

def fetch_people(out: Path, args, stats: dict) -> None:
    d = out / "people"
    for name in WHIMSY_FILES:
        path = d / name
        if path.exists():
            continue
        try:
            body, _ = http_get(f"{WHIMSY}/{name}")
        except HttpError as e:
            log(f"  people: {name} unavailable (HTTP {e.status}), skipped")
            continue
        write_bytes(path, body)
        log(f"  people: {name} ({len(body) // 1024} KB)")
    stats["people_files"] = sorted(p.name for p in d.glob("*.json"))


# ---------------------------------------------------------------- jira

def fetch_jira(out: Path, args, stats: dict) -> None:
    d = out / "jira"
    key = args.jira
    for name, url in {
        "project.json": f"{JIRA}/project/{key}",
        "statuses.json": f"{JIRA}/project/{key}/statuses",
        "versions.json": f"{JIRA}/project/{key}/versions",
        "components.json": f"{JIRA}/project/{key}/components",
        "priorities.json": f"{JIRA}/priority",
        "resolutions.json": f"{JIRA}/resolution",
    }.items():
        if not (d / name).exists():
            try:
                write_json(d / name, get_json(url)[0])
            except HttpError as e:
                log(f"  jira: {name} unavailable (HTTP {e.status}), skipped")

    jql = (f'project = {key} AND updated >= "{args.since}" '
           f'AND created < "{args.until_exclusive}" ORDER BY created ASC')
    page_size, start, total = 100, 0, None
    while total is None or start < total:
        path = d / "issues" / f"page-{start // page_size:04d}.json"
        if path.exists():
            data = read_json(path)
        else:
            data, _ = get_json(f"{JIRA}/search?" + q({
                "jql": jql, "startAt": start, "maxResults": page_size,
                "fields": "*all", "expand": "changelog,renderedFields,names",
            }))
            write_json(path, data)
        total = data["total"]
        got = len(data.get("issues", []))
        log(f"  jira: issues {start + got}/{total}")
        if got == 0:
            break
        start += got
        if args.max_issues and start >= args.max_issues:
            log(f"  jira: stopped at --max-issues {args.max_issues}")
            break
    stats["jira_issues_in_window"] = total
    stats["jira_jql"] = jql


# ---------------------------------------------------------------- mail

def months(since: dt.date, until: dt.date):
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def fetch_mail(out: Path, args, stats: dict) -> None:
    domain = f"{args.project}.apache.org"
    counts = {}
    for lst in args.lists:
        address = f"{lst}@{domain}"
        n = 0
        for month in months(args.since_date, args.until_date):
            path = out / "mail" / address / f"{month}.mbox"
            if not path.exists():
                try:
                    body, _ = http_get(f"{MAIL}?" + q({"list": address, "date": month}))
                except HttpError as e:
                    log(f"  mail: {address} {month} unavailable (HTTP {e.status}), skipped")
                    continue
                write_bytes(path, body)
            msgs = path.read_bytes().count(b"\nFrom ") + (1 if path.stat().st_size else 0)
            n += msgs
            log(f"  mail: {address} {month}: ~{msgs} messages")
        counts[address] = n
    stats["mail_messages_approx"] = counts


# ---------------------------------------------------------------- wiki

def fetch_wiki(out: Path, args, stats: dict) -> None:
    d = out / "wiki"
    space = args.wiki_space
    if not (d / "space.json").exists():
        write_json(d / "space.json",
                   get_json(f"{WIKI}/api/space/{space}?expand=description.plain,homepage")[0])

    # Every page in the space with its current body: KIPs and design pages span years.
    page_size, start, n = 50, 0, 0
    expand = "body.storage,version,history,ancestors,metadata.labels,space"
    while True:
        path = d / "pages" / f"page-{start // page_size:04d}.json"
        if path.exists():
            data = read_json(path)
        else:
            data, _ = get_json(f"{WIKI}/api/content?" + q({
                "spaceKey": space, "type": "page", "status": "current",
                "start": start, "limit": page_size, "expand": expand,
            }))
            write_json(path, data)
        got = len(data.get("results", []))
        n += got
        log(f"  wiki: pages {n}")
        if got < page_size:
            break
        start += page_size

    # Pages edited in the window: version list, the last N bodies, comments.
    cql = (f'space = "{space}" and type = page and lastmodified >= "{args.since}" '
           f'and lastmodified < "{args.until_exclusive}"')
    recent, start = [], 0
    while True:
        data, _ = get_json(f"{WIKI}/api/content/search?" + q({"cql": cql, "start": start, "limit": 100}))
        recent += [r["id"] for r in data.get("results", [])]
        if data.get("size", 0) < 100:
            break
        start += 100
    for i, pid in enumerate(recent, 1):
        path = d / "history" / f"{pid}.json"
        if path.exists():
            continue
        versions, _ = get_json(f"{WIKI}/experimental/content/{pid}/version?limit=200")
        numbers = sorted((v["number"] for v in versions.get("results", [])), reverse=True)
        bodies = []
        for num in numbers[1:1 + args.wiki_versions]:  # the current body is already in pages/
            try:
                bodies.append(get_json(f"{WIKI}/api/content/{pid}?" + q({
                    "status": "historical", "version": num, "expand": "body.storage,version"}))[0])
            except HttpError as e:
                log(f"  wiki: {pid} v{num} unavailable (HTTP {e.status})")
        comments, _ = get_json(f"{WIKI}/api/content/{pid}/child/comment?" + q({
            "limit": 200, "depth": "all", "expand": "body.storage,version,ancestors"}))
        write_json(path, {"id": pid, "versions": versions, "version_bodies": bodies, "comments": comments})
        log(f"  wiki: history {i}/{len(recent)} (page {pid}, {len(numbers)} versions)")
    stats["wiki_pages"] = n
    stats["wiki_pages_edited_in_window"] = len(recent)


# ---------------------------------------------------------------- github

def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("EKG_GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def gh_pages(url: str, max_items: int | None = None):
    """Yield items across Link-paginated GitHub list endpoints."""
    got = 0
    while url:
        data, hdrs = get_json(url, gh_headers())
        items = data if isinstance(data, list) else data.get("workflow_runs", data.get("items", []))
        for item in items:
            yield item
            got += 1
            if max_items and got >= max_items:
                return
        url = None
        for part in (hdrs.get("Link") or "").split(","):
            if 'rel="next"' in part:
                url = part[part.index("<") + 1:part.index(">")]


def fetch_github(out: Path, args, stats: dict) -> None:
    d = out / "github"
    repo = args.repo
    base = f"{GITHUB}/repos/{repo}"
    have_token = bool(os.environ.get("EKG_GITHUB_TOKEN"))
    if not have_token:
        log("  github: EKG_GITHUB_TOKEN not set; fetching the PR list only (60 calls/hour)")

    for name, url in {"repo.json": base, "labels.json": f"{base}/labels?per_page=100",
                      "milestones.json": f"{base}/milestones?state=all&per_page=100",
                      "environments.json": f"{base}/environments"}.items():
        if not (d / name).exists():
            try:
                write_json(d / name, get_json(url, gh_headers())[0])
            except HttpError as e:
                log(f"  github: {name} unavailable (HTTP {e.status})")
    for rel in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        path = d / "codeowners" / rel.replace("/", "__")
        if path.exists():
            break
        try:
            write_bytes(path, http_get(f"https://raw.githubusercontent.com/{repo}/HEAD/{rel}")[0])
            break
        except HttpError:
            pass

    # PRs updated in the window, newest update first; stop once older than --since.
    list_path = d / "pulls_list.json"
    if list_path.exists():
        prs = read_json(list_path)
    else:
        prs = []
        for pr in gh_pages(f"{base}/pulls?state=all&sort=updated&direction=desc&per_page=100"):
            if pr["updated_at"][:10] < args.since:
                break
            if pr["created_at"][:10] < args.until_exclusive:
                prs.append(pr)
            if args.max_prs and len(prs) >= args.max_prs:
                break
        write_json(list_path, prs)
    log(f"  github: {len(prs)} PRs in window")

    if have_token:
        for i, pr in enumerate(prs, 1):
            n = pr["number"]
            path = d / "pulls" / f"pr-{n}.json"
            if path.exists():
                continue
            write_json(path, {
                "pull": get_json(f"{base}/pulls/{n}", gh_headers())[0],
                "reviews": list(gh_pages(f"{base}/pulls/{n}/reviews?per_page=100")),
                "review_comments": list(gh_pages(f"{base}/pulls/{n}/comments?per_page=100")),
                "issue_comments": list(gh_pages(f"{base}/issues/{n}/comments?per_page=100")),
                "commits": list(gh_pages(f"{base}/pulls/{n}/commits?per_page=100")),
                "files": list(gh_pages(f"{base}/pulls/{n}/files?per_page=100", 300)),
                "timeline": list(gh_pages(f"{base}/issues/{n}/timeline?per_page=100", 300)),
            })
            if i % 10 == 0 or i == len(prs):
                log(f"  github: PR details {i}/{len(prs)}")

        for name, url, cap in [
            ("releases.json", f"{base}/releases?per_page=100", 300),
            ("tags.json", f"{base}/tags?per_page=100", 500),
            ("workflows.json", f"{base}/actions/workflows?per_page=100", None),
        ]:
            if not (d / name).exists():
                if name == "workflows.json":
                    write_json(d / name, get_json(url, gh_headers())[0])
                else:
                    write_json(d / name, list(gh_pages(url, cap)))
        runs_path = d / "workflow_runs.json"
        if not runs_path.exists():
            created = f"{args.since}..{args.until}"
            write_json(runs_path, list(gh_pages(
                f"{base}/actions/runs?" + q({"created": created, "per_page": 100}), args.max_runs)))
    stats["github_prs_in_window"] = len(prs)
    stats["github_pr_details"] = len(list((d / "pulls").glob("pr-*.json")))


def fetch_git(out: Path, args, stats: dict) -> None:
    """Blobless clone (commits and trees, no file contents) and a JSONL commit log for the window."""
    repo_dir = out / "git" / "repo.git"
    if not repo_dir.exists():
        log("  git: blobless clone (commits and trees only)...")
        subprocess.run(["git", "clone", "--bare", "--filter=blob:none",
                        f"https://github.com/{args.repo}.git", str(repo_dir)], check=True)
    else:
        subprocess.run(["git", "--git-dir", str(repo_dir), "fetch", "--filter=blob:none",
                        "origin", "+refs/heads/*:refs/heads/*", "--tags"], check=True)

    # Each record: START header fields (SEP-separated) HEAD_END, then git's --name-status lines.
    start, sep, head_end = "\x1e", "\x1f", "\x1d"
    fmt = start + sep.join(["%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%D", "%B"]) + head_end
    # --name-status compares trees, which a blobless clone has, so no file contents are fetched.
    res = subprocess.run(
        # explicit UTC midnights: a bare date makes git use the current time of day
        ["git", "--git-dir", str(repo_dir), "log", "HEAD", f"--since={args.since} 00:00:00 +0000",
         f"--until={args.until_exclusive} 00:00:00 +0000", "--name-status", "-M", f"--format={fmt}"],
        check=True, capture_output=True)
    text = res.stdout.decode("utf-8", "replace")
    path = out / "git" / "commits.jsonl"
    n = 0
    with open(path.with_suffix(".part"), "w", encoding="utf-8") as f:
        for record in text.split(start)[1:]:
            header, _, files_block = record.partition(head_end)
            fields = header.split(sep)
            files = []
            for line in files_block.strip("\n").split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0][:1] in "AMDRCT":
                    files.append({"status": parts[0], "path": parts[-1],
                                  **({"from": parts[1]} if len(parts) == 3 else {})})
            f.write(json.dumps({
                "sha": fields[0], "parents": fields[1].split(), "author_name": fields[2],
                "author_email": fields[3], "authored_at": fields[4], "committer_name": fields[5],
                "committer_email": fields[6], "committed_at": fields[7], "refs": fields[8],
                "message": fields[9].rstrip("\n"), "files": files,
            }, ensure_ascii=False) + "\n")
            n += 1
    path.with_suffix(".part").replace(path)
    log(f"  git: {n} commits in window -> {path.name}")
    stats["git_commits_in_window"] = n


# ---------------------------------------------------------------- main

def main() -> int:
    today = dt.date.today()
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", default="kafka", help="Apache project id (mail domain <id>.apache.org)")
    p.add_argument("--repo", default=None, help="GitHub owner/name (default apache/<project>)")
    p.add_argument("--jira", default=None, help="Jira project key (default PROJECT upper-cased)")
    p.add_argument("--wiki-space", default=None, help="Confluence space key (default as --jira)")
    p.add_argument("--lists", default="dev,users", help="comma-separated list names, e.g. dev,users,commits")
    p.add_argument("--since", default=(today - dt.timedelta(days=90)).isoformat(), help="YYYY-MM-DD (inclusive)")
    p.add_argument("--until", default=today.isoformat(), help="YYYY-MM-DD (inclusive)")
    p.add_argument("--only", default=",".join(STEPS), help=f"comma-separated steps: {','.join(STEPS)}")
    p.add_argument("--max-prs", type=int, default=300, help="cap on PRs fetched in detail (0 = no cap)")
    p.add_argument("--max-issues", type=int, default=0, help="cap on Jira issues (0 = no cap)")
    p.add_argument("--max-runs", type=int, default=500, help="cap on workflow runs")
    p.add_argument("--wiki-versions", type=int, default=3, help="older bodies kept per page edited in the window")
    p.add_argument("--out", default=None, help="output folder (default <root>/ekg-eval-raw/apache-<project>)")
    p.add_argument("--token-file", default=str(Path.home() / ".ekg_github_token"),
                   help="file holding a GitHub token, read when EKG_GITHUB_TOKEN is not set "
                        "(default ~/.ekg_github_token)")
    args = p.parse_args()

    if os.environ.get("EKG_GITHUB_TOKEN"):
        log("GitHub token: from EKG_GITHUB_TOKEN")
    elif GITHUB_TOKEN_HERE.strip():
        os.environ["EKG_GITHUB_TOKEN"] = GITHUB_TOKEN_HERE.strip()  # this process only
        log("GitHub token: from GITHUB_TOKEN_HERE in fetch_apache.py")
    elif Path(args.token_file).is_file():
        token = Path(args.token_file).read_text(encoding="utf-8-sig").strip()
        if token:
            os.environ["EKG_GITHUB_TOKEN"] = token  # this process only
            log(f"GitHub token: from {args.token_file}")
    else:
        log(f"GitHub token: none (EKG_GITHUB_TOKEN unset, no {args.token_file})")

    args.repo = args.repo or f"apache/{args.project}"
    args.jira = args.jira or args.project.upper()
    args.wiki_space = args.wiki_space or args.jira
    args.lists = [s.strip() for s in args.lists.split(",") if s.strip()]
    args.since_date = dt.date.fromisoformat(args.since)
    args.until_date = dt.date.fromisoformat(args.until)
    args.until_exclusive = (args.until_date + dt.timedelta(days=1)).isoformat()
    steps = [s.strip() for s in args.only.split(",") if s.strip()]
    bad = set(steps) - set(STEPS)
    if bad:
        p.error(f"unknown step(s): {', '.join(sorted(bad))}")

    out = Path(args.out) if args.out else here.parent / "ekg-eval-raw" / f"apache-{args.project}"
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    manifest.update({
        "project": args.project, "repo": args.repo, "jira": args.jira, "wiki_space": args.wiki_space,
        "lists": args.lists, "window": {"since": args.since, "until": args.until},
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {"github": GITHUB, "jira": JIRA, "wiki": WIKI, "mail": MAIL, "people": WHIMSY},
    })
    stats = manifest.setdefault("stats", {})
    log(f"Apache {args.project}: {args.since} .. {args.until} -> {out}")

    runners = {"people": fetch_people, "jira": fetch_jira, "mail": fetch_mail,
               "wiki": fetch_wiki, "github": fetch_github, "git": fetch_git}
    failed = []
    for step in STEPS:
        if step not in steps:
            continue
        log(f"[{step}]")
        try:
            runners[step](out, args, stats)
        except Exception as e:  # keep going so one source's outage doesn't lose the others
            log(f"  {step} FAILED: {e}")
            failed.append(step)
        write_json(manifest_path, manifest)

    log("done" if not failed else f"done with failures: {', '.join(failed)} (re-run to resume)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
