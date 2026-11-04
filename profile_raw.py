"""Profile a fetch_apache.py download: what each source holds, and what an adapter must handle.

Counts by source (formats, states, truncation, bots and notifications, links between
tools, identity overlap between tools), as background for check_schema.py.

    python -I ekg-eval-data/profile_raw.py [data dir] [--json facts.json]
    # data dir defaults to ekg-eval-raw/apache-kafka; --json also writes the key figures
"""
import argparse, collections, email.utils, json, mailbox, re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("data", nargs="?", default=str(Path(__file__).resolve().parent.parent / "ekg-eval-raw" / "apache-kafka"))
ap.add_argument("--json", default=None, help="also write the key figures as JSON to this path")
args = ap.parse_args()

D = Path(args.data)
C = collections.Counter
def J(p): return json.loads(Path(p).read_text(encoding="utf-8"))
def sec(t): print(f"\n=== {t}")

manifest = J(D / "manifest.json")
PROJ, REPO, SINCE = manifest["project"], manifest["repo"], manifest["window"]["since"]
KEY = re.compile(rf"\b{re.escape(manifest['jira'])}-\d+\b")
KIP = re.compile(r"\bKIP-\d+\b")
PRNUM = re.compile(r"\(#(\d+)\)")
F = {}  # the figures --json writes

# ---------------- people
sec("people")
people = J(D / "people/public_ldap_people.json")
print("people top keys", list(people)[:5])
pmap = people.get("people", {})
print("persons", len(pmap), "sample fields", C(k for v in list(pmap.values())[:2000] for k in v).most_common(8))
ci = J(D / "people/committee-info.json")
k = ci.get("committees", {}).get(PROJ, {})
print(f"committee-info {PROJ} keys", list(k)[:15])
roster = k.get("roster", {})
print("PMC roster", len(roster), "sample", list(roster.items())[:1], "chair", k.get("chair") or k.get("chairs"))
proj = J(D / "people/public_ldap_projects.json").get("projects", {}).get(PROJ, {})
print(f"ldap project {PROJ} keys", list(proj)[:10], "members", len(proj.get("members", [])), "owners", len(proj.get("owners", [])))
asf_ids = set(pmap)
F.update(pmc=len(roster), committers=len(proj.get("members", [])))

# ---------------- jira
sec("jira")
issues = [i for p in sorted((D / "jira/issues").glob("*.json")) for i in J(p)["issues"]]
print("issues", len(issues))
f0 = issues[0]["fields"]
print("created format", f0["created"], "| resolutiondate", f0.get("resolutiondate"))
print("issuetype", C(i["fields"]["issuetype"]["name"] for i in issues).most_common())
print("status", C((i["fields"]["status"]["name"], i["fields"]["status"]["statusCategory"]["key"]) for i in issues).most_common())
print("resolution", C((i["fields"].get("resolution") or {}).get("name") for i in issues).most_common(6))
print("priority", C((i["fields"].get("priority") or {}).get("name") for i in issues).most_common())
trunc_cl = sum(1 for i in issues if i["changelog"]["total"] > len(i["changelog"]["histories"]))
trunc_cm = sum(1 for i in issues if i["fields"]["comment"]["total"] > len(i["fields"]["comment"]["comments"]))
print("changelog truncated", trunc_cl, "| comments truncated", trunc_cm)
items = C(it["field"] for i in issues for h in i["changelog"]["histories"] for it in h["items"])
print("changelog fields", items.most_common(12))
created_in_window = sum(1 for i in issues if i["fields"]["created"][:10] >= SINCE)
print("created in window", created_in_window, "| older but updated", len(issues) - created_in_window)
users = {}
for i in issues:
    f = i["fields"]
    for u in [f.get("reporter"), f.get("assignee"), f.get("creator")] + [c["author"] for c in f["comment"]["comments"]] + [h.get("author") for h in i["changelog"]["histories"]]:
        if u: users[u.get("name") or u.get("key")] = u
