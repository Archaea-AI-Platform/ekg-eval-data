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
| `people.py` | A people directory (`people/directory.json`, `.jsonl` or `.csv`, or the ASF rosters read into the same rows) to Person, Account, EmailAddress, Group with dated MEMBER_OF, Position with HOLDS_POSITION, REPORTS_TO; verified HAS_ACCOUNT |
| `jira.py` | Tickets, states, IN_STATE and ASSIGNED_TO history and description versions from the changelog, one Activity + FieldChanges per change, versions, labels, comments, ticket links |
| `github.py` | Repo, PRs, reviews, review threads, comments, branches, dated labels and review requests, changed files, derived APPROVED_BY (marked stale after new commits), CI, tags |
| `git.py` | Commits (this kit's shape, `{subject, body, files_changed}`, or GitHub REST commit objects), parents, authors and co-authors as EmailAddress, files changed; a noreply address links to its GitHub account. Makes the Repository itself when there is no `github/` folder |
| `issues.py` | GitHub issues (`issues/*.json(l)`, scraper or REST shape) to Ticket `<owner/repo>#<n>`, open/closed states with dates, labels, assignees, comments, linked PRs; `#n` in text is resolved to the issue or the PR |
| `code.py` | The code tree (`code/`, a checkout or a `tree.json(l)` listing) to Folder and File (merged with git's Files), README/docs text, and for Python: module, class, function and method CodeSymbols DEFINED_IN their file, IMPORTS between modules |
| `symbols.py` | The code parse for any language, on tree-sitter: what a language's symbols look like is data (the grammar pack's tags query plus the extra patterns in `QUERIES`), so adding a language adds no code. File, class, interface, trait, enum, struct, record, object, function, method and constructor CodeSymbols with qualified name, signature and lines, DEFINED_IN their file; IMPORTS, EXTENDS (role extends or implements) and CALLS only where the language's own name binding settles the target inside the repository; everything else is counted. A file type with no grammar or no symbol query is skipped and counted. `map_symbols(g, repo, scope, rid, files, texts)`; `extract(path, text)` for one file |
| `mail.py` | Lists (address from settings, else List-Id, else folder name; any `.mbox` name), messages (an id is generated when Message-ID is missing; duplicates kept once), threads, senders. Notifier senders from settings: Jira notices ANNOUNCE their ticket, `git@apache.org` / GitHub notices their PR or issue, CI mail is counted as noise. Human mail DISCUSSES the keys in its subject and MENTIONS those in its own (unquoted) text; proposal threads queue MADE_IN |
| `forum.py` | A Discourse forum (`forum/topics/*.json`) to Thread (kind discussion) and Comment posts, with REPLY_TO, authors, and ticket, PR and `#n` references |
| `doctext/` | The document readers, with no graph knowledge: Confluence storage XHTML, HTML, Markdown (with front matter), text / rst, PDF (pdfplumber for tables, else pypdf), .docx, .pptx, .xlsx (standard library), .csv / .tsv, each to one `DocText`: ordered blocks (heading, paragraph, list item, table, code, quote, and page / slide / sheet containers) with their refs (address, page link, attachment, image, user mention, ticket key, email), the canonical text with every block's offsets, the file's own properties, and a `quality` record of what could not be read. `read(path_or_bytes, name)` picks the reader. Nothing is cut |
| `docgraph.py` | The one emitter every document source uses: a `DocText` to DocSection (per heading, page, slide, sheet), Piece (`text`, `kind`, `heading`, `order`, `span`; about 4,000 characters, a long table cut between rows with the header repeated), NEXT, the links its refs make, FileMetadata, front-matter status / date / owner, SAME_CONTENT_AS, and the `documents` section of the build report (how well each document was read) |
| `wiki.py` | Space, pages, version history, sections and pieces for every version that has a body, attachments, labels, comments, page and section editors; each proposal page (the settings' `decision_prefix`, KIP for Kafka) becomes a Decision, its status read from the page's state line as the reader gives it |
| `docs.py` | Stored documents (`docs/` at any depth: every format `doctext` reads) to File (sha256, size, mime type), a ContentVersion keyed by the content hash and dated by the file's time, sections and pieces, with ticket and `#n` MENTIONS. `map_file(g, name, bytes, meta, scope)` takes one file as a connector hands it over; the folder walk is one caller |
| `settings.py` | Per-project settings and presets (kafka, flink, spark, tvm), and the input contract checked before the run |
| `linear.py` | Linear workspace, users, teams (states, labels, cycles), projects (status updates, milestones), issues with IN_STATE / ASSIGNED_TO / HAS_LABEL history, comments, attachments, relations, documents. Private teams are scoped to their members |
| `slack.py` | Slack workspace, users, channels, private channels, group DMs and DMs, messages, threads, files, reactions, pins, topic and rename history. Public channels are granted to full members; private ones to their members, dated from join and leave events |
| `linker.py` | Resolves the references every adapter queued (keys, PR numbers, shas, KIPs) across sources; tool-made references outside the window get stub records |
| `identity.py` | Attaches accounts and addresses to Persons, strongest evidence first: the directory, then verified emails (Slack, Linear) and GitHub SAML SSO identities, which are verified; the rest are inferred and grant nothing. With no directory, a Person is made per verified email, and (setting `infer_people`, default `auto`) people are inferred from commit and mail authorship: addresses with the same full name are one person, a GitHub account joins through its noreply address or the same login as a forum account, a forum account through a unique full name. All inferred |
| `pipeline.py` | Checks the inputs, runs each adapter whose folder is present (one failing adapter is reported and the rest still run), measures coverage, builds the report |

### Project settings and the input contract

No project's or organisation's habits are in the code. They are settings: generic defaults, then an organisation's shared conventions (a family, e.g. `apache`), then a preset per project (picked by the manifest's `project`), then overrides in the manifest's `"settings"`:

```json
{"project": "tvm", "repo": "apache/tvm", "tenant": "apache", "fetched_at": "2026-10-30T00:00:00Z",
 "settings": {"default_branch": "main", "decision_prefix": "RFC", "issue_tracker": "github",
              "mail_lists": {"dev": "dev@tvm.apache.org"},
              "notifiers": {"notifications@github.com": "code_host", "builds@apache.org": "ci"},
              "infer_people": "auto", "public": true}}
```

| Setting | Default | Used by |
|---|---|---|
| `default_branch` | `github/repo.json`, else `main` | git (which commits are on the main line) |
| `decision_prefix` | none (`KIP` for kafka, `FLIP` flink, `SPIP` spark, `RFC` tvm) | wiki (Decision pages), mail and git (MADE_IN / ENACTS) |
| `issue_tracker` | `github` when there is an `issues/` folder | `#n` and "Fixes #n" in commits, PRs, mail, forum and docs |
| `mail_lists` | List-Id header, else the folder name | mail |
| `notifiers`, `tracker_notice_subject`, `ci_subject` | `notifications@github.com` only (the `apache` family adds `jira@apache.org`, `git@apache.org`, `builds@apache.org` and the `[jira] [Created] (KEY-1)` subject) | mail: which senders are integrations (`tracker`, `code_host`, `ci`) and where a tracker notice names its ticket |
| `vote_threads` | off (on for the `apache` family) | mail: votes and tallies in `[VOTE]` threads about a decision key |
| `release_branches`, `release_branch_since` | common namings (`4.3`, `release-1.18`, `branch-3.5`, `v0.15`); from the window's start (kafka: `\d+\.\d+`, from 2024-01-01) | git: which branches' commits are read as release-branch commits |
| `directory_tool`, `directory_name`, `directory_link_method`, `directory_email_domain`, `directory_login_tools` | `directory`, the tenant, `admin` (`csv` for a CSV), none, none (the `apache` family: `asf`, `apache`, `apache.org`, Jira and Confluence) | people and identity |
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
| Page sections couldn't be DocSections | PART_OF allows DocSection (-> Document) | wiki: a page's top sections are PART_OF the page, deeper ones SECTION_OF their parent |

Still open (not needed to store the data):

* **`formality` is blocked on sightings.** The spec stores a per-source `formality` on MADE_IN, EXTRACTED_FROM, RESTATED_IN, DOCUMENTED_IN and MIRRORS. Its Foundation section and the registry treat `formality` as computed everywhere (R66). The adapters leave it out.
* **Some enumerated states aren't enforced.** `PullRequest.state` (R70), `Review.state` and `TicketState.type` accept any string.
* **There's no body-text field on Ticket or PullRequest.** Descriptions go into ContentVersion. The adapter guide should say so.
* **Tickets can't be AUTHORED_BY their reporter**, although the Tickets adapter contract says so. The adapters use CREATED_BY role reporter.

## The landing layout: how a live connector plugs in

The interface between "getting the data" and "building the graph" is the dataset folder: one sub-folder per source, in the shapes below, plus `manifest.json`. `fetch_apache.py` and `fetch_linear.py` are two ways to fill it; a live connector is a third. It writes the same shapes (or calls the same map function with rows), and nothing downstream changes: the linker, identity resolution, validation, the SQLite store and the LLM fill only read records, links and the adapters' shared lookups.

The contract is `settings.CONTRACT`, and `check_inputs` checks a folder against it before anything runs:

| Folder | Files the adapter reads | Required in them | Map function |
|---|---|---|---|
| `people/` | `directory.json`, `directory.jsonl` or `directory.csv` (one row per person), or the ASF roster files | `id` per row | `people.map_people`, or `people.emit_directory(g, {"people": rows}, settings)` with rows in hand |
| `jira/` | `statuses.json`, `versions.json`, `issues/*.json` (REST v2 search pages with changelog) | manifest `jira` (the project key) | `jira.map_jira` |
| `linear/` | `organization.json`, `users.json`, `teams.json`, `issues/page-*.json` (GraphQL shapes) | | `linear.map_linear` |
| `github/` | `repo.json`, then `pulls/`, and the other REST dumps | `id`, `full_name` | `github.map_github` |
| `issues/` | `issues/*.json` or `.jsonl` | `number`, `title`; manifest `repo` | `issues.map_issues` |
| `git/` | `commits.jsonl` (any shape `git.normalize_commit` reads); optionally `repo.git` for tags and release branches | `sha` | `git.map_git` |
| `mail/` | `<list>/*.mbox` or `*.mbox` | | `mail.map_mail` |
| `slack/` | `users.json` and the export's channel folders | | `slack.map_slack` |
| `wiki/` | `space.json`, `pages/*.json` | | `wiki.map_wiki` |
| `forum/` | `topics/*.json` | `id`, `title` | `forum.map_forum` |
| `code/` | a listing (`tree.jsonl`: `{path, sha}`) or the files themselves | | `code.map_code`; symbols of any language: `symbols.map_symbols(g, repo, scope, rid, files, texts)` |
| `docs/` | any documents | | `docs.map_docs` |

The people directory is the shape an HR system or identity provider connector writes (`settings.DIRECTORY_ROW`); only `id` is required:

```json
{"id": "e-101", "name": "Bob Ruiz", "emails": ["bob@acme.io"], "title": "Backend Engineer", "manager": "e-100",
 "start_date": "2025-02-01", "end_date": null,
 "teams": [{"name": "Payments", "since": "2025-02-01"}],
 "position": {"id": "pos-41", "name": "Backend Engineer II"},
 "accounts": {"github": "bob-gh", "slack": "U02BOB"}}
```

It becomes a Person, the person's Account in the directory (HAS_ACCOUNT, verified), an EmailAddress per address, a Group per team with dated MEMBER_OF, a Position with dated HOLDS_POSITION (the person's own seat, named after the title, when the row names no seat), REPORTS_TO the manager, and a verified HAS_ACCOUNT to each account in another tool that the row names. A last day is stored as the exclusive end. The ASF roster reader produces these same rows from whimsy's files, so both go through one function.

