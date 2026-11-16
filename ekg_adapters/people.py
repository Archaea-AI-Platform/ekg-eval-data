"""People pillar: a directory of people -> Person, Account, EmailAddress, Group, Position, and who is in what,
holds what and reports to whom.

One intermediate shape, two readers:

  1. the generic directory: people/directory.json | .jsonl | .csv, one row per person
     {id, name, email(s), title, team(s), manager, start_date, end_date, accounts{tool: login}, ...}
     (settings.DIRECTORY_ROW). This is what an HR system or identity-provider connector writes.
  2. the ASF roster (whimsy's public_ldap_people / committee-info / public_ldap_projects), read into the same rows.

emit_directory() turns the rows into records, whatever reader made them:
  Person, and the person's Account in the directory itself (HAS_ACCOUNT, verified: the directory is the
  tenant's own, so its links are admin-sourced); an EmailAddress per address (ADDRESS_OF, HAS_ALIAS);
  a Group per team with dated MEMBER_OF; a Position per seat with dated HOLDS_POSITION; REPORTS_TO the
  manager; and, for identity resolution, which accounts in other tools the directory says are this person's.
The directory's tool name, its own name and the link method are settings (directory_tool, directory_name,
directory_link_method), so nothing here names an organisation.
"""

import csv
import datetime as dt
import html
import json
import re
from pathlib import Path

from . import common
from .access import grant, place
from .common import Graph, J, day, ldap_time, utc

DIRECTORY_FILES = ("directory.json", "directory.jsonl", "directory.csv")
ROSTER_FILES = ("public_ldap_people.json", "committee-info.json", "public_ldap_projects.json")
LIST_SEP = re.compile(r"\s*[;|]\s*")


def map_people(g: Graph, d: Path, project: str = None):
    s = g.ctx.get("settings") or {}
    directory = read_directory(d / "people", g.notes)
    roster = None
    if directory is None:
        if not all((d / "people" / f).exists() for f in ROSTER_FILES):
            raise ValueError("people/ has neither directory.json|jsonl|csv nor the roster files " + ", ".join(ROSTER_FILES))
        directory, roster = asf_roster(d, project, s)
    persons, accounts, positions, scope = emit_directory(g, directory, s)
    if roster:
        _asf_chair(g, d, roster, persons, positions, scope, s)


# ---------------------------------------------------------------- reader 1: the generic directory

def read_directory(folder: Path, notes=None):
    """people/directory.json | .jsonl | .csv -> {"people": [row, ...], "groups": [...], "positions": [...],
    "link_method": ...}, or None when there is no such file. JSON may be a list of rows, or an object with
    "people" (a list, or {id: row}) and optionally "groups", "positions", "link_method"."""
    path = next((folder / f for f in DIRECTORY_FILES if (folder / f).exists()), None)
    if path is None:
        return None
    out = {"people": [], "groups": [], "positions": [], "link_method": None}
    if path.suffix == ".csv":
        out["link_method"] = "csv"
        with open(path, encoding="utf-8-sig", newline="") as f:
            for raw in csv.DictReader(f):
                row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if (v or "").strip()}
                for many in ("emails", "teams"):
                    if many in row:
                        row[many] = [x for x in LIST_SEP.split(row[many]) if x]
                accts = {k.split("_", 1)[1] if k.startswith("account_") else k.split(".", 1)[1]: row.pop(k)
                         for k in list(row) if k.startswith(("account_", "accounts."))}
                if accts:
                    row["accounts"] = accts
                out["people"].append(row)
    elif path.suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                try:
                    out["people"].append(json.loads(line))
                except json.JSONDecodeError:
                    if notes is not None:
                        notes["people: unreadable directory row skipped"] += 1
    else:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            people = obj.get("people", [])
            out["people"] = [{"id": k, **v} for k, v in people.items()] if isinstance(people, dict) else list(people)
            out["groups"], out["positions"] = list(obj.get("groups") or []), list(obj.get("positions") or [])
            out["link_method"] = obj.get("link_method")
        else:
            out["people"] = list(obj)
    return out


# ---------------------------------------------------------------- the rows -> records

def _when(value):
    """A date or a time as the row gives it -> (UTC time, precision): a bare date is that day at midnight."""
    if not value:
        return None, None
    value = str(value).strip()
    try:
        return (day(value), "day") if len(value) <= 10 else (utc(value), None)
    except ValueError:
        return None, None