jira_with_email = sum(1 for u in users.values() if u.get("emailAddress"))
jira_is_asf = sum(1 for n in users if n in asf_ids)
print("jira users", len(users), "| user fields", sorted(next(iter(users.values())).keys()))
print("jira user has emailAddress", jira_with_email, "| name is ASF id", jira_is_asf)
bots = C(c["author"].get("displayName") for i in issues for c in i["fields"]["comment"]["comments"])
print("top comment authors", bots.most_common(5))
gh_comment = sum(1 for i in issues for c in i["fields"]["comment"]["comments"] if f"github.com/{REPO}/pull/" in (c.get("body") or ""))
print("comments containing a GitHub PR url", gh_comment)
link_names = C(l["type"]["name"] for i in issues for l in i["fields"]["issuelinks"])
print("issue links", link_names.most_common())
print("fixVersions used", sum(1 for i in issues if i["fields"].get("fixVersions")), "| components used", sum(1 for i in issues if i["fields"].get("components")), "| labels used", sum(1 for i in issues if i["fields"].get("labels")))
print("security level set", sum(1 for i in issues if i["fields"].get("security")))
print("description markup sample", repr((f0.get("description") or "")[:120]))
jira_keys = {i["key"] for i in issues}
F.update(jira_issues=len(issues), jira_truncated=trunc_cl + trunc_cm, jira_status_changes=items["status"],
         jira_link_names=len(link_names), jira_users=len(users), jira_users_with_email=jira_with_email,
         jira_user_is_asf_id=jira_is_asf, jira_created_sample=f0["created"])

# ---------------- github
sec("github")
prs = [J(p) for p in sorted((D / "github/pulls").glob("pr-*.json"))]
print("PR detail files", len(prs))
p0 = prs[0]["pull"]
print("created_at format", p0["created_at"], "| keys of a PR detail", sorted(prs[0]))
st = C(("merged" if x["pull"]["merged_at"] else x["pull"]["state"]) + ("/draft" if x["pull"]["draft"] else "") for x in prs)
print("states", st.most_common())
print("merged_by present on merged", sum(1 for x in prs if x["pull"]["merged_at"] and x["pull"].get("merged_by")), "/", sum(1 for x in prs if x["pull"]["merged_at"]))
print("review states", C(r["state"] for x in prs for r in x["reviews"]).most_common())
print("PRs with >=1 approval", sum(1 for x in prs if any(r["state"] == "APPROVED" for r in x["reviews"])))
print("review comments", sum(len(x["review_comments"]) for x in prs), "| with in_reply_to", sum(1 for x in prs for c in x["review_comments"] if c.get("in_reply_to_id")))
print("issue comments", sum(len(x["issue_comments"]) for x in prs))
print("files capped at 300", sum(1 for x in prs if len(x["files"]) >= 300), "| files missing patch", sum(1 for x in prs for f in x["files"] if "patch" not in f))
print("timeline events", C(e.get("event") for x in prs for e in x["timeline"]).most_common(14))
titled = [x for x in prs if KEY.search(x["pull"]["title"])]
print(f"PR title names a {manifest['jira']} key", len(titled), "/", len(prs), "| MINOR:", sum(1 for x in prs if x["pull"]["title"].upper().startswith("MINOR")))
keys_named = {k for x in titled for k in KEY.findall(x["pull"]["title"])}
print("named keys found in jira download", len(keys_named & jira_keys), "/", len(keys_named))
logins = C(x["pull"]["user"]["login"] for x in prs)
gh_is_asf = sum(1 for l in logins if l in asf_ids)
print("distinct PR authors", len(logins), "| login equals an ASF id", gh_is_asf)
print("author_association", C(x["pull"]["author_association"] for x in prs).most_common())
bots_gh = C(c["user"]["login"] for x in prs for c in x["issue_comments"] if c["user"]["type"] == "Bot")
print("bot comments", bots_gh.most_common(4))
runs = J(D / "github/workflow_runs.json") if (D / "github/workflow_runs.json").exists() else []
print("workflow runs", len(runs), "| events", C(r["event"] for r in runs).most_common(4), "| conclusions", C(r["conclusion"] for r in runs).most_common(5))
rel = J(D / "github/releases.json") if (D / "github/releases.json").exists() else []
tags = J(D / "github/tags.json") if (D / "github/tags.json").exists() else []
print("releases", len(rel), "| tags", len(tags), "| environments", (J(D / "github/environments.json") or {}).get("total_count") if (D / "github/environments.json").exists() else None)
codeowners = [p.name for p in (D / "github/codeowners").glob("*")] if (D / "github/codeowners").exists() else []
print("codeowners files", codeowners or "none")
F.update(prs=len(prs), pr_titles_with_key=len(titled), pr_authors=len(logins), gh_login_is_asf_id=gh_is_asf,
         ci_runs=len(runs), releases=len(rel), tags=len(tags), codeowners=bool(codeowners))

