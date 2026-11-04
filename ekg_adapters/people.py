"""People pillar: the Apache directory (whimsy rosters) -> Person, Account, Group, Position, EmailAddress.

The roster is the tenant's own directory, so its account links are admin-sourced and
verified (People pillar trust rule). In production this adapter's place is taken by
the HR system and the identity provider.
"""

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
    for uid in committee.get("chair", {}):
        if uid in persons:  # the roster gives no start date for the chair
            g.link("HOLDS_POSITION", persons[uid], chair, "snapshot", kind="primary")
    g.ctx.update(persons=persons, asf_accounts=accounts,
                 person_by_name={(people.get(u, {}).get("name") or "").casefold(): u for u in persons})
    g.notes["people: persons with no title, manager or employer in the source"] = len(persons)
