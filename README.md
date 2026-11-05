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
| `git.py` | Commits (this kit's shape, `{subject, body, files_changed}`, or GitHub REST commit objects), parents, authors and co-authors as EmailAddress, files changed; a noreply address links to its GitHub account. Makes the Repository itself when there is no `github/` folder |
| `issues.py` | GitHub issues (`issues/*.json(l)`, scraper or REST shape) to Ticket `<owner/repo>#<n>`, open/closed states with dates, labels, assignees, comments, linked PRs; `#n` in text is resolved to the issue or the PR |
| `code.py` | The code tree (`code/`, a checkout or a `tree.json(l)` listing) to Folder and File (merged with git's Files), README/docs text, and for Python: module, class, function and method CodeSymbols DEFINED_IN their file, IMPORTS between modules |
| `mail.py` | Lists (address from settings, else List-Id, else folder name; any `.mbox` name), messages (an id is generated when Message-ID is missing; duplicates kept once), threads, senders. Notifier senders from settings: Jira notices ANNOUNCE their ticket, `git@apache.org` / GitHub notices their PR or issue, CI mail is counted as noise. Human mail DISCUSSES the keys in its subject and MENTIONS those in its own (unquoted) text; proposal threads queue MADE_IN |
| `forum.py` | A Discourse forum (`forum/topics/*.json`) to Thread (kind discussion) and Comment posts, with REPLY_TO, authors, and ticket, PR and `#n` references |
| `wiki.py` | Space, pages, version history, a Piece per section, labels, comments; each proposal page (the settings' `decision_prefix`, KIP for Kafka) becomes a Decision, its status read from the page's state line |
| `docs.py` | Stored documents (`docs/`: PDF with pypdf when installed, .xlsx, .csv/.tsv, .md/.txt/.rst, .html) to File, ContentVersion and a Piece per page or sheet, with ticket and `#n` MENTIONS |
| `settings.py` | Per-project settings and presets (kafka, flink, spark, tvm), and the input contract checked before the run |
| `linear.py` | Linear workspace, users, teams (states, labels, cycles), projects (status updates, milestones), issues with IN_STATE / ASSIGNED_TO / HAS_LABEL history, comments, attachments, relations, documents. Private teams are scoped to their members |
| `slack.py` | Slack workspace, users, channels, private channels, group DMs and DMs, messages, threads, files, reactions, pins, topic and rename history. Public channels are granted to full members; private ones to their members, dated from join and leave events |
| `linker.py` | Resolves the references every adapter queued (keys, PR numbers, shas, KIPs) across sources; tool-made references outside the window get stub records |
| `identity.py` | Attaches accounts and addresses to Persons, strongest evidence first: the directory, then verified emails (Slack, Linear) and GitHub SAML SSO identities, which are verified; the rest are inferred and grant nothing. With no directory, a Person is made per verified email, and (setting `infer_people`, default `auto`) people are inferred from commit and mail authorship: addresses with the same full name are one person, a GitHub account joins through its noreply address or the same login as a forum account, a forum account through a unique full name. All inferred |
| `pipeline.py` | Checks the inputs, runs each adapter whose folder is present (one failing adapter is reported and the rest still run), measures coverage, builds the report |

### Project settings and the input contract

Kafka's habits are no longer in the code. They are settings, with a preset per project (picked by the manifest's `project`) and overrides in the manifest's `"settings"`:

```json
{"project": "tvm", "repo": "apache/tvm", "tenant": "apache", "fetched_at": "2026-10-30T00:00:00Z",
 "settings": {"default_branch": "main", "decision_prefix": "RFC", "issue_tracker": "github",
              "mail_lists": {"dev": "dev@tvm.apache.org"},
              "notifiers": {"notifications@github.com": "github", "builds@apache.org": "ci"},
              "infer_people": "auto", "public": true}}
```

| Setting | Default | Used by |
|---|---|---|
| `default_branch` | `github/repo.json`, else `main` | git (which commits are on the main line) |
| `decision_prefix` | none (`KIP` for kafka, `FLIP` flink, `SPIP` spark, `RFC` tvm) | wiki (Decision pages), mail and git (MADE_IN / ENACTS) |
| `issue_tracker` | `github` when there is an `issues/` folder | `#n` and "Fixes #n" in commits, PRs, mail, forum and docs |
| `mail_lists` | List-Id header, else the folder name | mail |
| `notifiers`, `ci_subject` | Jira, `git@apache.org`, GitHub, `builds@apache.org` | mail: which senders are integrations and which are CI noise |
| `infer_people` | `auto` (only when there is no directory) | identity |

Before anything runs, `check_inputs` reads the contract in `settings.py`: per folder, the files the adapter needs, the fields its rows need and the manifest keys. A missing file or manifest key skips that adapter with the reason; an unreadable file or bad JSONL lines are reported and the adapter skips just those rows. The report's `adapters` section says, per folder, `ran`, `skipped` or `failed` and why, and `notes` counts every skipped row by reason. Without a `manifest.json` the tenant is the folder name and `fetched_at` is now.

On Kafka, 2026-07-31 .. 2026-10-29, the raw JSON as-is passes 0 of 3,659 checks. The adapters produce 76,488 records and 145,170 links across 32 record types and 46 link types. All of them pass, and 9,519 of the links join two pillars. Coverage, identity results and the full list of schema issues are in the PDF.

"Valid" means the right shape: types, fields, enumerations, UTC times, endpoints, the envelope. It does not check the rules about timelines (no overlapping states, R6) or whether values are right.

### Schema issues found, and what was changed

Fixed in query-master (spec `app/ekg/spec/ekg-pillars.html`, builder `scripts/build_ekg_registry.py` v2, regenerated `registry.json`):

| Gap | Change | Used by |
|---|---|---|
| `role`, `kind`, `action` gave ~55,000 "not a field" warnings | The builder now registers the spec's link discriminators (role, kind, aspect: the fact key) on every link, and `action` where the spec names its values. Named values stay advisory | every adapter |
| MENTIONS `kind` had no `mention` / `channel` | Values the spec lists one by one ("kind mention; kind channel") now join the field's enumeration | Slack |
| IN_STATE `cause` had no `automation` | "cause adds automation\|transition" is now read (the builder dropped values written with `\|`) | Linear |
| Decision had no status for a rejected proposal | `status` adds `rejected`, `withdrawn` | wiki (rejected; discarded/abandoned = withdrawn; dormant = proposed) |
| A KIP's key and name lived only in `statement` | Decision `identifier`, `title`; Guideline and Requirement `title` | wiki, LLM fill |
| DECIDED_BY / ASSIGNED_TO needed an Account | Both also go to Alias and EmailAddress, as AUTHORED_BY does | LLM fill (mailing-list deciders and owners) |
| A sender known only by name had nowhere to go | EmailMessage `sender_name` | mail (list-rewritten senders) |
| A chat Thread couldn't be IN its Channel | IN allows Thread | Slack |
| Page sections couldn't be DocSections | PART_OF allows DocSection (-> Document) | not used yet: wiki sections are still Pieces |

Still open (not needed to store the data):

* **`formality` is blocked on sightings.** The spec stores a per-source `formality` on MADE_IN, EXTRACTED_FROM, RESTATED_IN, DOCUMENTED_IN and MIRRORS. Its Foundation section and the registry treat `formality` as computed everywhere (R66). The adapters leave it out.
* **Some enumerated states aren't enforced.** `PullRequest.state` (R70), `Review.state` and `TicketState.type` accept any string.
* **There's no body-text field on Ticket or PullRequest.** Descriptions go into ContentVersion. The adapter guide should say so.
* **Tickets can't be AUTHORED_BY their reporter**, although the Tickets adapter contract says so. The adapters use CREATED_BY role reporter.

## Linear and Slack

The pipeline runs every adapter whose folder is present in the data directory: `people/`, `jira/`, `linear/`, `github/`, `issues/`, `git/`, `code/`, `mail/`, `slack/`, `forum/`, `wiki/`, `docs/`. So one company's Linear, Slack and GitHub data go in one folder with a `manifest.json`:

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
python -I ekg-eval-data/fixtures/make_tvm.py     # writes fixtures/tvm/
python -I ekg-eval-data/test_tvm_fill.py
```

`test_tvm_fill.py` runs the pipeline on `fixtures/tvm/`, which has the shapes from the TVM test run: GitHub issues instead of Jira, `{subject, body, files_changed}` commits, an mbox with no List-Id that holds `git@apache.org` PR notices, a CI notice, a message with no Message-ID and a duplicate, a Discourse forum, a Python code tree, and a csv/xlsx/md `docs/` folder, with no directory and no `github/` folder. A bad JSONL line, an issue with no number and a broken forum file are there on purpose. It checks `#n` resolution, "Fixes #n", PR notices, inferred people, the code symbols and imports, and the skip-and-report handling. It also tests the LLM fill writer with a stand-in model (no key, no cost): the quote check, and the Decision, ActionItem, Guideline and Knowledge records with their DECIDED_BY, ASSIGNED_TO and AUTHORED_BY links.

The test runs the whole pipeline on `fixtures/acme/`. That's a small synthetic company in the real Linear GraphQL and Slack export shapes, plus a GitHub and git slice; none of it is real people. The test checks that every item validates and that the links above exist. It also checks access the way the spec's access layer works: a person's verified accounts, then their groups, then their grants, at a point in time. For example:
* a guest sees only their channels
* someone who left a private channel saw it only while they were in it
* a private Linear team is hidden from non-members
* a private repo is hidden from a collaborator whose GitHub account is unverified

Positive controls make sure an access layer that showed nothing at all would fail.

The Linear and Slack schema gaps (IN_STATE cause `automation`, MENTIONS kind `mention` / `channel`, Thread IN Channel) are fixed: see "Schema issues found" above.

## What this data does and does not test

- **Identity resolution is a real test here.** One person appears as an ASF id (Jira, wiki, rosters), a GitHub login, and an email in git and on the lists, often an employer address rather than `@apache.org`. The public rosters do not link GitHub logins to ASF ids, so the resolution ladder has to work for it: verified email, then display name, then review.
- **Permissions are not tested.** Everything here is public. To test the access layer, add synthetic overlays: put some Jira issues under a security level, mark a list as private with `history: since_joined`, add Bcc copies and late joiners. For private data that already exists, use [HERB](https://huggingface.co/datasets/Salesforce/HERB) or [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany).
- **Not covered:** chat (ASF Slack has no public export), calendars, call transcripts, and dated HR reporting lines. HERB covers chat and meetings, the AMI corpus covers transcripts.
- **Real personal data:** names and email addresses of real people. Keep it in the local eval environment and do not commit it. `ekg-eval-raw/` is outside every service folder for that reason.

## Filling the gaps with the LLM

`fill_graph.py` runs after `build_graph.py`. Code finds every link or field the adapters left empty and the candidates for it; query-master's own LLM client accepts one candidate or rejects them all. No person reviews the result. Code then checks every acceptance: the choice must be a candidate, the quote must appear word for word in the text shown, and the result must pass query-master's validator. Accepted answers become `llm_judge` links in `<graph>/llm/`. Every verdict, including rejects, goes to `ledger.jsonl`, which is also the cache. `patterns.json` lists the recurring cues behind accepted answers, which are candidates for new adapter rules.

```bash
export LLM_BACKEND=openrouter OPENROUTER_API_KEYS=<key> EKG_LLM_MODEL=mistralai/devstral-2512
python -I ekg-eval-data/fill_graph.py --dry-run      # gaps and candidates per kind, no LLM
python -I ekg-eval-data/fill_graph.py --smoke 2      # two real calls, nothing written
python -I ekg-eval-data/fill_graph.py --limit 50     # ask, check, write
```

The `knowledge` task reads mail threads, Slack threads, forum topics, and PR and ticket comment threads. The `doc_knowledge` task reads stored documents and recently edited wiki pages, one document at a time. Both extract decisions (with rationale and date), guidelines (with strength), requirements, knowledge, action items and open questions, then judge them. Each item links to its origin, and to its person through the author of the quoted message: Decision DECIDED_BY, Knowledge AUTHORED_BY (speaker), Question ASKED_BY, and ActionItem ASSIGNED_TO the owner the model names. A link that needs an Account and only has an email address follows the address to its account, or is skipped and counted under `people_links`. For GitHub issues, a closed and completed issue counts as fixed for `ticket_fix`.

## Saving: query-master's schema as SQLite

`save_graph.py` writes `<graph>/graph.sqlite` with query-master's four EKG tables: `ekg_registry_types`, `ekg_registry_links`, `ekg_records` and `ekg_links`. The tables are generated from `app/ekg/models.py`, so the columns match it. Rows are built the way `app/ekg/schema/store.py` builds them, and every row is validated first. SQLite triggers refuse deletes and changes to a record's identity. Read-only views `node` and `claim` give the same data under the root schema's names (ekg-schema.html v0.8). `run_pipeline.py` runs the three stages in order: parse, then LLM, then save. Each stage keeps its JSONL.

```bash
python -I ekg-eval-data/save_graph.py --graph ekg-eval-out/apache-kafka
python -I ekg-eval-data/run_pipeline.py --data ekg-eval-raw/apache-kafka            # parse -> LLM -> SQLite
```

**One combined graph.** Pass several graphs (or several downloads) and an output file:

```bash
python -I ekg-eval-data/save_graph.py --graph ekg-eval-out/apache-kafka ekg-eval-out/apache-tvm --sqlite ekg-eval-out/combined.sqlite
python -I ekg-eval-data/run_pipeline.py --data ekg-eval-raw/apache-kafka ekg-eval-raw/apache-tvm --combined ekg-eval-out/combined.sqlite
```

Record ids come from the tenant and each source's own key. Within one tenant (`apache` for every ASF download), the same person, address or ticket is one row, and a stub one download left for an item outside it is replaced by the real record from the other, so its links now reach it. A real record is never replaced by a stub. `_eval_meta` records the per-graph counts and `records_merged_across_graphs`. Saving needs SQLAlchemy, which is in query-master's venv (`query-master/.venv/Scripts/python.exe`).