# ---------------- git
sec("git")
commits = [json.loads(l) for l in open(D / "git/commits.jsonl", encoding="utf-8")]
non_utc = sum(1 for c in commits if not c["committed_at"].endswith("+00:00"))
print("commits", len(commits), "| committed_at sample", commits[0]["committed_at"])
print("non-UTC offsets", non_utc)
print("merge commits", sum(1 for c in commits if len(c["parents"]) > 1))
with_pr = [c for c in commits if PRNUM.search(c["message"].split("\n")[0])]
print("subject names a PR (#n)", len(with_pr), f"| names a {manifest['jira']} key", sum(1 for c in commits if KEY.search(c["message"].split("\n")[0])))
merge_shas = {x["pull"]["merge_commit_sha"] for x in prs if x["pull"]["merged_at"]}
print("PR merge_commit_sha found in commits.jsonl", len(merge_shas & {c["sha"] for c in commits}), "/", len(merge_shas))
emails = C(c["author_email"].lower() for c in commits)
apache_emails = sum(1 for e in emails if e.endswith("@apache.org"))
noreply_emails = sum(1 for e in emails if "noreply.github.com" in e)
print("distinct author emails", len(emails), "| @apache.org", apache_emails, "| github noreply", noreply_emails)
print("committer emails", C(c["committer_email"] for c in commits).most_common(3))
trailers = C(m for c in commits for m in re.findall(r"^([A-Za-z-]+-by):", c["message"], re.M))
print("trailers", trailers.most_common(6))
F.update(commits=len(commits), commits_non_utc=non_utc, commits_naming_pr=len(with_pr), author_emails=len(emails),
         author_emails_apache=apache_emails, author_emails_noreply=noreply_emails,
         co_authored=trailers["Co-authored-by"])

# ---------------- mail
sec("mail")
msgs, from_addrs = [], C()
for mb in sorted((D / "mail").glob("*/*.mbox")):
    for m in mailbox.mbox(str(mb)):
        msgs.append((mb.parent.name, m))
print("messages", len(msgs))
ids = {m["Message-ID"] for _, m in msgs}
print("missing Message-ID", sum(1 for _, m in msgs if not m["Message-ID"]), "| duplicate ids", len(msgs) - len(ids))
replies = [m for _, m in msgs if m["In-Reply-To"]]
print("replies", len(replies), "| parent in download", sum(1 for m in replies if m["In-Reply-To"].strip() in ids))
print("date samples", [m["Date"] for _, m in msgs[:2]])
subj = C()
for _, m in msgs:
    s = str(m["Subject"] or "")
    for tag in ("[DISCUSS]", "[VOTE]", "[RESULT]", "[ANNOUNCE]", "[jira]", "[PR]", "Re:"):
        if tag.lower() in s.lower(): subj[tag] += 1
