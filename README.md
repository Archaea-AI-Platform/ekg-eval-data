# EKG evaluation data: one Apache project, five pillars

`fetch_apache.py` downloads one Apache project (Kafka by default) from every tool that project uses, for one date window, raw as each API returns it. The same people and records show up across tools: PR titles name `KAFKA-1234`, Jira issues link to PRs, design proposals (KIPs) live on the wiki and are debated in `[DISCUSS] KIP-…` email threads, and the rosters list who sits on which committee and since when. That gives real, checkable cross-pillar links to test the adapters and the graph against.

Stdlib Python only. No account is needed, except a GitHub token for PR details.

```powershell
$env:EKG_GITHUB_TOKEN = "<a GitHub token; public read needs no scopes>"
python ekg-eval-data/fetch_apache.py                                   # Kafka, last 90 days
python ekg-eval-data/fetch_apache.py --since 2026-07-01 --until 2026-09-30
python ekg-eval-data/fetch_apache.py --only people,jira,mail,wiki      # no GitHub
python ekg-eval-data/fetch_apache.py --project flink --lists dev,user  # another project
```

The GitHub token is read from `EKG_GITHUB_TOKEN`, then from `GITHUB_TOKEN_HERE` near the top of `fetch_apache.py`, then from `~/.ekg_github_token`. The first line of output says which one it used. Use a fine-grained token with public read-only access, and clear `GITHUB_TOKEN_HERE` before sharing the script.

Output goes to `ekg-eval-raw/apache-<project>/` at the repo root. Re-running skips files that already exist, so an interrupted or rate-limited run resumes. If one source fails, the other steps still run and the exit code is 1.

## What lands where

| Folder | Source | Feeds |
|---|---|---|
| `people/` | whimsy.apache.org public rosters: `public_ldap_people.json` (id → name), `public_ldap_groups.json`, `public_ldap_projects.json` (committers and PMC per project), `committee-info.json` (PMC rosters with join dates, chairs), `member-info.json` | Person, Account, Group, MEMBER_OF (dated), Position (PMC chair), HOLDS_POSITION |
| `jira/issues/page-NNNN.json` | Jira search, `fields=*all`, `expand=changelog,renderedFields` | Ticket, IN_STATE from changelog status transitions, Activity and FieldChange, Comment, ASSIGNED_TO, BLOCKS / DUPLICATE_OF / RELATED_TO, LINKS_TO (remote links to GitHub PRs) |
| `jira/{statuses,versions,components,…}.json` | project metadata | TicketState (with status category → `type`), Release, Label `kind: component` |
| `mail/<list>/<YYYY-MM>.mbox` | lists.apache.org monthly mbox | EmailMessage, EmailThread (`In-Reply-To` / `References`), FROM, SENT_TO, MailingList, MENTIONS of `KAFKA-…` / `KIP-…` |
| `wiki/pages/page-NNNN.json` | every page in the Confluence space, current body (storage format), version, ancestors, labels | Document `kind: wiki_page`, Folder `kind: space`, Label, Decision (KIP pages with Status / Discussion thread / JIRA fields) |
| `wiki/history/<pageId>.json` | pages edited in the window: full version list, last N older bodies, comments | ContentVersion, EDITED_BY, Comment `kind: doc` |
| `github/pulls_list.json`, `github/pulls/pr-<n>.json` | PR, reviews, review comments, conversation comments, commits, files, timeline | PullRequest, Review, Thread `kind: review`, Comment, CONTAINS, CHANGES, REVIEW_REQUESTED, MERGED_BY, IMPLEMENTS (title → Jira key) |
| `github/{releases,tags,workflows,workflow_runs,labels,milestones,environments,repo}.json` | repo metadata, Actions | Release, Tag, Pipeline, PipelineRun, Label, Milestone, Repository |
| `git/commits.jsonl` | blobless clone (`git/repo.git`), `git log --name-status` for the window | Commit, PARENT, CHANGES, AUTHORED_BY (`role: author / committer`) |
| `manifest.json` | window, sources, counts per step | |

Smoke run (2026-10-20 .. 2026-10-28): 235 Jira issues, 561 emails across `dev@` and `users@`, 1,671 wiki pages (16 edited in the window), 70 commits. It used about 130 MB, most of it the git clone and the wiki bodies.

## Turning it into the EKG graph: the adapters

```powershell
python -I ekg-eval-data/profile_raw.py     # what each source holds and what an adapter must handle
python -I ekg-eval-data/build_graph.py     # adapters + linker + identity -> ekg-eval-out/apache-kafka/
python -I ekg-eval-data/build_graph.py --raw-check --json report.json
python -I ekg-eval-data/check_schema.py    # same as build_graph.py --raw-check --no-write
python -I ekg-eval-data/summary_pdf.py     # all of the above, written up as ~/Downloads/ekg-apache-data-summary.pdf
```

