"""Per-project settings and the input contract.

Nothing about one project or one organisation is written into the adapters: the default branch, how
release branches are named, which mail senders are integrations and what their notices look like, the
decision-proposal key, the tenant, the directory's own tool name. Each is a setting here and comes, in
order, from the dataset's manifest.json ("settings"), then the project's preset, then the preset's
family (an organisation whose projects share conventions, e.g. "apache"), then the generic default.
Kafka's values are the "kafka" preset on the "apache" family, so a Kafka manifest behaves as before.

The input contract (CONTRACT) says, per source folder, which files the adapter reads and which
fields it needs in them. check_inputs() reads it before the run and reports every missing file,
unreadable file and missing field, and which adapters will run or be skipped and why, instead of
stopping with a traceback halfway.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

DEFAULTS = {
    "default_branch": None,       # None: github/repo.json's default_branch, else "main"
    # which branch names are release branches (a full-match regex)
    "release_branches": r"(?:(?:release|releases|rel|branch|stable|maint)[-/_]?)?v?\d+\.\d+(?:\.(?:\d+|x))?",
    "release_branch_since": None,  # read release-branch commits from this day on; None: the manifest window's start
    "decision_prefix": None,      # proposal keys that are decisions, e.g. "KIP" for KIP-123; None: none
    "mail_lists": {},             # mail/<folder> -> list address; default: the List-Id header, else the folder name
    # mail senders that are integrations: what they post is a notice about an item, not a new fact.
    # address -> "tracker" (ANNOUNCES the ticket its subject names), "code_host" (ANNOUNCES the PR or issue
    # its URL names) or "ci" (noise, counted and not linked)
    "notifiers": {
        "notifications@github.com": "code_host",
    },
    # a regex whose group 1 is the ticket key in a tracker notice's subject; None: the first known ticket key
    "tracker_notice_subject": None,
    "ci_subject": r"^\[(GH|CI|BUILD)\]|workflow run|build (failed|fixed|is back)|\bjenkins\b",  # code-host notices that are CI
    # a person's announcement on a mailing list (a regex on its subject): one naming a release's version ANNOUNCES it
    "announce_subject": r"^\s*\[(ANNOUNCE|ANNOUNCEMENT|ANN)\]",
    "issue_tracker": None,        # "github" when tickets are GitHub issues: #N in text may name an issue
    "infer_people": "auto",       # people from commit and mail authorship: "auto" = only when there is no directory
    "public": True,               # the sources are public (granted to the Public principal)
    # "+1 (binding)" votes and tallies in [VOTE] threads about a decision key (consensus by mail); off elsewhere
    "vote_threads": False,
    # a regex for a message that withdraws an announced acceptance; None: the built-in English phrases
    "withdrawal_phrases": None,
    # the people directory (people/): the tool name its records carry, the directory's own name (None: the
    # tenant), and how its account links were made (HAS_ACCOUNT link_method: idp | ldap | csv | admin ...;
    # None: "csv" for a CSV file, else "admin")
    "directory_tool": "directory",
    "directory_name": None,
    "directory_link_method": None,
    "directory_email_domain": None,  # a roster that lists ids only: each id's address is <id>@<this domain>
    "directory_login_tools": [],     # tools people sign in to with their directory id (login == the directory's id)
    # the email domains the tenant owns (admin settings): each, and each sub-domain of one that an address in the
    # graph uses, is a Domain its addresses are MEMBER_OF. None: directory_email_domain, when that is set
    "owned_email_domains": None,
}
# An organisation's conventions, shared by its projects. folder_prefix: a dataset folder named
# <prefix><project> belongs to the family (and takes its tenant) when the manifest does not say.
FAMILIES = {
    "apache": {
        "folder_prefix": "apache-", "tenant": "apache",
        "settings": {
            "notifiers": {
                "jira@apache.org": "tracker",        # "[jira] [Created] (KEY-1) ..." -> ANNOUNCES the ticket
                "git@apache.org": "code_host",       # "URL: https://github.com/<repo>/pull/N" -> ANNOUNCES the PR / issue
                "builds@apache.org": "ci",           # CI results: noise, counted and not linked
            },
            "tracker_notice_subject": r"^\[jira\] \[\w+\] \(([A-Z][A-Z0-9]+-\d+)\)",
            "vote_threads": True,                    # consensus votes on the dev list
            "directory_tool": "asf", "directory_name": "apache", "directory_email_domain": "apache.org",
            "directory_login_tools": ["jira", "confluence"],   # ASF Jira and Confluence usernames are the ASF id
        },
    },
}
PRESETS = {
    "kafka": {"family": "apache", "default_branch": "trunk", "decision_prefix": "KIP",
              "release_branches": r"\d+\.\d+", "release_branch_since": "2024-01-01"},
    "flink": {"family": "apache", "default_branch": "master", "decision_prefix": "FLIP"},
    "spark": {"family": "apache", "default_branch": "master", "decision_prefix": "SPIP"},
    "tvm": {"family": "apache", "default_branch": "main", "decision_prefix": "RFC", "issue_tracker": "github"},
}
NOTIFIER_KINDS = {"jira": "tracker", "github": "code_host"}  # the names older manifests use

# The people directory, one row per person (people/directory.json | .jsonl | .csv). Only `id` is required.
# This is the shape an HR system or identity-provider connector writes; the ASF roster reader produces the
# same rows from whimsy's files.
DIRECTORY_ROW = {
    "id": "the person's id in the directory (required)",
    "name": "full name",
    "email": "work address; or `emails`: a list, the first is the primary",
    "title": "job title",
    "employment_type": "employee | contractor | ...",
    "location": "where the person works",
    "timezone": "IANA zone",
    "status": "active (default) | inactive | ...",
    "start_date": "first day",
    "end_date": "last day (stored as the exclusive end, the next day)",
    "manager": "the manager's id (REPORTS_TO, kind line)",
    "teams": "team names, or {id, name, kind, role, since, until} (Group + dated MEMBER_OF); or `team`: one name",
    "position": "{id, name, kind, since, until}: the seat held (Position + HOLDS_POSITION); with only a title, "
                "the person's own seat is named after the title",
    "accounts": "{tool: login or id} of the same person in other tools (HAS_ACCOUNT, verified, once that account is loaded)",
}

# folder -> (files the adapter reads, fields each JSON(L) row needs, the manifest keys it needs).
# "a|b|c" in the file list: any one of them is enough.
CONTRACT = {
    "people": (["people/directory.json|people/directory.jsonl|people/directory.csv|people/public_ldap_people.json"],
               {"people/directory.jsonl": ["id"]}, []),
    "jira": (["jira/statuses.json", "jira/versions.json", "jira/issues/*.json"], {}, ["jira"]),
    "linear": (["linear/organization.json", "linear/users.json", "linear/teams.json", "linear/issues/page-*.json"], {}, []),
    "github": (["github/repo.json"], {"github/repo.json": ["id", "full_name"]}, []),
    "issues": (["issues/*.json*"], {"issues/*.json*": ["number", "title"]}, ["repo"]),
    "git": (["git/commits.jsonl"], {"git/commits.jsonl": ["sha"]}, []),
    "mail": (["mail/*/*.mbox"], {}, []),
    "slack": (["slack/users.json"], {}, []),
    "wiki": (["wiki/space.json", "wiki/pages/*.json"], {}, []),
    "forum": (["forum/topics/*.json"], {"forum/topics/*.json": ["id", "title"]}, []),
    "code": (["code/"], {}, []),
    "docs": (["docs/"], {}, []),
}


def _family_by_folder(data: Path):
    name = data.resolve().name
    return next((f for f in FAMILIES.values() if f.get("folder_prefix") and name.startswith(f["folder_prefix"])), None)


def load_manifest(data: Path) -> tuple[dict, list[str]]:
    """The manifest, with defaults for what a hand-made dataset usually leaves out. Returns (manifest, notes)."""
    notes, path = [], data / "manifest.json"
    manifest = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}: not valid JSON ({e})")
    else:
        notes.append("no manifest.json: using defaults (tenant = folder name, fetched_at = now)")
    if "fetched_at" not in manifest:
        manifest["fetched_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if path.exists():
            notes.append("manifest has no fetched_at: using now")
    name, by_folder = data.resolve().name, _family_by_folder(data)
    if not manifest.get("tenant"):  # a family's folder ("apache-kafka") is that family's tenant; else the folder name
        manifest["tenant"] = by_folder["tenant"] if by_folder else name.replace(":", "-")
    if not manifest.get("project") and by_folder:
        manifest["project"] = name[len(by_folder["folder_prefix"]):]
    return manifest, notes


def family_of(manifest: dict, data: Path):
    """The family whose conventions apply: the manifest's "family", else the preset's, else the folder's prefix."""
    preset = PRESETS.get((manifest.get("project") or "").lower(), {})
    named = (manifest.get("settings") or {}).get("family") or manifest.get("family") or preset.get("family")
    if named:
        return FAMILIES.get(str(named).lower())
    return _family_by_folder(data)


