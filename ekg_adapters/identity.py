"""Identity resolution: attach each tool's accounts and addresses to Persons.

The People pillar's ladder, strongest evidence first:
  1. the directory itself (admin, verified)            ASF roster, or an HR / IdP adapter
  2. a verified email from a tool that verifies it      Slack and Linear accounts, GitHub SAML SSO: joins the directory
     (verified_email, verified)                         Person who owns the address; with no directory,
                                                        a Person is made for the address
  3. an address the person is known to own              commits and mail sent from it
  4. one tool's record about another                    a merged PR whose merge commit came from a person's
                                                        address -> the PR author's GitHub account
  5. the same login in another tool on the same         Jira / Confluence username == directory id
     directory
  6. a unique exact display name                        credit only
Steps 1 and 2 are verified and grant access; the rest are inferred, which names the
person but grants nothing. Accounts that match nothing stay unresolved authors.
"""

import collections

from .common import Graph

VERIFIES_EMAIL = {"slack", "linear"}  # tools whose account email is confirmed by the tool


def resolve_identity(g: Graph):
    persons = g.ctx.setdefault("persons", {})          # key -> Person (directory uid, or "email:<address>")
    had_directory = bool(persons)
    stats = collections.Counter()
    done_github = set()  # (person, GitHub account) pairs already linked
    tools_of = collections.defaultdict(set)
    for key in persons:
        tools_of[key].add("directory")
    by_id = {r["id"]: r for r in g.records.values()}

    # who owns which address: the directory's own HAS_ALIAS links
    owner_of_addr = {}
    person_key = {p["id"]: k for k, p in persons.items()}
    for lk, _, _ in g.links.values():
        if lk["type"] == "HAS_ALIAS" and lk["a"] in person_key and by_id[lk["b"]]["type"] == "EmailAddress":
            owner_of_addr[lk["b"]] = person_key[lk["a"]]
    addr_rec = {r["props"]["address"]: r for r in g.records.values() if r["type"] == "EmailAddress"}

    def attach(key, acct, tool, how, conf, verified=False, method="inferred", evidence=None, link_method=None):
        g.link("HAS_ACCOUNT", persons[key], acct, method, conf, evidence=evidence,
               link_method=link_method or ("verified_email" if verified else "inferred"), verified=verified)
        stats[f"{tool}: {how}"] += 1
        tools_of[key].add(tool)

    # ---- step 2: verified emails (Slack, Linear) and GitHub SAML SSO identities
    saml = g.ctx.get("github_saml", set())
    for r in list(g.records.values()):
        if r["type"] != "Account" or r["props"].get("is_bot"):
            continue
        tool = r["props"]["source_tool"]
        if tool not in VERIFIES_EMAIL and r["id"] not in saml:
            continue
        for email in r["props"].get("emails") or []:
            addr = addr_rec.get(email)
            if addr is None:
                addr = addr_rec[email] = g.rec("EmailAddress", "email", email, {"address": email, "normalized_value": email,
                                               "display_name": r["props"].get("display_name")}, r["scope"])
            key = owner_of_addr.get(addr["id"])
            if key is None:  # no directory entry owns it: the address stands for a person of its own
                key = f"email:{email}"
                persons[key] = g.rec("Person", "email", email, {"full_name": r["props"].get("display_name"),
                                     "work_email": email, "status": "active", "person_source": "verified_email"}, r["scope"])
                g.link("HAS_ALIAS", persons[key], addr, "integration", role="primary", link_method="verified_email", verified=True)
                owner_of_addr[addr["id"]] = key
                person_key[persons[key]["id"]] = key
                stats["person made from a verified email (no directory entry)"] += 1
            g.link("ADDRESS_OF", addr, r, "integration")
            if r["id"] in saml:
                attach(key, r, tool, "SAML SSO identity", 1.0, verified=True, method="integration", link_method="gh_saml")
                done_github.add((key, r["id"]))
            else:
                attach(key, r, tool, "verified email", 1.0, verified=True, method="integration")
            break

    by_name = collections.defaultdict(set)
    for key, rec in persons.items():
        if rec["props"].get("full_name"):
            by_name[rec["props"]["full_name"].casefold()].add(key)
    unique_name = {n: next(iter(k)) for n, k in by_name.items() if len(k) == 1}

    # ---- step 3: other addresses, by a unique display name
    for addr in addr_rec.values():
        if addr["id"] in owner_of_addr:
            continue
        if (key := unique_name.get((addr["props"].get("display_name") or "").casefold())):
            g.link("HAS_ALIAS", persons[key], addr, "inferred", 0.7, link_method="inferred", verified=False)
            owner_of_addr[addr["id"]] = key
            stats["email: unique display name"] += 1

    authored_by = collections.defaultdict(set)
    for lk, _, _ in g.links.values():
        if lk["type"] == "AUTHORED_BY" and lk["props"].get("role") == "author" and by_id[lk["a"]]["type"] == "Commit":
            authored_by[lk["a"]].add(lk["b"])
        if lk["type"] in ("AUTHORED_BY", "FROM") and lk["b"] in owner_of_addr:
            tools_of[owner_of_addr[lk["b"]]].add("git" if by_id[lk["a"]]["type"] == "Commit" else "mail")

    # ---- step 4: GitHub, through merged PRs and noreply addresses
    pr_author = {lk["a"]: lk["b"] for lk, _, _ in g.links.values()
                 if lk["type"] == "AUTHORED_BY" and by_id[lk["a"]]["type"] == "PullRequest"}
    done = done_github
    for lk, _, _ in list(g.links.values()):  # attach() adds links
        if lk["type"] != "MERGED_AS" or lk["a"] not in pr_author:
            continue
        gh = by_id[pr_author[lk["a"]]]
        for addr_id in authored_by.get(lk["b"], ()):
            key = owner_of_addr.get(addr_id)
            if key and (key, gh["id"]) not in done:
                done.add((key, gh["id"]))
                attach(key, gh, "github", "merged PR's commit author address", 0.9, evidence=[lk["b"]])
    for lk, _, _ in list(g.links.values()):
        if lk["type"] == "ADDRESS_OF" and by_id[lk["b"]]["props"].get("tool") == "github" and lk["a"] in owner_of_addr:
            key, gh = owner_of_addr[lk["a"]], by_id[lk["b"]]
            if (key, gh["id"]) not in done:
                done.add((key, gh["id"]))
                attach(key, gh, "github", "noreply address owned by the person", 0.8, evidence=[lk["a"]])

    # ---- steps 5 and 6: Jira and Confluence (no verified email): same login, else a unique display name
    for tool in ("jira", "confluence"):
        for acct in g.ctx.get(f"{tool}_accounts", {}).values():
            login, name = acct["props"].get("login"), (acct["props"].get("display_name") or "").casefold()
            if login in persons:
                attach(login, acct, tool, "same login as the directory", 0.9)
            elif name in unique_name:
                attach(unique_name[name], acct, tool, "unique display name", 0.7)
            else:
                stats[f"{tool}: unresolved (no directory match)"] += 1

    # ---- step 7: no directory (public data): people inferred from commit and mail authorship
    mode = g.ctx.get("settings", {}).get("infer_people", "auto")
    if mode is True or (mode == "auto" and not had_directory):
        _infer_people(g, persons, owner_of_addr, by_id, attach, stats, tools_of)

    linked = {kb for lk, _, kb in g.links.values() if lk["type"] == "HAS_ACCOUNT"}
    for tool in ("github", "slack", "linear", "forum"):
        accts = [a for a in g.ctx.get(f"{tool}_accounts", {}).values() if not a["props"].get("is_bot")]
        if accts:
            stats[f"{tool}: unresolved (no person match)"] = sum(1 for a in accts if a["props"]["_key"] not in linked)

    reach = collections.Counter(len(t) for t in tools_of.values())
    return {"links": dict(stats), "persons": len(persons),
            "persons_by_tool_count": {f"{k} tool{'s' if k != 1 else ''}": v for k, v in sorted(reach.items())},
            "persons_in_tool": dict(collections.Counter(t for ts in tools_of.values() for t in ts))}


