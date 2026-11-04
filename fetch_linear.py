"""Download a Linear workspace through its GraphQL API, in the layout the linear adapter reads.

Writes <out>/linear/: organization.json, users.json, teams.json (with states, labels,
members, cycles), projects.json (with status, updates, milestones), documents.json and
issues/page-NNNN.json (issues updated in the window, with history, comments,
attachments, relations, labels, subscribers). Also writes or updates <out>/manifest.json.

Every field queried here was checked against Linear's published schema
(github.com/linear/linear, packages/sdk/src/schema.graphql). Nested lists come 50 at a
time to stay under Linear's query-complexity limit; an issue with more history or
comments than that is completed with follow-up queries. Re-running skips pages already
written.

The API key (Linear: Settings -> Security & access -> Personal API keys; read access is
enough) is read from EKG_LINEAR_API_KEY, else LINEAR_KEY_HERE below, else
~/.ekg_linear_token.

    python -I ekg-eval-data/fetch_linear.py --tenant acme --since 2026-07-01
    python -I ekg-eval-data/fetch_linear.py --tenant acme --since 2026-07-01 --out ekg-eval-raw/acme
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LINEAR_KEY_HERE = ""  # paste a Linear API key here to use it without setting anything else; remove before sharing
API = "https://api.linear.app/graphql"
HERE = Path(__file__).resolve().parent

PAGE = "pageInfo { hasNextPage endCursor }"
USER = "id name displayName email active admin guest createdAt"
HISTORY = ("id createdAt actorId fromStateId toStateId fromAssigneeId toAssigneeId fromPriority toPriority "
           "fromTitle toTitle fromEstimate toEstimate fromDueDate toDueDate fromProjectId toProjectId "
           "fromCycleId toCycleId fromParentId toParentId addedLabelIds removedLabelIds updatedDescription "
           "archived autoClosed autoArchived")
COMMENT = "id body createdAt updatedAt editedAt resolvedAt user { id } parent { id } resolvingUser { id }"
ISSUE = f"""
  id identifier number title description priority priorityLabel estimate createdAt updatedAt startedAt
  completedAt canceledAt dueDate archivedAt trashed branchName url
  state {{ id }} team {{ id }} assignee {{ id }} creator {{ id }} project {{ id }} cycle {{ id }}
  projectMilestone {{ id }} parent {{ id identifier }}
  labels(first: 50) {{ nodes {{ id }} }}
  subscribers(first: 50) {{ nodes {{ id }} }}
  history(first: 50) {{ nodes {{ {HISTORY} }} {PAGE} }}
  comments(first: 50) {{ nodes {{ {COMMENT} }} {PAGE} }}
  attachments(first: 50) {{ nodes {{ id title url sourceType createdAt }} }}
  relations(first: 50) {{ nodes {{ id type createdAt relatedIssue {{ id identifier }} }} }}
"""
TEAM = f"""
  id key name private cyclesEnabled cycleDuration triageEnabled createdAt
  states(first: 100) {{ nodes {{ id name type position }} }}
  labels(first: 250) {{ nodes {{ id name isGroup parent {{ id }} }} }}
  members(first: 250) {{ nodes {{ id }} }}
  cycles(first: 100) {{ nodes {{ id number name startsAt endsAt completedAt }} }}
"""
PROJECT = f"""
  id name description state health startDate targetDate createdAt completedAt canceledAt
  status {{ name type }} lead {{ id }}
  teams(first: 50) {{ nodes {{ id }} }}
  projectUpdates(first: 50) {{ nodes {{ id body health createdAt user {{ id }} }} }}
  projectMilestones(first: 50) {{ nodes {{ id name targetDate createdAt }} }}
