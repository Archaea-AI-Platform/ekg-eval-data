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

    linked = {kb for lk, _, kb in g.links.values() if lk["type"] == "HAS_ACCOUNT"}
    for tool in ("github", "slack", "linear"):
        accts = [a for a in g.ctx.get(f"{tool}_accounts", {}).values() if not a["props"].get("is_bot")]
        if accts:
            stats[f"{tool}: unresolved (no person match)"] = sum(1 for a in accts if a["props"]["_key"] not in linked)

    reach = collections.Counter(len(t) for t in tools_of.values())
    return {"links": dict(stats), "persons": len(persons),
            "persons_by_tool_count": {f"{k} tool{'s' if k != 1 else ''}": v for k, v in sorted(reach.items())},
            "persons_in_tool": dict(collections.Counter(t for ts in tools_of.values() for t in ts))}