def _infer_people(g, persons, owner_of_addr, by_id, attach, stats, tools_of):
    """People from who wrote what, for data with no directory: inferred links only, which name a person but
    grant nothing. Addresses with the same full name (two words or more) are one person; any other address
    is a person of its own. A GitHub account joins its person through its noreply address."""
    used = {}  # address record id -> the tools it was used in
    for lk, _, _ in g.links.values():
        if lk["type"] in ("AUTHORED_BY", "FROM") and by_id[lk["b"]]["type"] == "EmailAddress":
            used.setdefault(lk["b"], set()).add("git" if by_id[lk["a"]]["type"] == "Commit" else "mail")
    names_account = collections.defaultdict(list)  # address -> the GitHub accounts it names (noreply)
    for lk, _, _ in g.links.values():
        if lk["type"] == "ADDRESS_OF" and by_id[lk["b"]]["props"].get("tool") == "github":
            names_account[lk["a"]].append(by_id[lk["b"]])
    groups = collections.defaultdict(list)
    for aid in used:
        a = by_id[aid]["props"]
        if aid in owner_of_addr or a.get("kind") == "automation" or a["address"] in ("noreply@github.com",):
            continue
        name = " ".join((a.get("display_name") or "").split())
        key = name.casefold() if len(name.split()) >= 2 else a["address"]
        groups[key].append(aid)
    for key, aids in groups.items():
        addrs = [by_id[i] for i in aids]
        plain = [x for x in addrs if "noreply" not in x["props"]["address"]] or addrs
        name = next((x["props"].get("display_name") for x in addrs if x["props"].get("display_name")), None)
        pkey = f"inferred:{key}"
        persons[pkey] = g.rec("Person", "email", pkey, {"full_name": name, "work_email": plain[0]["props"]["address"],
                              "status": "active", "person_source": "inferred_from_activity"}, plain[0]["scope"])
        stats["person inferred from commit and mail authorship"] += 1
        for x in addrs:
            g.link("HAS_ALIAS", persons[pkey], x, "inferred", 0.6, link_method="inferred", verified=False)
            owner_of_addr[x["id"]] = pkey
            tools_of[pkey].update(used[x["id"]])
        for aid in aids:  # the GitHub account a noreply address names
            for acct in names_account.get(aid, ()):
                attach(pkey, acct, "github", "noreply address of an inferred person", 0.7, evidence=[aid])
    # accounts with nothing but a display name (forum, issue authors): only a unique full-name match
    by_name = collections.defaultdict(set)
    for k, p in persons.items():
        if k.startswith("inferred:") and len((p["props"].get("full_name") or "").split()) >= 2:
            by_name[p["props"]["full_name"].casefold()].add(k)
    linked = {lk["b"] for lk, _, _ in g.links.values() if lk["type"] == "HAS_ACCOUNT"}
    owner = {lk["b"]: lk["a"] for lk, _, _ in g.links.values() if lk["type"] == "HAS_ACCOUNT"}
    key_of = {p["id"]: k for k, p in persons.items()}
    for tool in ("forum", "github"):
        for acct in g.ctx.get(f"{tool}_accounts", {}).values():
            hits = by_name.get((acct["props"].get("display_name") or "").casefold(), set())
            if acct["id"] not in linked and not acct["props"].get("is_bot") and len(hits) == 1:
                attach(next(iter(hits)), acct, tool, "unique full name of an inferred person", 0.5)
                linked.add(acct["id"])
                owner[acct["id"]] = persons[next(iter(hits))]["id"]
    # the same login on the forum and the code host (projects often ask for it): one person, when only
    # one account of each tool has that login and the other account already has a person
    logins = collections.defaultdict(list)
    for tool in ("forum", "github"):
        for acct in g.ctx.get(f"{tool}_accounts", {}).values():
            if acct["props"].get("login") and not acct["props"].get("is_bot"):
                logins[acct["props"]["login"].casefold()].append(acct)
    for accts in logins.values():
        if len(accts) != 2 or accts[0]["props"]["tool"] == accts[1]["props"]["tool"]:
            continue
        have = [a for a in accts if a["id"] in owner]
        if len(have) == 1:
            other = next(a for a in accts if a is not have[0])
            pkey = key_of.get(owner[have[0]["id"]])
            if pkey:
                attach(pkey, other, other["props"]["tool"], "same login on the forum and the code host", 0.5,
                       evidence=[have[0]["id"]])
