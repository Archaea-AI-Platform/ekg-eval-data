"""Conversations pillar: Slack (workspace export, or Web API results laid out the same way) -> Workspace,
Account, App, Group (workspace members), Channel, Message, Thread, File, Activity, Alias.

Input layout (slack/), as Slack's export writes it:
  users.json        [User]          id, team_id, name, deleted, is_bot, is_restricted, is_ultra_restricted, profile{...}
  channels.json     public channels  id, name, created, creator, is_archived, members, topic, purpose, pins
  groups.json       private channels (same shape)              optional
  mpims.json        group DMs (same shape)                     optional
  dms.json          DMs: id, created, members                  optional
  <channel name>/YYYY-MM-DD.json    that day's messages (DMs: folder named by the DM id)

Access: a public channel is readable by every full member of the workspace (a Group)
and by its own members; a private channel, group DM or DM only by its members, from
their join date when the export shows one. Guests only see the channels they are in.

Each message is classified: an integration bot's notice (Linear, Jira, GitHub)
ANNOUNCES the item it names; a person's message MENTIONS the people, channels and
ticket keys in it and LINKS_TO the PRs and issues it links. Membership changes,
topic, purpose and renames are Activities; a former channel name is an Alias.
"""

import datetime as dt
import html
import re
from pathlib import Path

from .access import grant, place
from .common import Graph, J, from_dt, ticket_rx

USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
CHANNEL = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")  # G: older private channel ids
LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
ISSUE_URL = re.compile(r"https://linear\.app/[\w-]+/issue/([A-Z][A-Z0-9]+-\d+)|https://[\w.-]+/browse/([A-Z][A-Z0-9]+-\d+)")
UNTRUSTED = re.compile(r"\b(ignore|disregard) (all |the )?(previous|prior|above) (instructions|prompts)\b", re.I)
MEMBERSHIP = {"channel_join": "join", "group_join": "join", "channel_leave": "leave", "group_leave": "leave"}
CHANGES = {"channel_topic": "topic", "group_topic": "topic", "channel_purpose": "purpose", "group_purpose": "purpose",
           "channel_name": "name", "group_name": "name", "channel_archive": "archived", "channel_unarchive": "archived"}
NOTIFIERS = ("linear", "jira", "github")


def ts_utc(ts):
    return from_dt(dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc)) if ts else None


def plain(text, names):
    """Slack mrkdwn -> plain text: <@U1> -> @name, <#C1|eng> -> #eng, <url|label> -> label (url)."""
    t = USER.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), text or "")
    t = CHANNEL.sub(lambda m: "#" + (m.group(2) or m.group(1)), t)
    t = LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), t)
    return html.unescape(t)


def map_slack(g: Graph, d: Path):
    d = d / "slack"
    users = J(d / "users.json")
    team_id = g.ctx.get("slack_team_id") or next((u["team_id"] for u in users if u.get("team_id")), "workspace")
    ws_scope, ws_access = place(g, "slack", "workspace", team_id, public=False)
    ws = g.rec("Workspace", "slack", team_id, {"name": g.ctx.get("slack_workspace_name") or team_id, "handle": team_id,
               "kind": "chat"}, ws_scope)
    members_group = g.rec("Group", "slack", f"{team_id}:members", {"name": "workspace members", "kind": "workspace_members",
                          "tool": "slack"}, ws_scope)
    grant(g, members_group, ws_access, via="base_permission")

    accounts = g.ctx.setdefault("slack_accounts", {})
    names = {}
    for u in users:
        p = u.get("profile", {})
        guest = bool(u.get("is_restricted") or u.get("is_ultra_restricted"))
        bot = bool(u.get("is_bot") or u["id"] == "USLACKBOT")
        accounts[u["id"]] = a = g.rec("Account", "slack", u["id"], {
            "login": u.get("name"), "display_name": p.get("display_name") or p.get("real_name") or u.get("real_name"),
            "emails": [p["email"].lower()] if p.get("email") else None,
            "status": "deactivated" if u.get("deleted") else "active", "is_bot": bot, "is_guest": guest,
            "guest_kind": ("single_channel" if u.get("is_ultra_restricted") else "multi_channel") if guest else None,
            "tool": "slack", "workspace_id": team_id}, ws_scope)
        names[u["id"]] = a["props"].get("display_name") or u.get("name")
        g.link("MEMBER_OF", a, ws, "snapshot", basis="snapshot",
               role="guest" if guest else "owner" if u.get("is_owner") else "maintainer" if u.get("is_admin") else "member")
        if not (guest or bot or u.get("deleted")):
            g.link("MEMBER_OF", a, members_group, "snapshot", role="member", basis="snapshot")
        if bot and p.get("api_app_id"):
            app = g.rec("App", "slack", p["api_app_id"], {"name": p.get("real_name") or u.get("name")}, ws_scope)
            g.link("BOT_OF", a, app, "snapshot")

    def account(uid, name=None):
        if not uid:
            return None
        if uid not in accounts:  # someone not in users.json: a Slack Connect user from another workspace
            accounts[uid] = g.rec("Account", "slack", uid, {"login": name, "tool": "slack", "is_guest": True}, ws_scope)
            g.notes["slack: authors not in users.json (external, Slack Connect)"] += 1
        return accounts[uid]

    channels = g.ctx.setdefault("slack_channels", {})
    convs = []
    for file, kind, vis in (("channels.json", "channel", "public"), ("groups.json", "channel", "private"),
                            ("mpims.json", "group_dm", "private"), ("dms.json", "dm", "private")):
        if (d / file).exists():
            convs += [(c, kind, vis) for c in J(d / file)]
    key_rx = ticket_rx(g)
    for c, kind, vis in convs:
        _conversation(g, d, c, kind, vis, team_id, ws, members_group, account, names, channels, key_rx)