def project_settings(manifest: dict, data: Path) -> dict:
    """Effective settings: manifest overrides, then the project's preset, then its family, then the defaults."""
    s = json.loads(json.dumps(DEFAULTS))
    preset = PRESETS.get((manifest.get("project") or "").lower(), {})
    family = family_of(manifest, data) or {}
    for src in (family.get("settings") or {}, preset, manifest.get("settings") or {}):
        for k, v in src.items():
            if k == "family":
                continue
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k] = {**s[k], **v}
            else:
                s[k] = v
    s["notifiers"] = {k: NOTIFIER_KINDS.get(v, v) for k, v in s["notifiers"].items()}
    if not s["release_branch_since"]:
        window = manifest.get("window")
        since = window.get("since") if isinstance(window, dict) else window[0] if isinstance(window, (list, tuple)) and window else None
        s["release_branch_since"] = since if isinstance(since, str) else None
    if not s["default_branch"]:
        repo = data / "github" / "repo.json"
        try:
            s["default_branch"] = json.loads(repo.read_text(encoding="utf-8")).get("default_branch") or "main"
        except (OSError, json.JSONDecodeError):
            s["default_branch"] = "main"
    if not s["issue_tracker"] and (data / "issues").is_dir():
        s["issue_tracker"] = "github"
    return s


