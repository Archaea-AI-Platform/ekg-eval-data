"""Conversations pillar, a forum (Discourse) -> Thread (kind discussion), Comment (kind discussion), Account.

The schema needs no forum type: the spec already maps a GitHub Discussion to a Thread (kind
discussion) and its comments to Comments (kind discussion), and a forum topic is the same thing.

Reads forum/topics/*.json, one Discourse topic per file as its API returns it (/t/<id>.json):
  {id, title, created_at, category_id, tags, post_stream: {posts: [{id, username, name, created_at,
   cooked (HTML) or raw, post_number, reply_to_post_number}]}}
and forum/categories.json when present (for category names). A topic with no posts (a partial
scrape) is kept as a Thread and counted. Links: each post IN its topic, AUTHORED_BY its account, a
reply REPLY_TO the post it answers; ticket keys, GitHub PR and issue URLs, and #n references (when
the project's tickets are GitHub issues) become MENTIONS or LINKS_TO from the post.
"""

import json
import re
from pathlib import Path

from .access import place
from .common import Graph, ticket_rx, utc, xhtml_text

GH_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)")
GH_REF = re.compile(r"(?<![\w/&])#(\d{1,6})\b")


def map_forum(g: Graph, d: Path):
    s = g.ctx["settings"]
    root = d / "forum"
    scope, _ = place(g, "forum", "chat_channel", g.ctx.get("repo_name") or "forum", public=s.get("public", True))
    categories = {}
    if (root / "categories.json").exists():
        raw = json.loads((root / "categories.json").read_text(encoding="utf-8"))
        rows = raw.get("category_list", {}).get("categories", raw) if isinstance(raw, dict) else raw
        categories = {c["id"]: c.get("name") for c in rows if isinstance(c, dict) and "id" in c}
    key_rx, repo = ticket_rx(g), (g.ctx.get("repo_name") or "").lower()
    github_issues = s.get("issue_tracker") == "github"
    accounts = g.ctx.setdefault("forum_accounts", {})

    def account(username, name):
        if not username:
            return None
        if username not in accounts:
            accounts[username] = g.rec("Account", "forum", username, {"login": username, "display_name": name or None,
                                       "status": "active", "is_bot": username in ("system", "discobot"), "tool": "forum"}, scope)
        return accounts[username]

    for p in sorted((root / "topics").glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            tid = t["id"]
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
            g.notes["forum: unreadable topic file skipped"] += 1
            continue
        created = utc(t.get("created_at"))
        category = categories.get(t.get("category_id"))
        title = t.get("title") or t.get("fancy_title")
        thread = g.rec("Thread", "forum", tid, {"title": f"[{category}] {title}" if category else title, "kind": "discussion",
                       "created_at": created, "resolved": bool(t.get("accepted_answer")) or None}, scope)
        posts = (t.get("post_stream") or {}).get("posts") or []
        if not posts:
            g.notes["forum: topics without posts (partial scrape), kept as a Thread"] += 1
            continue
        by_number = {}
        for post in posts:
            at = utc(post.get("created_at"))
            text = post.get("raw") or xhtml_text(post.get("cooked") or "")
            cm = g.rec("Comment", "forum", post.get("id") or f"{tid}:{post.get('post_number')}", {
                "text": text, "kind": "discussion", "at": at, "edited_at": utc(post.get("updated_at"))
                if post.get("updated_at") and post.get("updated_at") != post.get("created_at") else None}, scope,
                acl_from=thread)
            by_number[post.get("post_number")] = cm
            g.link("IN", cm, thread, "field", valid_from=at)
            if (a := account(post.get("username"), post.get("name"))):
                g.link("AUTHORED_BY", cm, a, "field", role="author", at=at)
                g.link("PARTICIPATED_IN", a, thread, "field", role="started" if post.get("post_number") == 1 else "replied",
                       valid_from=at)
            if (parent := by_number.get(post.get("reply_to_post_number"))) is not None and parent is not cm:
                g.link("REPLY_TO", cm, parent, "field")
            for k in sorted(set(key_rx.findall(text))):
                g.ref(cm, "MENTIONS", "Ticket", k, "text_rule", 0.8, kind="key", at=at)
            for r, what, n in set(GH_URL.findall(post.get("cooked") or text)):
                if r.lower() == repo:
                    g.ref(cm, "LINKS_TO", "PullRequest" if what == "pull" else "GitHubRef", int(n), "field", 0.95,
                          kind="url", at=at)
            if github_issues:
                for n in sorted({int(x) for x in GH_REF.findall(text)}):
                    g.ref(cm, "MENTIONS", "GitHubRef", n, "text_rule", 0.6, kind="key", at=at)
        g.ctx.setdefault("forum_threads", {})[tid] = thread
    g.notes["forum: topics loaded"] = len(g.ctx.get("forum_threads", {}))
