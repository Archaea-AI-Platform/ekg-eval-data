"""Per-project settings and the input contract.

Rules that used to be written into the adapters for Kafka (the default branch, which mail senders are
bots and what they announce, the decision-proposal key) are settings here. Each comes, in order, from
the dataset's manifest.json, then the project's preset below, then the generic default. Kafka's
values are the "kafka" preset, so an old Kafka manifest behaves as before.

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
    "decision_prefix": None,      # proposal keys that are decisions, e.g. "KIP" for KIP-123; None: none
    "mail_lists": {},             # mail/<folder> -> list address; default: the List-Id header, else the folder name
    # mail senders that are integrations: what they post is a notice about an item, not a new fact
    "notifiers": {
        "jira@apache.org": "jira",               # "[jira] [Created] (KEY-1) ..." -> ANNOUNCES the ticket
        "git@apache.org": "github",              # "URL: https://github.com/<repo>/pull/N" -> ANNOUNCES the PR / issue
        "notifications@github.com": "github",
        "builds@apache.org": "ci",               # CI results: noise, counted and not linked
    },
    "ci_subject": r"^\[(GH|CI|BUILD)\]|workflow run|build (failed|fixed|is back)|\bjenkins\b",  # github notices that are CI
    "issue_tracker": None,        # "github" when tickets are GitHub issues: #N in text may name an issue
    "infer_people": "auto",       # people from commit and mail authorship: "auto" = only when there is no directory
    "public": True,               # the sources are public (granted to the Public principal)
}
PRESETS = {
    "kafka": {"default_branch": "trunk", "decision_prefix": "KIP"},
    "flink": {"default_branch": "master", "decision_prefix": "FLIP"},
    "spark": {"default_branch": "master", "decision_prefix": "SPIP"},
    "tvm": {"default_branch": "main", "decision_prefix": "RFC", "issue_tracker": "github"},
}

# folder -> (files the adapter reads, fields each JSON(L) row needs, the manifest keys it needs)
CONTRACT = {
    "people": (["people/public_ldap_people.json", "people/committee-info.json", "people/public_ldap_projects.json"], {}, ["project"]),
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
    if not manifest.get("tenant"):
        name = data.resolve().name
        manifest["tenant"] = "apache" if name.startswith("apache-") else name.replace(":", "-")
    if not manifest.get("project") and data.resolve().name.startswith("apache-"):
        manifest["project"] = data.resolve().name[len("apache-"):]
    return manifest, notes


def project_settings(manifest: dict, data: Path) -> dict:
    """Effective settings: manifest overrides, then the project's preset, then the defaults."""
    s = json.loads(json.dumps(DEFAULTS))
    preset = PRESETS.get((manifest.get("project") or "").lower(), {})
    for src in (preset, manifest.get("settings") or {}):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k] = {**s[k], **v}
            else:
                s[k] = v
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
            if not list(data.glob(pattern)):
                problems.append(f"missing {pattern}")
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