print("subject tags", subj.most_common())
kip_mail = sum(1 for _, m in msgs if KIP.search(str(m["Subject"] or "")))
print("mentions KIP-n", kip_mail, f"| {manifest['jira']}-n in subject", sum(1 for _, m in msgs if KEY.search(str(m["Subject"] or ""))))
for _, m in msgs:
    name, addr = email.utils.parseaddr(str(m["From"] or ""))
    from_addrs[addr.lower()] += 1
sender_is_git = sum(1 for a in from_addrs if a in emails)
print("distinct senders", len(from_addrs), "| top", from_addrs.most_common(4))
print("sender == a git author email", sender_is_git)
print("multipart", sum(1 for _, m in msgs if m.is_multipart()), "| content types", C(m.get_content_type() for _, m in msgs).most_common(4))
print("to headers", C(str(m["To"])[:40] for _, m in msgs).most_common(3))
print("rewritten 'via' senders", sum(1 for _, m in msgs if " via " in str(m["From"] or "")))
F.update(emails=len(msgs), emails_unique=len(ids), emails_naming_kip=kip_mail, senders=len(from_addrs),
         senders_matching_git=sender_is_git)

# ---------------- wiki
sec("wiki")
pages = [r for p in sorted((D / "wiki/pages").glob("*.json")) for r in J(p)["results"]]
print("pages", len(pages), "| version.when sample", pages[0]["version"]["when"], "| history.createdDate", pages[0]["history"].get("createdDate"))
kips = [p for p in pages if re.match(r"KIP-\d+", p["title"])]
print("KIP pages", len(kips))
state = C()
for p in kips:
    body = p["body"]["storage"]["value"]
    m = re.search(r"Current [Ss]tate\s*(?:</[^>]+>\s*)*:?\s*(?:<[^>]+>\s*)*([A-Za-z][A-Za-z \-]{2,30})", body)
    state[(m.group(1).strip().split()[0] if m else "unparsed")] += 1
print("KIP 'Current state' values (first word)", state.most_common(10))
print(f"KIP body has a {manifest['jira']} ref", sum(1 for p in kips if KEY.search(p["body"]["storage"]["value"])), "| has a lists.apache.org thread link", sum(1 for p in kips if "lists.apache.org/thread" in p["body"]["storage"]["value"]))
macros = C(m for p in pages for m in re.findall(r'ac:name="([a-z\-]+)"', p["body"]["storage"]["value"]))
print("confluence macros", macros.most_common(8))
print("labels used", sum(1 for p in pages if p["metadata"]["labels"]["results"]))
depth = C(len(p["ancestors"]) for p in pages)
print("ancestor depth", sorted(depth.items()))
editors = {p["version"]["by"].get("username") for p in pages}
print("distinct last editors", len(editors), "| username is ASF id", sum(1 for e in editors if e in asf_ids))
hist = [J(p) for p in (D / "wiki/history").glob("*.json")]
versions_listed = sum(len(h["versions"]["results"]) for h in hist)
print("history files", len(hist), "| versions listed", versions_listed, "| old bodies", sum(len(h["version_bodies"]) for h in hist), "| comments", sum(h["comments"].get("size", 0) for h in hist))
print("version lists capped at 200", sum(1 for h in hist if len(h["versions"]["results"]) >= 200))
F.update(wiki_pages=len(pages), kip_pages=len(kips), wiki_versions_listed=versions_listed, wiki_pages_edited=len(hist))

# ---------------- cross-source identity
sec("identity across sources")
jira_names = set(users)
pmc_in_jira, pmc_on_github = len(set(roster) & jira_names), len(set(roster) & set(logins))
print("jira usernames that are also GitHub PR logins", len(jira_names & set(logins)))
print("wiki editors that are also jira usernames", len(editors & jira_names))
print("PMC roster ids that appear as jira users", pmc_in_jira, "/", len(roster))
print("PMC roster ids that appear as GitHub logins", pmc_on_github)
F.update(pmc_in_jira=pmc_in_jira, pmc_on_github=pmc_on_github)

if args.json:
    Path(args.json).write_text(json.dumps(F, indent=1), encoding="utf-8")
    print(f"\nfigures written to {args.json}")