What a connector has to supply besides the files is in `manifest.json`:

- `tenant` (else the folder name), `fetched_at` (else now), `repo`, `jira`, `window`, and optionally `id_secret` (the key record ids are derived with).
- `settings`: anything in `settings.DEFAULTS` that differs for this source. The defaults name no organisation. A project's habits are a preset (`PRESETS`), and an organisation's shared ones are a family (`FAMILIES`, e.g. `apache`: its notifier addresses, its roster's tool name and mail domain, votes on the dev list); a preset names its family and a manifest may too (`"family": "apache"`).

Adding a tool that has no adapter yet is one module with a `map_<tool>(g, data_dir, ...)` function, a line in `CONTRACT` and a line in `pipeline.ADAPTERS`. Its `source_tool` name needs no registration: a tool the kit has not seen is counted under the pillar of each record's type. Only two adapters take rows directly today (`people.emit_directory`, `symbols.map_symbols`, and `git.normalize_commit` for one commit); the others read their folder, so a connector that streams rows writes them to the folder first.

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

`test_symbols.py` reads Java, Scala, Python, TypeScript, Go and C# snippets with the one extractor (symbols, nesting, imports, an unknown file type skipped) and builds a small mixed-language repository to check DEFINED_IN, IMPORTS, EXTENDS and CALLS. `test_directory.py` lays `fixtures/acme_directory/` over `fixtures/acme/`: a generic people directory, a mailing list and settings that name no foundation (tenant `acme`, directory tool `workday`); it checks the directory's records and links, that the directory's Person is the one every tool's account joins, that no record carries another organisation's tenant, tool or address, and that the proposal and vote logic does nothing when its settings are off.

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

`fill_graph.py` (and step 2 of `run_pipeline.py`) runs after the adapters. The model only proposes; code decides what is written. No person reviews the result. Code checks every answer: a choice must be one of the candidates shown, a quote must be word for word in the text shown, every name is resolved against the graph, every link must be one the registry allows between those two types, and every record and link must pass query-master's validator. What passes lands in `<graph>/llm/` (or `--out`): `records.jsonl` and `links.jsonl` (only what this stage added or changed), `ledger.jsonl` (every verdict, rejections included, and the cache of verdicts), `responses.jsonl` (every reply with its prompt and token usage, so a stopped run pays nothing twice), `patterns.json` (cues behind accepted answers, candidates for adapter rules) and `report.json`.

```bash
./with_env.sh .venv/Scripts/python.exe -I ekg-eval-data/fill_graph.py --graph ekg-eval-out/apache-kafka-v2 --dry-run   # tasks, windows, prompt sizes; no call
./with_env.sh .venv/Scripts/python.exe -I ekg-eval-data/fill_graph.py --graph G --smoke 2                  # two real calls, nothing written
./with_env.sh .venv/Scripts/python.exe -I ekg-eval-data/fill_graph.py --graph G --kinds table_map,doc_facts --docs 40 --out G-sample/llm
```

**Kinds.** `pr_ticket`, `commit_pr`, `ticket_fix`, `email_topic`, `identity`, `decision_status` (one judge call per gap: pick a candidate or reject), `knowledge` (conversations: extract, then judge), `table_map` (one call per table), `doc_facts` (one call per document window, for every record and link the schema supports), `entity_choice` (made at run time when a name matches several records). `doc_knowledge` (the older two-call document reading) is not run unless asked. **Flags:** `--kinds`, `--limit N` (at most N new tasks per kind), `--docs N` (N documents, evenly spaced), `--concurrency` (calls in flight, default 16), `--window-tokens`, `--max-reply-tokens` (when the backend cannot say), `--model`, `--out`, `--dry-run`, `--smoke N`.

**Sizing (fill_budget).** No fixed character caps and no `max_tokens` on a call. The window (OpenRouter's model list, the pod, or `--window-tokens`) gives the prompt 60% of it; the reply keeps the rest, capped by the provider's reply limit (Glimmer: 131,072-token window, 16,384-token replies). A reasoning model's thinking grows with what it reads, so an extraction call reads at most half its reply in text (`read_chars`: 24,576 characters for Glimmer); a longer document is many consecutive windows, cut only between pieces, each headed by the title, the outline and its place. The schema section may take what is left (`schema_chars`, 201,353 characters for Glimmer).

**Schema in the prompts.** Written from the registry (extract_schema.Catalog), with the registry's description (`about`) of each offered type, link (what "A LINK B" means and which way it is stored) and field, as far as `schema_chars` allows; layers give way in the order record fields, link fields, links, types, each first cut to its first sentence. The document prompt carries all four layers (10,120 characters without descriptions, 36,409 with them on ACME). The table prompt carries type and link descriptions only (77,700 characters; with every layer it was 148,510 and mappings thought for up to 11,000 tokens). The report's `schema_descriptions` gives both sizes and what was let in. The document rules tell Requirement from Guideline by their descriptions: what a product, system or change has to do is a Requirement whatever its section is called, and an item a requirements document numbers with its own id is one, named with that id first.

**Order of work.** (1) Tables: every mapping is asked; every table's rows are written (the table that identifies its records best first), then every table's links, so a commit log's `#174` finds the pull request the merge log made. (2) The catalog is rebuilt with the types the tables added (pull requests, commits, files, accounts), and the document windows are built again with every table no mapping fully reads in them as text. (3) Documents (`doc_facts`) and the other kinds. (4) Names with several matching records (`entity_choice`). (5) Id references, in code only: a stated record whose own quoted words write another record's own id (the id its name starts with; `PAY-02 ... after ORD-01 ...`), in any document, is linked to it. The registry has no Requirement-to-Requirement reference link, so the citing passage `LINKS_TO` the cited Requirement (method `text_rule`). A quote that ran on into the next item is no citation, and an id several records carry is not linked. Counts are in `id_references`.

**Tables (extract_tables).** The model sees the heading, the header and a sample of rows, never the whole table, and replies with a mapping of columns that never lists rows. Its shape is `rows` (each row is one record or one relation), `key_value` (one record, one row per field) or `none` (no column names records of a listed type: read as text). `cell_parts` split a composite cell (`#174 / PAY-AUTH`, `Lionel 42`) by a separator or a regular expression, checked on every sample row; the parts are columns `c2.1`, `c2.2`. For each column part that names a record there is a `links` entry: link type, other type, direction (`row_is`), `from_column` (the near end is the row's record, or the record another column of the same row names: a commit's pull request `CHANGES` the commit's file, a module's file is `IN` its repository), `other_key_field` (the cell holds the other record's key: a pull request's number, a file's path), `split`, and the columns holding the link's own fields (a date, a role, a count). `schema_gap` names what no listed link or field can hold (commit counts per person and module: no link from a Person or Account to a Service has a count field). Code holds the mapping to the registry (direction fixed where only one way exists), applies it to every row, finds each record by key field, id, email, login or name, and creates one only where the graph has none (a tool's record from a table counts as "no such record from its tool yet"). Every cell that cannot be written, and every column no mapping reads, is counted. A mapping that leaves a column unread or names a schema gap is kept for what it stores and its table is read as text too. A mapping is rejected when it fails the registry, when a split does not parse a sample row (asked again once with the reason), when it lists more entries than the table has columns, or when its reply ran into the reply limit, took more than three quarters of it (`mapping_reply_tokens`, 12,288 for Glimmer), or was still running after that many tokens at a slow pace (`mapping_seconds`, 307 s: no call can be capped, and on ACME v5 one mapping ran 11 minutes after the rest were done). A regular expression that fails a sample cell only by its character classes is read by the plain text it writes between its groups (`#(\d+) / (\w+)` on `#174 / PAY-AUTH`), and a placeholder cell (`—`) is no value rather than a failed split. A rejection stands next run and its table is read as text. `report.kinds.table_map.acceptance` has one row per table.

**Documents (extract_facts).** Each window's answer lists records and links with a quote and its passage label. Code checks each quote word for word (after PDF normalisation; a quote cited to the wrong passage is moved to the one passage that holds it), resolves names (several matches lead to an `entity_choice`; none creates the record only for a type a document may create), checks each link against the registry, and writes the origin link the schema requires (EXTRACTED_FROM, MADE_IN ...) or, for an entity, Piece MENTIONS. A numbered item met again in its own document (a follow-up reading, the next window) is the record already made for it. A link with an `any` end in the registry is offered for the types a document states, so a Requirement can be ABOUT the Service or Topic it concerns. Coverage is guaranteed by code: the paragraphs no accepted quote touches are read again in smaller follow-up calls, at most twice; what still yields nothing is reported as read with nothing kept (`coverage_by_document`).

**Confidence** (fixed priors, never the model's opinion, times the document's extraction quality): 0.95 for a mapped table row (method `template_rule`), 0.85 when every name resolved to one record, 0.70 when a record was created from the text, 0.60 when the model chose among candidates (`llm_judge`), 0.80 for an id reference found by code (`text_rule`).

**Report.** Per kind: tasks, calls, accepted, rejected, errors and why; `calls.usage` (prompt, completion and reasoning tokens and cost as the provider reported them, by step) and `calls.wall_seconds`; `budget`; `schema_descriptions`; `schema_gaps`; `id_references`; `written` (records and links by type, method and confidence, every reason a fact was not written, possible duplicate people).

The `knowledge` task reads mail threads, chat threads, forum topics, and PR and ticket comment threads. It extracts decisions, guidelines, requirements, knowledge, action items and open questions, then judges them. Each item links to its origin and to its person through the author of the quoted message.

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

## Asking the graph: tools and evaluation

query-master's `app/ekg/query/` reads a built graph through a per-person view and gives an LLM tools over it
(the spec's 10 core tools and 10 extras, plus `overview`, `experts`, `discussions` and `schema`). These
scripts use it; run them with query-master's venv python.

```bash
export LLM_BACKEND=openrouter OPENROUTER_API_KEYS=<key> EKG_LLM_MODEL=mistralai/devstral-2512
PY=query-master/.venv/Scripts/python.exe
$PY -I ekg-eval-data/ask_graph.py --list-tools
$PY -I ekg-eval-data/ask_graph.py --tool overview --args '{"id": "KAFKA-20901"}'        # one tool, no LLM
$PY -I ekg-eval-data/ask_graph.py "Which PR fixed KAFKA-20901 and who approved it?"     # the agent
$PY -I ekg-eval-data/ask_graph.py --as "David Jacot" "What is David working on?"          # as one person
$PY -I ekg-eval-data/make_eval_set.py      # questions + gold answers from the graph -> eval/kafka_questions.json
$PY -I ekg-eval-data/run_eval.py           # ask them all, score -> eval/results/{results.jsonl,summary.json}
```

`make_eval_set.py` works the gold answers out from the stored records and links directly, not through the tools,
in 11 categories: ticket → fixing PR and approver, PR → ticket, commit → PR and author, ticket status and
assignee, state at a past date, KIP status and its thread, top contributor under a directory, counts, a person's
accounts, decisions and rules about a ticket, and questions whose right answer is "nothing". `run_eval.py` keeps
each answer with every tool call, so a failure can be read step by step. A hand-written set (the ACME questions,
once their data is in this layout) uses the same file shape: `{"graph": ..., "as_of": ..., "questions": [{"id",
"category", "question", "asker", "gold": {"must": [[...]], "none": false}}]}`.

The same tools are on query-master's internal endpoints (service token): `GET /api/v1/ekg/internal/query/tools`,
`POST .../query/tool {tool, args, asker}`, `POST .../query/ask {question, asker}`, with the graph from
`EKG_GRAPH_SQLITE` (e.g. `ekg-eval-out/apache-kafka/graph.sqlite`).

## Combined Kafka data and the shared query set

`merge_extra.py` merges an extra-data drop (`kafka-eval-shared/extra-data`) into the download, in its own
layout, without replacing anything (`<raw>/extra/merge_report.json` lists every decision):

```bash
python -I ekg-eval-data/merge_extra.py --extra <kafka-eval-shared>/extra-data --raw ekg-eval-raw/apache-kafka --full-git
```

For Kafka it added mail from 2023-01 to 2026-06 and 2026-11 (47 monthly archives), 592 PRs (7 with full
detail, 585 known only from search hits, which the GitHub adapter loads as partial PRs), job lists for 41 CI
runs (Check records), 254 Jira tickets that were stubs before, 1,715 more Confluence page versions, the whole
git history of HEAD (18,544 commits, from `git/repo.git`; older commits without rename detection) and the file
tree at HEAD (7,642 files). Rosters, board minutes, remote tags and fixVersion totals are kept under `extra/`;
no adapter reads them yet. It reads the blobless `repo.git` with lazy fetch and automatic gc turned off.

The shared query set is `eval/kafka_shared_queries.jsonl` (119 questions, from `2_trimmed.jsonl`). Each has
a gold answer, acceptable variants, a checklist of required facts, phrases it must not say, and grading
`script` (a variant must appear) or `judge` (an LLM judge checks the checklist); `eval_grading.py` does both.
Every question is asked as of its own date.

```bash
$PY -I ekg-eval-data/run_eval.py --questions ekg-eval-data/eval/kafka_shared_queries.jsonl          # in process
QUERY_MASTER_SERVICE_TOKEN=<token> $PY -I ekg-eval-data/run_eval.py --questions ... --api http://127.0.0.1:7111
$PY -I ekg-eval-data/run_eval.py --questions ... --api http://127.0.0.1:7111 --user-token <web app token>
$PY -I ekg-eval-data/demo_app.py [--api http://127.0.0.1:7111]      # http://127.0.0.1:8765
python -I ekg-eval-data/test_eval_grading.py
```

With `--api`, questions go to query-master's graph agent over HTTP (its streamed search endpoint, on its own
store), the way the web app asks. The graph gets into query-master's store with
`python -m app.ekg.query.load --sqlite <graph.sqlite>` (see query-master's `app/ekg/README.md`).
`demo_app.py` shows the question set with the last run's answers and grades, and runs any question live,
tool call by tool call.