`build_graph.py` runs the package in `ekg_adapters/` and writes `records.jsonl` and `links.jsonl` (the spec's two shapes). It validates every item with query-master's own `validate_record` / `validate_link`, reading the registry built from the spec. Exit code 1 means something failed validation.

| Module | What it does |
|---|---|
| `common.py` | The record and link envelope (`_key`, ids, claim ids, cores and predicates from the registry), merge by key and claim id, UTC times, Jira markup and Confluence XHTML to text, validation, writing |
| `access.py` | One AccessScope per place (directory, repo, Jira project, each list, wiki space), granted to the Public principal |
| `people.py` | Rosters to Person, Account, Group, Position and the person's apache.org address; verified HAS_ACCOUNT |
| `jira.py` | Tickets, states, IN_STATE and ASSIGNED_TO history and description versions from the changelog, one Activity + FieldChanges per change, versions, labels, comments, ticket links |
| `github.py` | Repo, PRs, reviews, review threads, comments, branches, dated labels and review requests, changed files, derived APPROVED_BY (marked stale after new commits), CI, tags |
| `git.py` | Commits, parents, authors and co-authors as EmailAddress, files changed; a noreply address links to its GitHub account |
| `mail.py` | Lists, messages, threads, senders; Jira notifications ANNOUNCE their ticket, human mail DISCUSSES the keys it names, KIP threads queue MADE_IN |
| `wiki.py` | Space, pages, version history, a Piece per section, labels, comments; each KIP page becomes a Decision |
| `linear.py` | Linear workspace, users, teams (states, labels, cycles), projects (status updates, milestones), issues with IN_STATE / ASSIGNED_TO / HAS_LABEL history, comments, attachments, relations, documents. Private teams are scoped to their members |
| `slack.py` | Slack workspace, users, channels, private channels, group DMs and DMs, messages, threads, files, reactions, pins, topic and rename history. Public channels are granted to full members; private ones to their members, dated from join and leave events |
| `linker.py` | Resolves the references every adapter queued (keys, PR numbers, shas, KIPs) across sources; tool-made references outside the window get stub records |
| `identity.py` | Attaches accounts and addresses to Persons, strongest evidence first: the directory, then verified emails (Slack, Linear) and GitHub SAML SSO identities, which are verified; the rest are inferred and grant nothing. With no directory, a Person is made per verified email |
| `pipeline.py` | Runs them in order, measures coverage, builds the report |

On Kafka, 2026-07-31 .. 2026-10-29, the raw JSON as-is passes 0 of 3,659 checks. The adapters produce 76,488 records and 145,170 links across 32 record types and 46 link types. All of them pass, and 9,519 of the links join two pillars. Coverage, identity results and the full list of schema issues are in the PDF.

"Valid" means the right shape: types, fields, enumerations, UTC times, endpoints, the envelope. It does not check the rules about timelines (no overlapping states, R6) or whether values are right.

### Schema issues found

* **`formality` is blocked on sightings.** The spec stores a per-source `formality` on MADE_IN, EXTRACTED_FROM, RESTATED_IN, DOCUMENTED_IN and MIRRORS. Its Foundation section and the registry treat `formality` as computed everywhere (R66). The adapters leave it out.
* **Decision has no status for a rejected proposal.** Discarded and rejected KIPs (62 here) have no value to use.
* **Some enumerated states aren't enforced.** `PullRequest.state` (R70), `Review.state` and `TicketState.type` accept any string.
* **There's no body-text field on Ticket or PullRequest.** Descriptions go into ContentVersion. The adapter guide should say so.
* **A wiki page's sections can't be DocSections.** DocSection only attaches to a File or another DocSection. The adapter uses Pieces instead.
* **The link discriminators aren't registered.** `role`, `kind` and `action`, which the spec uses on 11 link types, give about 55,000 "unknown field" warnings.
* **Tickets can't be AUTHORED_BY their reporter**, although the Tickets adapter contract says so. The adapter uses CREATED_BY role reporter.
* **An email sender known only by name has nowhere to go.** FROM only accepts an EmailAddress, so 170 list-rewritten senders get no author link.

## Linear and Slack

The pipeline runs every adapter whose folder is present in the data directory: `people/`, `jira/`, `linear/`, `github/`, `git/`, `mail/`, `slack/`, `wiki/`. So one company's Linear, Slack and GitHub data go in one folder with a `manifest.json`:

```json
{"tenant": "acme", "repo": "acme/payments", "slack_workspace": "Acme",
 "fetched_at": "2026-10-05T00:00:00Z", "window": {"since": "2026-10-01", "until": "2026-10-04"}}
```

**Linear.** The UI only exports CSV, which loses history, so use the API:

```powershell
$env:EKG_LINEAR_API_KEY = "lin_api_..."     # Settings -> Security & access -> Personal API keys
python -I ekg-eval-data/fetch_linear.py --tenant acme --since 2026-07-01   # -> ekg-eval-raw/acme/linear/
```

Every field it queries was checked against Linear's published GraphQL schema, and a test against a mock API checks its paging and output layout. It hasn't been run against the live API yet.

**Slack.** Use the workspace export (Settings -> Import/Export -> Export), unzipped into `slack/`: `users.json`, `channels.json`, `groups.json`, `mpims.json`, `dms.json`, and one folder per conversation with a file per day. A standard export covers public channels only. Private channels and DMs need a Business+ or Enterprise Grid export, or Web API results saved in the same layout (message objects have the same shape).

**GitHub, for private repos.** Two optional files make access work. Without them a private repo is visible to nobody (fail closed):
* `github/collaborators.json`: `GET /repos/{owner}/{repo}/collaborators`. Each collaborator gets a dated grant at their role's level.
* `github/external_identities.json`: the org's SAML SSO identities (`[{id, login, email}]`, from GraphQL `organization.samlIdentityProvider.externalIdentities`). This is the only thing that verifies who a GitHub account belongs to. Without it GitHub accounts are inferred, and inferred links grant nothing.

**Cross-tool links these adapters make.** Each one is checked by the tests:

| Link | How it is found |
|---|---|
| Ticket CREATED_FROM / LINKS_TO Message, DISCUSSED_IN Thread | a Slack message attached to a Linear issue (Linear's Slack integration); CREATED_FROM when attached as the issue was made |
| Ticket LINKS_TO PullRequest | a GitHub PR attached to a Linear issue (Linear's GitHub integration) |
| PullRequest IMPLEMENTS Ticket | the PR's head branch is the issue's `branchName`, or the title names the key |
| Message ANNOUNCES Ticket / PullRequest | a Linear, Jira or GitHub bot posting in Slack |
| Message MENTIONS Ticket / Account / Channel, LINKS_TO Ticket / PullRequest | keys, @-mentions, #channels and URLs in a message |
| Person HAS_ACCOUNT | the same verified email in Slack and Linear (and GitHub SSO) |

## Tests

```powershell
python -I ekg-eval-data/test_adapters.py
```

The test runs the whole pipeline on `fixtures/acme/`. That's a small synthetic company in the real Linear GraphQL and Slack export shapes, plus a GitHub and git slice; none of it is real people. The test checks that every item validates and that the links above exist. It also checks access the way the spec's access layer works: a person's verified accounts, then their groups, then their grants, at a point in time. For example:
* a guest sees only their channels
* someone who left a private channel saw it only while they were in it
* a private Linear team is hidden from non-members
* a private repo is hidden from a collaborator whose GitHub account is unverified

Positive controls make sure an access layer that showed nothing at all would fail.

**More schema issues the Linear and Slack adapters found:**
* **IN_STATE `cause` has no plain `automation`.** The spec says cause "adds automation | transition", but the registry's enum doesn't include them. Linear's auto-close becomes `git_automation` when the issue has a GitHub link; otherwise cause is left out.
* **MENTIONS `kind` has no `mention` or `channel`.** The spec lists them, but the registry allows only url, key, path, symbol, sha and branch. Slack @-mentions and #channel mentions carry no kind.
* **A chat Thread can't be placed in its Channel.** IN and PART_OF don't allow Thread → Channel. A thread is tied to its channel only through its messages.

## What this data does and does not test

- **Identity resolution is a real test here.** One person appears as an ASF id (Jira, wiki, rosters), a GitHub login, and an email in git and on the lists, often an employer address rather than `@apache.org`. The public rosters do not link GitHub logins to ASF ids, so the resolution ladder has to work for it: verified email, then display name, then review.
- **Permissions are not tested.** Everything here is public. To test the access layer, add synthetic overlays: put some Jira issues under a security level, mark a list as private with `history: since_joined`, add Bcc copies and late joiners. For private data that already exists, use [HERB](https://huggingface.co/datasets/Salesforce/HERB) or [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany).
- **Not covered:** chat (ASF Slack has no public export), calendars, call transcripts, and dated HR reporting lines. HERB covers chat and meetings, the AMI corpus covers transcripts.
- **Real personal data:** names and email addresses of real people. Keep it in the local eval environment and do not commit it. `ekg-eval-raw/` is outside every service folder for that reason.
