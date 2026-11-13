"""People pillar: the Apache directory (whimsy rosters) -> Person, Account, Group, Position, EmailAddress.

The roster is the tenant's own directory, so its account links are admin-sourced and
verified (People pillar trust rule). In production this adapter's place is taken by
the HR system and the identity provider.
"""

import html
import re
from pathlib import Path

from .access import place
from .common import Graph, J, day, ldap_time


def map_people(g: Graph, d: Path, project: str):
    people = J(d / "people/public_ldap_people.json")["people"]
    committee = J(d / "people/committee-info.json")["committees"][project]
    ldap = J(d / "people/public_ldap_projects.json")["projects"][project]
    scope, _ = place(g, "asf", "org_directory", "apache")

    pmc = g.rec("Group", "asf", f"pmc:{project}", {"name": f"{committee.get('display_name', project)} PMC",
                "kind": "pmc"}, scope)
    committers = g.rec("Group", "asf", f"committers:{project}", {"name": f"{project} committers", "kind": "committers"}, scope)
    chair = g.rec("Position", "asf", f"chair:{project}", {"name": f"{project} PMC chair", "kind": "chair", "status": "filled"}, scope)

    persons, accounts = {}, {}
    for uid in sorted(set(ldap["members"]) | set(ldap["owners"]) | set(committee["roster"])):
        p = people.get(uid, {})
        persons[uid] = g.rec("Person", "asf", uid, {"full_name": p.get("name"), "status": "active",
                             "work_email": f"{uid}@apache.org", "person_source": "directory"}, scope)
        accounts[uid] = g.rec("Account", "asf", uid, {"login": uid, "display_name": p.get("name"), "status": "active",
                              "is_bot": False, "emails": [f"{uid}@apache.org"], "tool": "asf"}, scope)
        created = ldap_time(p.get("createTimestamp"))
        g.link("HAS_ACCOUNT", persons[uid], accounts[uid], "snapshot", link_method="admin", verified=True,
               valid_from=created)
        addr = g.rec("EmailAddress", "email", f"{uid}@apache.org", {"address": f"{uid}@apache.org",
                     "display_name": p.get("name"), "normalized_value": f"{uid}@apache.org", "kind": "work"}, scope)
        g.link("ADDRESS_OF", addr, accounts[uid], "snapshot", valid_from=created)
        g.link("HAS_ALIAS", persons[uid], addr, "snapshot", role="primary", link_method="tool_link", verified=True,
               valid_from=created)
        if uid in ldap["members"]:
            g.link("MEMBER_OF", accounts[uid], committers, "snapshot", role="member", basis="snapshot")
    for uid, v in committee["roster"].items():
        g.link("MEMBER_OF", accounts[uid], pmc, "snapshot", role="member", basis="snapshot",
               valid_from=day(v.get("date")), valid_from_precision="day")
    by_name = {(people.get(u, {}).get("name") or "").casefold(): u for u in persons}
    terms = _chair_terms(g, d, persons, by_name, chair, scope)  # dated terms from the board minutes, when present
    for uid in committee.get("chair", {}):
        if uid in persons:  # the roster says who holds it now; the minutes (when there) say since when
            g.link("HOLDS_POSITION", persons[uid], chair, "snapshot", kind="primary", valid_from=terms.get(uid))
    g.ctx.update(persons=persons, asf_accounts=accounts, person_by_name=by_name)
    g.notes["people: persons with no title, manager or employer in the source"] = len(persons)


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
CHAIR_ORDER = re.compile(r"Special Order (\w+), ((?:Change the|Establish the) [^,]+?), was approved")
APPOINTED = re.compile(r"that ([A-Z][\w.' -]+?)(?: \((\w+)\))? be (?:and hereby is )?appointed to the office of Vice President")
PREDECESSOR = re.compile(r"heretofore appointed ([A-Z][\w.' -]+?) \((\w+)\)\s+to the office of Vice President")
RECOMMENDED = re.compile(r"recommend ([A-Z][\w.' -]+?) \((\w+)\) as the successor")


def _chair_terms(g, d, persons, by_name, chair, scope) -> dict:
    """The board minutes (extra/people/board_minutes_*.html): each resolution that establishes the project or
    changes its chair becomes a dated Decision made in the minutes; each chair holds the seat from the day the board
    appointed them until the next appointment (HOLDS_POSITION valid_from / valid_to), and SUCCEEDS the one before.
    Returns {uid: the day the current term began}."""
    files = sorted((d / "extra" / "people").glob("board_minutes_*.html")) if (d / "extra" / "people").is_dir() else []
    if not files:
        return {}
    text = html.unescape(re.sub(r"<[^>]+>", " ", files[0].read_text(encoding="utf-8", errors="replace")))
    text = re.sub(r"\s+", " ", text)
    minutes = g.rec("Document", "asf", f"board-minutes:{files[0].stem}", {
        "title": "ASF board minutes: Apache Kafka resolutions and reports (whimsy.apache.org/board/minutes/Kafka.html)",
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
        statement = (f"{e['title']}: {e['new']} ({e['new_uid']}) appointed Vice President, Apache Kafka (PMC chair)"
                     + (f", succeeding {e['old']} ({e['old_uid']})" if e["old"] else "")
                     + (" on the PMC's recommendation" if e["recommended"] else "") + f"; Special Order {e['order']}")
        dec = g.rec("Decision", "asf", f"board:{e['at'][:10]}:{e['order']}", {
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