def _day_after(value):
    """A last day -> the exclusive end (the next day at midnight); a full time is the end itself."""
    at, precision = _when(value)
    if at and precision == "day":
        at = (dt.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ") + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return at, precision


def _listed(row, many, one):
    """teams / team, emails / email, positions / position -> a list."""
    value = row.get(many) if row.get(many) is not None else row.get(one)
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        return [x for x in LIST_SEP.split(value) if x] if many != "positions" else [value]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def emit_directory(g: Graph, directory: dict, s: dict):
    """The directory rows as records and links. Returns (persons by id, directory accounts by id, positions by
    id, the directory's scope)."""
    tool = s.get("directory_tool") or "directory"
    public = s.get("public", True)
    scope, scope_rec = place(g, tool, "org_directory", s.get("directory_name") or common.TENANT, public=public)
    method = directory.get("link_method") or s.get("directory_link_method") or "admin"
    alias_method = "csv" if method == "csv" else "tool_link"
    notes = g.notes

    groups, positions = {}, {}

    def group(spec):
        """A team, by name or {id, name, kind}: one Group per id."""
        spec = {"name": spec} if isinstance(spec, str) else dict(spec)
        gid = str(spec.get("id") or f"team:{spec.get('name')}")
        if gid not in groups:
            groups[gid] = g.rec("Group", tool, gid, {"name": spec.get("name") or gid, "kind": spec.get("kind") or "hr_team"}, scope)
        elif spec.get("name"):
            groups[gid]["props"].setdefault("name", spec["name"])
        return groups[gid], spec

    def position(spec):
        spec = dict(spec)
        pid = str(spec["id"])
        if pid not in positions:
            opened, _ = _when(spec.get("opened_at"))
            positions[pid] = g.rec("Position", tool, pid, {
                "name": spec.get("name") or pid, "kind": spec.get("kind"), "status": spec.get("status") or "filled",
                "position_id": spec.get("position_id"), "opened_at": opened}, scope)
        return positions[pid]

    for spec in directory.get("groups") or []:
        group(spec)
    for spec in directory.get("positions") or []:
        if spec.get("id"):
            position(spec)

    persons, accounts, rows, named = {}, {}, {}, g.ctx.setdefault("directory_names_account", {})
    for row in directory["people"]:
        if not isinstance(row, dict) or row.get("id") in (None, ""):
            notes["people: directory row with no id skipped"] += 1
            continue
        uid, name = str(row["id"]), row.get("name") or row.get("full_name")
        emails = list(dict.fromkeys(e.strip().lower() for e in _listed(row, "emails", "email") if e and "@" in e))
        start, start_p = _when(row.get("start_date"))
        end, _ = _day_after(row.get("end_date"))
        created = row.get("account_created") or start
        status = row.get("status") or "active"
        rows[uid] = row
        persons[uid] = g.rec("Person", tool, uid, {
            "full_name": name, "preferred_name": row.get("preferred_name"), "status": status,
            "work_email": emails[0] if emails else None, "title": row.get("title"),
            "employment_type": row.get("employment_type"), "location": row.get("location"),
            "timezone": row.get("timezone"), "start_date": start, "hr_worker_id": row.get("hr_worker_id"),
            "person_source": "directory"}, scope)
        accounts[uid] = g.rec("Account", tool, uid, {
            "login": row.get("login") or uid, "display_name": name, "status": status, "is_bot": False,
            "emails": emails, "tool": tool}, scope)
        g.link("HAS_ACCOUNT", persons[uid], accounts[uid], "snapshot", link_method=method, verified=True,
               valid_from=created, valid_to=end)
        if not public:  # a private directory: every member holds its scope (names, titles, teams, managers)
            grant(g, accounts[uid], scope_rec, "direct", valid_from=created, valid_to=end)
        for n, addr in enumerate(emails):
            rec = g.rec("EmailAddress", "email", addr, {"address": addr, "display_name": name, "normalized_value": addr,
                        "kind": "work"}, scope)
            g.link("ADDRESS_OF", rec, accounts[uid], "snapshot", valid_from=created)
            g.link("HAS_ALIAS", persons[uid], rec, "snapshot", role="primary" if n == 0 else "secondary",
                   link_method=alias_method, verified=True, valid_from=created)
        first_team = None
        for spec in _listed(row, "teams", "team"):
            grp, spec = group(spec)
            first_team = first_team or grp
            since, since_p = _when(spec.get("since"))
            until, _ = _day_after(spec.get("until"))
            g.link("MEMBER_OF", accounts[uid], grp, "snapshot", role=spec.get("role") or "member", basis="snapshot",
                   valid_from=since, valid_from_precision=spec.get("since_precision") or since_p, valid_to=until)
        seats = [p if isinstance(p, dict) else {"name": p} for p in _listed(row, "positions", "position")]
        if not seats and row.get("title"):  # no seat id from the source: the person's own seat, named after the title
            seats = [{"id": row.get("position_id") or f"seat:{uid}", "name": row["title"],
                      "position_id": row.get("position_id")}]
        for spec in seats:
            spec = {"id": f"seat:{uid}", **{k: v for k, v in spec.items() if v not in (None, "")}}
            seat = position({**spec, "name": spec.get("name") or row.get("title")})
            since, since_p = _when(spec.get("since") or row.get("start_date"))
            until, _ = _day_after(spec.get("until") or row.get("end_date"))
            g.link("HOLDS_POSITION", persons[uid], seat, "snapshot", kind=spec.get("holding") or "primary",
                   title=row.get("title"), valid_from=since, valid_from_precision=since_p, valid_to=until)
            if first_team is not None:
                g.link("PART_OF", seat, first_team, "snapshot")
        for other, login in (row.get("accounts") or {}).items():
            if login not in (None, ""):
                named[str(other).lower(), str(login).casefold()] = uid
    for uid, row in rows.items():
        boss = row.get("manager")
        if boss in (None, ""):
            continue
        if str(boss) in persons and str(boss) != uid:
            since, since_p = _when(row.get("manager_since"))
            g.link("REPORTS_TO", persons[uid], persons[str(boss)], "snapshot", kind="line", valid_from=since,
                   valid_from_precision=since_p)
        else:
            notes["people: manager id not in the directory (no REPORTS_TO)"] += 1
    by_name = {(rows[u].get("name") or rows[u].get("full_name") or "").casefold(): u for u in persons}
    g.ctx.update(persons=persons, directory_accounts=accounts, person_by_name=by_name)
    notes["people: persons with no title, manager or employer in the source"] = sum(
        1 for r in rows.values() if not r.get("title") and not r.get("manager"))
    return persons, accounts, positions, scope


# ---------------------------------------------------------------- reader 2: the ASF roster (whimsy)

def asf_roster(d: Path, project: str, s: dict):
    """whimsy's public rosters for one project -> the directory rows, plus what the chair step needs.
    Committers and PMC members are the people; the PMC and the committers are the groups; the PMC chair is a
    seat. The roster lists ids only, so each address is <id>@<directory_email_domain>."""
    if not project:
        raise ValueError('the roster reader needs "project" in manifest.json (the project\'s id in the rosters)')
    people = J(d / "people/public_ldap_people.json")["people"]
    committees = J(d / "people/committee-info.json")["committees"]
    projects = J(d / "people/public_ldap_projects.json")["projects"]
    if project not in committees or project not in projects:
        raise ValueError(f'project "{project}" is not in the rosters under people/')
    committee, ldap = committees[project], projects[project]
    domain = s.get("directory_email_domain")
    display = committee.get("display_name", project)
    pmc, committers, chair = f"pmc:{project}", f"committers:{project}", f"chair:{project}"
    rows = []
    for uid in sorted(set(ldap["members"]) | set(ldap["owners"]) | set(committee["roster"])):
        p = people.get(uid, {})
        teams = [{"id": committers}] if uid in ldap["members"] else []
        if uid in committee["roster"]:
            teams.append({"id": pmc, "since": committee["roster"][uid].get("date"), "since_precision": "day"})
        rows.append({"id": uid, "name": p.get("name"), "emails": [f"{uid}@{domain}"] if domain else [],
                     "account_created": ldap_time(p.get("createTimestamp")), "teams": teams})
    directory = {
        "people": rows, "link_method": "admin",
        "groups": [{"id": pmc, "name": f"{display} PMC", "kind": "pmc"},
                   {"id": committers, "name": f"{project} committers", "kind": "committers"}],
        "positions": [{"id": chair, "name": f"{project} PMC chair", "kind": "chair", "status": "filled"}],
    }
    return directory, {"chair": chair, "chairs": list(committee.get("chair", {})), "display": display}


def _asf_chair(g, d, roster, persons, positions, scope, s):
    """Who chairs the PMC: the roster says who holds the seat now; the board minutes (when there) say since when."""
    chair = positions[roster["chair"]]
    terms = _chair_terms(g, d, persons, g.ctx["person_by_name"], chair, scope, s.get("directory_tool") or "directory",
                         roster["display"])
    for uid in roster["chairs"]:
        if uid in persons:
            g.link("HOLDS_POSITION", persons[uid], chair, "snapshot", kind="primary", valid_from=terms.get(uid))


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
CHAIR_ORDER = re.compile(r"Special Order (\w+), ((?:Change the|Establish the) [^,]+?), was approved")
APPOINTED = re.compile(r"that ([A-Z][\w.' -]+?)(?: \((\w+)\))? be (?:and hereby is )?appointed to the office of Vice President")
PREDECESSOR = re.compile(r"heretofore appointed ([A-Z][\w.' -]+?) \((\w+)\)\s+to the office of Vice President")
RECOMMENDED = re.compile(r"recommend ([A-Z][\w.' -]+?) \((\w+)\) as the successor")


def _chair_terms(g, d, persons, by_name, chair, scope, tool, display) -> dict:
    """The ASF board minutes (extra/people/board_minutes_*.html): each resolution that establishes the project or
    changes its chair becomes a dated Decision made in the minutes; each chair holds the seat from the day the board
    appointed them until the next appointment (HOLDS_POSITION valid_from / valid_to), and SUCCEEDS the one before.
    Returns {uid: the day the current term began}."""
    files = sorted((d / "extra" / "people").glob("board_minutes_*.html")) if (d / "extra" / "people").is_dir() else []
    if not files:
        return {}
    text = html.unescape(re.sub(r"<[^>]+>", " ", files[0].read_text(encoding="utf-8", errors="replace")))
    text = re.sub(r"\s+", " ", text)
    minutes = g.rec("Document", tool, f"board-minutes:{files[0].stem}", {
        "title": f"ASF board minutes: Apache {display} resolutions and reports (whimsy.apache.org/board/minutes/{display}.html)",
        "kind": "board_minutes"}, scope)
    # each entry starts "21 Nov 2012 ¶" (a report may add "[Jun Rao / Roman]" before the mark)
    parts = re.split(r"(\d{1,2} [A-Z][a-z]{2} \d{4})(?: \[[^\]]*\])? ¶", text)
    events = []
    for k in range(1, len(parts) - 1, 2):
        dd, mon, yyyy = parts[k].split()
        if mon.lower() not in MONTHS:
            continue
        when = f"{yyyy}-{MONTHS[mon.lower()]:02d}-{int(dd):02d}T00:00:00Z"
        body = parts[k + 1]
        order = CHAIR_ORDER.search(body)
        new = APPOINTED.search(body)
        if not order or not new:
            continue
        rec = RECOMMENDED.search(body)
        old = PREDECESSOR.search(body)
        new_uid = new.group(2) or (rec.group(2) if rec else None) or by_name.get(new.group(1).strip().casefold())
        events.append({"at": when, "order": order.group(1), "title": order.group(2), "new": new.group(1).strip(),
                       "new_uid": new_uid, "old": old.group(1).strip() if old else None,
                       "old_uid": old.group(2) if old else None, "recommended": bool(rec), "text": body[:3000]})
    events.sort(key=lambda e: e["at"])
    terms = {}
    for i, e in enumerate(events):
        statement = (f"{e['title']}: {e['new']} ({e['new_uid']}) appointed Vice President, Apache {display} (PMC chair)"
                     + (f", succeeding {e['old']} ({e['old_uid']})" if e["old"] else "")
                     + (" on the PMC's recommendation" if e["recommended"] else "") + f"; Special Order {e['order']}")
        dec = g.rec("Decision", tool, f"board:{e['at'][:10]}:{e['order']}", {
            "identifier": f"Special Order {e['order']}", "title": e["title"], "statement": statement,
            "rationale": e["text"], "status": "accepted", "decided_at": e["at"]}, scope)
        g.link("MADE_IN", dec, minutes, "field", at=e["at"])
        nxt = events[i + 1]["at"] if i + 1 < len(events) else None
        if e["new_uid"] in persons:
            if nxt:  # a past term: the next appointment ended it
                g.link("HOLDS_POSITION", persons[e["new_uid"]], chair, "field", kind="primary", valid_from=e["at"],
                       valid_to=nxt)
            else:
                terms[e["new_uid"]] = e["at"]
            if e["old_uid"] in persons:
                g.link("SUCCEEDS", persons[e["new_uid"]], persons[e["old_uid"]], "field", kind="predecessor", at=e["at"])
    g.notes["people: chair resolutions read from the board minutes"] = len(events)
    return terms