def _conversation(g, d, c, kind, vis, team_id, ws, members_group, account, names, channels, key_rx):
    cid = c["id"]
    scope, access = place(g, "slack", "chat_channel", cid, public=False)
    created = ts_utc(c.get("created"))
    name = c.get("name") or "dm:" + ",".join(sorted(c.get("members", [])))
    chan = g.rec("Channel", "slack", cid, {"name": name, "kind": kind, "visibility": vis,
                 "purpose": (c.get("purpose") or {}).get("value") or None, "topic": (c.get("topic") or {}).get("value") or None,
                 "archived": c.get("is_archived"), "shared_externally": bool(c.get("is_ext_shared") or c.get("is_shared")) or None,
                 "created_at": created, "ingest_mode": "export"}, scope)
    channels[cid] = chan
    g.link("IN", chan, ws, "field", valid_from=created)
    if vis == "public":
        grant(g, members_group, access, via="base_permission", valid_from=created)
    if c.get("creator"):
        g.link("CREATED_BY", chan, account(c["creator"]), "field", role="creator", at=created)

    folder = d / (c.get("name") if kind != "dm" else cid)
    msgs = [m for f in sorted(folder.glob("*.json")) for m in J(f)] if folder.exists() else []
    msgs.sort(key=lambda m: float(m["ts"]))
    joined, spans = {}, []  # membership from join/leave events
    for m in msgs:
        at, st = ts_utc(m["ts"]), m.get("subtype")
        if st in MEMBERSHIP:
            uid = m.get("user")
            act = g.rec("Activity", "slack", f"{cid}:{m['ts']}", {"verb": MEMBERSHIP[st], "at": at}, scope, acl_from=chan)
            g.link("ACTS_ON", act, chan, "event")
            if uid:
                g.link("ACTED_BY", act, account(uid), "event", role="actor", action="perform", at=at)
                if MEMBERSHIP[st] == "join":
                    joined.setdefault(uid, at)
                elif uid in joined:
                    spans.append((uid, joined.pop(uid), at))
            continue
        if st in CHANGES:
            _channel_change(g, chan, cid, m, at, CHANGES[st], scope, account)
            continue
        _message(g, chan, cid, m, at, scope, account, names, key_rx)

    # membership: current members (dated from their join when seen) and past members (closed spans)
    for uid in c.get("members", []):
        start = joined.pop(uid, None)
        g.link("MEMBER_OF", account(uid), chan, "event" if start else "snapshot", role="member",
               basis="event" if start else "snapshot", valid_from=start)
        grant(g, account(uid), access, via="direct", method="event" if start else "snapshot", valid_from=start)
    for uid, start in joined.items():  # joined, never left, but not in the member list: left before export
        spans.append((uid, start, None))
    for uid, start, end in spans:
        if end and end <= start:
            continue
        g.link("MEMBER_OF", account(uid), chan, "event", role="member", basis="event", valid_from=start, valid_to=end)
        grant(g, account(uid), access, via="direct", method="event", valid_from=start, valid_to=end)
    for pin in c.get("pins", []) or []:
        msg = g.ctx.setdefault("messages", {}).get(f"{cid}:{pin.get('id')}")
        if msg and pin.get("user"):
            g.link("PINNED_BY", msg, account(pin["user"]), "field", role="pinner", at=ts_utc(pin.get("created")))


def _channel_change(g, chan, cid, m, at, field, scope, account):
    act = g.rec("Activity", "slack", f"{cid}:{m['ts']}", {"verb": f"change_{field}", "at": at}, scope, acl_from=chan)
    g.link("ACTS_ON", act, chan, "event")
    if m.get("user"):
        g.link("ACTED_BY", act, account(m["user"]), "event", role="actor", action="perform", at=at)
    new = m.get("name") if field == "name" else m.get(field) if field in ("topic", "purpose") else m.get("subtype")
    fc = g.rec("FieldChange", "slack", f"{cid}:{m['ts']}:0", {"field": field, "at": at, "from_value": m.get("old_name"),
               "to_value": new}, scope, acl_from=chan)
    g.link("PART_OF", fc, act, "event")
    if field == "name" and m.get("old_name"):
        alias = g.rec("Alias", "slack", f"{cid}:{m['old_name']}", {"value": m["old_name"], "kind": "channel_name",
                      "normalized_value": m["old_name"].lower(), "former": True, "valid_to": at}, scope, acl_from=chan)
        g.link("HAS_ALIAS", chan, alias, "event", role="former", valid_to=at)