"""
DOCUMENT = "id title content createdAt updatedAt creator { id } project { id }"


def api_key():
    key = os.environ.get("EKG_LINEAR_API_KEY") or LINEAR_KEY_HERE.strip()
    path = Path.home() / ".ekg_linear_token"
    if not key and path.is_file():
        key = path.read_text(encoding="utf-8-sig").strip()
    if not key:
        sys.exit("No Linear API key: set EKG_LINEAR_API_KEY, fill LINEAR_KEY_HERE, or write ~/.ekg_linear_token")
    return key


def gql(key, query, variables=None, retries=5):
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    for attempt in range(retries + 1):
        req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json", "Authorization": key,
                                                               "User-Agent": "kyro-ekg-eval-fetch/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) or "RATELIMITED" in text:
                wait = 2 ** attempt * 10
                print(f"  rate limited or server error ({e.code}); waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise SystemExit(f"Linear API error {e.code}: {text[:300]}")
        errors = data.get("errors")
        if errors and any((err.get("extensions") or {}).get("code") == "RATELIMITED" for err in errors) and attempt < retries:
            time.sleep(2 ** attempt * 10)
            continue
        if errors:
            raise SystemExit(f"Linear GraphQL error: {json.dumps(errors)[:500]}")
        return data["data"]
    raise SystemExit("gave up after repeated rate limits")


def all_pages(key, root, fields, page_size=50):
    """Every node of a top-level connection (users, teams, projects, documents)."""
    out, after = [], None
    while True:
        q = f"query($after: String) {{ {root}(first: {page_size}, after: $after, includeArchived: true) {{ nodes {{ {fields} }} {PAGE} }} }}"
        data = gql(key, q, {"after": after})[root]
        out += data["nodes"]
        if not data["pageInfo"]["hasNextPage"]:
            return out
        after = data["pageInfo"]["endCursor"]


def complete(key, issue, conn, fields):
    """Fetch the rest of a nested connection the first page cut off."""
    page = issue[conn]
    while page.get("pageInfo", {}).get("hasNextPage"):
        q = f"query($id: String!, $after: String) {{ issue(id: $id) {{ {conn}(first: 100, after: $after) {{ nodes {{ {fields} }} {PAGE} }} }} }}"
        nxt = gql(key, q, {"id": issue["id"], "after": page["pageInfo"]["endCursor"]})["issue"][conn]
        page["nodes"] += nxt["nodes"]
        page["pageInfo"] = nxt["pageInfo"]


def write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", required=True, help="a short name for the company (used in record keys)")
    ap.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=90)).isoformat(), help="issues updated on or after (YYYY-MM-DD)")
    ap.add_argument("--out", default=None, help="output folder (default ekg-eval-raw/<tenant>)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = Path(args.out or HERE.parent / "ekg-eval-raw" / args.tenant)
    d = out / "linear"
    key = api_key()

    print("organization, users, teams, projects, documents ...", flush=True)
    write(d / "organization.json", gql(key, "{ organization { id name urlKey } }")["organization"])
    write(d / "users.json", all_pages(key, "users", USER, 100))
    write(d / "teams.json", all_pages(key, "teams", TEAM, 5))  # 250 members + 250 labels each: keep pages small
    write(d / "projects.json", all_pages(key, "projects", PROJECT, 20))
    write(d / "documents.json", all_pages(key, "documents", DOCUMENT, 50))

    print(f"issues updated since {args.since} ...", flush=True)
    after, n, page_no = None, 0, 0
    filt = ", filter: { updatedAt: { gte: $since } }, orderBy: updatedAt"
    while True:
        path = d / "issues" / f"page-{page_no:04d}.json"
        q = f"query($after: String, $since: DateTimeOrDuration) {{ issues(first: 25, after: $after, includeArchived: true{filt}) {{ nodes {{ {ISSUE} }} {PAGE} }} }}"
        data = gql(key, q, {"after": after, "since": f"{args.since}T00:00:00.000Z"})["issues"]
        if not path.exists():
            for issue in data["nodes"]:
                complete(key, issue, "history", HISTORY)
                complete(key, issue, "comments", COMMENT)
            write(path, data["nodes"])
        n += len(data["nodes"])
        print(f"  {n} issues", flush=True)
        if not data["pageInfo"]["hasNextPage"]:
            break
        after, page_no = data["pageInfo"]["endCursor"], page_no + 1

    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.update({"tenant": args.tenant, "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "window": manifest.get("window") or {"since": args.since, "until": dt.date.today().isoformat()}})
    manifest.setdefault("sources", {})["linear"] = {"api": API, "issues": n}
    write(manifest_path, manifest)
    print(f"done: {n} issues -> {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