def _sample_rows(path: Path, n=50):
    """Up to n rows of a JSON array, JSONL file or JSON object, for the field check."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":  # a bad line is skipped by the adapter, so it is counted, not fatal
        rows, bad = [], 0
        for line in text.split(chr(10)):  # not splitlines(): a commit message may hold U+2028
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    bad += 1
        if bad:
            rows.append({"_bad_lines": bad})
        return rows[:n] + rows[-1:] if bad else rows[:n]
    obj = json.loads(text)
    if isinstance(obj, list):
        return obj[:n]
    return [obj]


def check_inputs(data: Path, manifest: dict) -> dict:
    """Per source folder: run or skip, and every problem found. Nothing here raises."""
    out = {}
    for folder, (files, fields, needs) in CONTRACT.items():
        if not (data / folder).is_dir():
            continue
        problems = []
        for pattern in files:
            if pattern.endswith("/"):
                continue
            if not any(list(data.glob(alt)) for alt in pattern.split("|")):
                problems.append("missing " + " or ".join(pattern.split("|")))
        for key in needs:
            if not manifest.get(key):
                problems.append(f'manifest.json has no "{key}"')
        for pattern, required in fields.items():
            for path in sorted(data.glob(pattern))[:3]:
                try:
                    rows = _sample_rows(path)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
                    problems.append(f"{path.relative_to(data)}: unreadable ({type(e).__name__})")
                    continue
                bad = sum(r.pop("_bad_lines", 0) for r in rows if isinstance(r, dict))
                if bad:
                    problems.append(f"{path.relative_to(data)}: {bad} line{'s' if bad > 1 else ''} not valid JSON (skipped)")
                rows = [r for r in rows if r]
                for f in required:
                    if rows and all(f not in r for r in rows if isinstance(r, dict)):
                        problems.append(f"{path.relative_to(data)}: rows have no '{f}' field")
        fatal = [p for p in problems if p.startswith(("missing", "manifest"))]
        out[folder] = {"run": not fatal, "problems": problems}
    return out