def _message(g, chan, cid, m, at, scope, account, names, key_rx):
    raw = m.get("text") or ""
    text = plain(raw, names)
    st = m.get("subtype")
    bot = m.get("bot_profile") or {}
    author = account(m.get("user")) if m.get("user") else None
    if author is None and m.get("bot_id"):  # an integration posting without a user
        author = g.rec("Account", "slack", m["bot_id"], {"login": bot.get("name"), "display_name": bot.get("name"),
                       "is_bot": True, "tool": "slack", "status": "active"}, scope)
        if bot.get("app_id"):
            app = g.rec("App", "slack", bot["app_id"], {"name": bot.get("name")}, scope)
            g.link("BOT_OF", author, app, "snapshot")
    msg = g.rec("Message", "slack", f"{cid}:{m['ts']}", {"at": at, "text": text, "kind": st or "message",
                "edited_at": ts_utc((m.get("edited") or {}).get("ts")), "deleted": True if st == "tombstone" else None,
                "contains_untrusted_instructions": True if UNTRUSTED.search(text) else None}, scope, acl_from=chan)
    g.ctx.setdefault("messages", {})[f"{cid}:{m['ts']}"] = msg
    g.link("IN", msg, chan, "field", valid_from=at)
    if m.get("thread_ts"):
        tkey = f"{cid}:{m['thread_ts']}"
        threads = g.ctx.setdefault("chat_threads", {})
        if tkey not in threads:
            threads[tkey] = g.rec("Thread", "slack", tkey, {"kind": "chat", "thread_ts": m["thread_ts"],
                                  "created_at": ts_utc(m["thread_ts"])}, scope, acl_from=chan)
            g.link("IN", threads[tkey], chan, "field", valid_from=ts_utc(m["thread_ts"]))
        g.link("IN", msg, threads[tkey], "field", valid_from=at)
    if author:
        g.link("AUTHORED_BY", msg, author, "field", role="author", at=at)
    for r in m.get("reactions", []) or []:
        for uid in r.get("users", []):
            g.link("REACTED", account(uid), msg, "field", emoji=r["name"], kind="reaction")
    for f in m.get("files", []) or []:
        if f.get("mode") == "tombstone":
            continue
        fr = g.rec("File", "slack", f["id"], {"name": f.get("name"), "title": f.get("title"), "mime_type": f.get("mimetype"),
                   "kind": "upload", "created_at": ts_utc(f.get("created"))}, scope)
        g.link("SHARED_IN", fr, chan, "field", valid_from=at)
        g.link("HAS_ATTACHMENT", msg, fr, "field", valid_from=at)
        if f.get("user"):
            g.link("CREATED_BY", fr, account(f["user"]), "field", role="uploader", how="upload", at=ts_utc(f.get("created")))
    # references: people, channels, links, ticket keys; integration bots announce instead of mention
    notifier = _notifier(author, bot)
    for uid in set(USER.findall(raw)):
        g.link("MENTIONS", msg, account(uid), "field", kind="mention", at=at)
    for other, _ in set(CHANNEL.findall(raw)):
        g.ref(msg, "MENTIONS", "Channel", other, "field", kind="channel", at=at)
    url_keys = set()
    for url, _ in LINK.findall(raw):
        pr = PR_URL.match(url)
        if pr and pr.group(1).lower() == g.ctx.get("repo_name", "").lower():
            g.ref(msg, "ANNOUNCES" if notifier == "github" else "LINKS_TO", "PullRequest", int(pr.group(2)),
                  "integration" if notifier else "field", 0.95, stub=True, kind="notify" if notifier else "url", at=at)
        issue = ISSUE_URL.match(url)
        if issue:
            key = issue.group(1) or issue.group(2)
            url_keys.add(key)
            g.ref(msg, "ANNOUNCES" if notifier else "LINKS_TO", "Ticket", key, "integration" if notifier else "field",
                  0.95, stub=True, kind="notify" if notifier else "url", at=at)
    for key in set(key_rx.findall(text)) - url_keys:
        if notifier:
            g.ref(msg, "ANNOUNCES", "Ticket", key, "integration", stub=True, kind="notify", at=at)
        else:
            g.ref(msg, "MENTIONS", "Ticket", key, "text_rule", 0.8, kind="key", at=at)
    if notifier:
        g.notes[f"slack: {notifier} integration posts (ANNOUNCES what they name)"] += 1


def _notifier(author, bot):
    """'linear', 'jira' or 'github' when the author is that tool's integration bot, else None."""
    if author is None or not author["props"].get("is_bot"):
        return None
    name = (bot.get("name") or author["props"].get("display_name") or author["props"].get("login") or "").lower()
    return next((n for n in NOTIFIERS if n in name), None)
