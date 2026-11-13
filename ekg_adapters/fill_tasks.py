"""What the adapters left unfilled, as questions an LLM settles by accepting or rejecting.

Each builder reads the finished graph, finds one kind of gap and turns every instance into a Task:
the subject, the candidates code found for it, and the source text the answer must quote. The LLM
never invents a target: it picks one candidate or rejects them all. A gap with no candidate is
counted with its reason, never asked.

  pr_ticket        PR with no ticket                 -> IMPLEMENTS PR -> Ticket
  commit_pr        commit with no PR                 -> MERGED_AS PR -> Commit
  ticket_fix       fixed ticket with no PR or commit -> IMPLEMENTS PR|Commit -> Ticket
  email_topic      mail thread linked to nothing     -> DISCUSSES email -> Ticket, MADE_IN Decision -> email
  identity         account with no person            -> HAS_ACCOUNT Person -> Account (inferred: names, grants nothing)
  decision_status  decision page with no status      -> Decision.status, cited to the page
  knowledge        conversation (mail, chat, forum,  -> Knowledge / Decision / Guideline / Requirement / ActionItem / Question
                   PR and ticket comments)              records (extract, then judge), with who said, asked, decided or took it on
  doc_knowledge    stored documents, recent wiki      -> the same records, from one document's text
"""

from __future__ import annotations

import collections
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field

TEXT_CAP = 1500        # characters of one subject source shown to the model
DOC_TEXT_CAP = 9000    # a document, read whole (one source)
CANDIDATE_CAP = 600    # characters of one candidate
PROMPT_CAP = 14000     # everything together; under query-master's LLM_PROMPT_CHAR_BUDGET (20,000)
MAX_CANDIDATES = 5
MAX_SOURCES = 8

NOT_FIXED = {"duplicate", "won't fix", "invalid", "not a bug", "not a problem", "cannot reproduce", "abandoned",
             "later", "information provided", "won't do", "incomplete", "auto closed", "workaround"}
NO_TICKET = re.compile(r"^\s*\[?(minor|hotfix|trivial)\]?\b", re.I)  # the project's own "needs no ticket" markers
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
PR_REF = re.compile(r"(?:/pull/|(?<![\w/])#)(\d{1,6})\b")
DECISION_STATUS = ["proposed", "accepted", "superseded", "reversed", "reinstated", "disputed"]
GUIDELINE_STRENGTH = ["must", "should", "avoid"]
KNOWLEDGE_KINDS = ["how_to", "workaround", "gotcha", "norm", "folklore", "rationale", "tip", "warning", "fact"]


@dataclass
class Item:
    """A piece of text the model reads, under a label it answers with."""
    label: str
    id: str | None      # the record it came from (None for a value choice)
    title: str
    text: str = ""
    type: str | None = None


@dataclass
class Task:
    kind: str
    subject: str                    # record id
    question: str
    rule: str                       # what makes an answer acceptable
    sources: list[Item]             # the subject's own text
    candidates: list[Item]          # records or values to choose among ([] for knowledge)
    write: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Changes when the subject, its candidates or their text change, so a stale verdict is asked again."""
        h = hashlib.sha1(f"{self.kind}|{self.subject}".encode())
        for it in self.sources + self.candidates:
            h.update(f"|{it.label}:{it.id}:{it.title}:{it.text}".encode())
        return "t_" + h.hexdigest()[:20]

    def labels(self) -> list[str]:
        return [c.label for c in self.candidates]


class View:
    """Read-only lookups over the written graph."""

    def __init__(self, records, links):
        self.rec = {r["id"]: r for r in records}
        self.out, self.inn = collections.defaultdict(list), collections.defaultdict(list)
        for lk in links:
            self.out[lk["a"]].append(lk)
            self.inn[lk["b"]].append(lk)
        self.by_type = collections.defaultdict(list)
        for r in self.rec.values():
            self.by_type[r["type"]].append(r)

    def typed(self, t, real=True):
        """Records of a type; real=True leaves out stubs (records named by a reference, never downloaded)."""
        return [r for r in self.by_type[t] if not real or not _stub(r)]

    def ends(self, rid, ltypes, types=None, direction="both"):
        found = []
        if direction in ("out", "both"):
            found += [self.rec[lk["b"]] for lk in self.out[rid] if lk["type"] in ltypes]
        if direction in ("in", "both"):
            found += [self.rec[lk["a"]] for lk in self.inn[rid] if lk["type"] in ltypes]
        return [r for r in found if types is None or r["type"] in types]

    def description(self, rid):
        cvs = [(lk["props"].get("order") or 0, self.rec[lk["a"]]["props"].get("text")) for lk in self.inn[rid]
               if lk["type"] == "VERSION_OF" and self.rec[lk["a"]]["type"] == "ContentVersion"]
        cvs = [c for c in cvs if c[1]]
        return max(cvs, key=lambda c: c[0])[1] if cvs else ""

    def comments(self, rid):
        cs = [self.rec[lk["a"]] for lk in self.inn[rid]
              if lk["type"] == "ON" and self.rec[lk["a"]]["type"] in ("Comment", "Review")]
        return sorted([c for c in cs if c["props"].get("text") and not self.is_bot(c["id"])],
                      key=lambda c: c["props"].get("at") or "")

    def author(self, rid):
        for r in self.ends(rid, {"AUTHORED_BY", "FROM", "CREATED_BY"}, direction="out"):
            p = r["props"]
            return p.get("display_name") or p.get("login") or p.get("address") or "?"
        return "?"

    def is_bot(self, rid):
        for r in self.ends(rid, {"AUTHORED_BY", "FROM"}, direction="out"):
            p = r["props"]
            if p.get("is_bot") or p.get("kind") == "automation" or (p.get("login") or "").endswith("[bot]"):
                return True
        return False


def _stub(r):
    p = r["props"]
    return (r["type"] in ("Ticket", "PullRequest") and "title" not in p) or p.get("_stub")


# ---------------------------------------------------------------- candidate search

STOP = set("""the and for with from into that this when not are was use add adds added fix fixes fixed make
remove update support minor should can will which via its also more one new all when then than has have
apache kafka flink jira github pull request issue""".split())


def tokens(text):
    return {w for w in re.findall(r"[a-z][a-z0-9_]{2,}", (text or "").lower()) if w not in STOP}


class Similar:
    """Titles that share rare words with a text. It only proposes candidates; the LLM decides."""

    def __init__(self, entries):
        self.entries = [(rid, tokens(text)) for rid, text in entries]
        df = collections.Counter(t for _, ts in self.entries for t in ts)
        n = max(len(self.entries), 1)
        self.idf = {t: math.log(n / c) for t, c in df.items()}
        self.index = collections.defaultdict(list)
        for i, (_, ts) in enumerate(self.entries):
            for t in ts:
                self.index[t].append(i)

    def top(self, text, n=3, min_shared=2, exclude=()):
        q = tokens(text)
        scores, shared = collections.Counter(), collections.Counter()
        for t in q:
            for i in self.index.get(t, ()):
                scores[i] += self.idf[t]
                shared[i] += 1
        ranked = sorted(scores, key=lambda i: (-round(scores[i], 6), i))  # ties in input order: same candidates every run
        best = [i for i in ranked if shared[i] >= min_shared and self.entries[i][0] not in exclude]
        return [self.entries[i][0] for i in best[:n]]


def _clip(text, cap):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text if len(text) <= cap else text[:cap].rsplit(" ", 1)[0] + " …"


def _first_line(text):
    return (text or "").strip().split("\n", 1)[0]


# ---------------------------------------------------------------- items

def ticket_item(v, t, label, cap=CANDIDATE_CAP):
    p = t["props"]
    return Item(label, t["id"], f"Ticket {p.get('identifier')}: {p.get('title') or ''}", _clip(v.description(t["id"]), cap), "Ticket")


def pr_item(v, pr, label, cap=CANDIDATE_CAP):
    p = pr["props"]
    title = f"Pull request #{p.get('number')}: {p.get('title') or ''} (branch {p.get('head_ref') or '?'}, {p.get('state') or '?'})"
    return Item(label, pr["id"], title, _clip(v.description(pr["id"]), cap), "PullRequest")


def commit_item(v, c, label, cap=CANDIDATE_CAP):
    p = c["props"]
    return Item(label, c["id"], f"Commit {p['sha'][:10]}: {_first_line(p.get('message'))}", _clip(p.get("message"), cap), "Commit")


def comment_items(v, rid, start, n=MAX_SOURCES - 1, cap=TEXT_CAP // 2):
    return [Item(f"S{start + i}", c["id"], f"Comment by {v.author(c['id'])} ({(c['props'].get('at') or '')[:10]})",
                 _clip(c["props"]["text"], cap), c["type"]) for i, c in enumerate(v.comments(rid)[:n])]


def decision_item(v, d, label):
    return Item(label, d["id"], f"Proposal: {d['props'].get('statement')}", "", "Decision")


def _ticket_keys(v):
    prefixes = {t["props"]["identifier"].split("-")[0] for t in v.typed("Ticket", real=False) if t["props"].get("identifier")}
    if not prefixes:
        return re.compile(r"(?!x)x")
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True)) + r")-\d+\b")


def _ordered(seq):
    return list(dict.fromkeys(seq))


# ---------------------------------------------------------------- builders: each returns (tasks, skipped counter)

def pr_ticket(v):
    tickets = {t["props"]["identifier"]: t for t in v.typed("Ticket")}
    sim = Similar([(t["id"], t["props"].get("title")) for t in tickets.values()])
    key_rx, tasks, skipped = _ticket_keys(v), [], collections.Counter()
    for pr in v.typed("PullRequest"):
        if v.ends(pr["id"], {"IMPLEMENTS", "LINKS_TO"}, {"Ticket"}):
            continue
        if v.is_bot(pr["id"]):
            skipped["bot PR (no ticket expected)"] += 1
            continue
        if NO_TICKET.match(pr["props"].get("title") or ""):
            skipped["marked as needing no ticket (MINOR / hotfix)"] += 1
            continue
        p, desc = pr["props"], v.description(pr["id"])
        sources = [pr_item(v, pr, "S1", TEXT_CAP)] + comment_items(v, pr["id"], 2)
        text = " ".join([p.get("title") or "", p.get("head_ref") or "", desc] + [s.text for s in sources[1:]])
        named = _ordered(key_rx.findall(text))
        cands = [tickets[k] for k in named if k in tickets]
        if not cands and named:
            skipped["names a ticket outside the download"] += 1
        cands += [v.rec[i] for i in sim.top(f"{p.get('title')} {desc[:400]}", exclude={c["id"] for c in cands})]
        if not cands:
            skipped["no candidate ticket"] += 1
            continue
        tasks.append(Task("pr_ticket", pr["id"], f"Which ticket, if any, is pull request #{p.get('number')} the work for?",
                          "the text shows this PR fixes or implements that ticket. A ticket only mentioned, a follow-up, "
                          "or one that merely shares words with the PR is a reject.",
                          sources, [ticket_item(v, c, f"C{i + 1}") for i, c in enumerate(cands[:MAX_CANDIDATES])],
                          {"link": "IMPLEMENTS", "from": "subject", "own": {"link_method": "llm", "valid_from": p.get("created_at")}}))
    return tasks, skipped


def commit_pr(v):
    prs = v.typed("PullRequest")
    sim = Similar([(pr["id"], pr["props"].get("title")) for pr in prs])
    by_ticket = collections.defaultdict(set)
    for pr in prs:
        for t in v.ends(pr["id"], {"IMPLEMENTS", "LINKS_TO"}, {"Ticket"}):
            by_ticket[t["props"].get("identifier")].add(pr["id"])
    key_rx, tasks, skipped = _ticket_keys(v), [], collections.Counter()
    for c in v.typed("Commit"):
        if any(lk["type"] == "MERGED_AS" for lk in v.inn[c["id"]]):  # stub PRs count: the link exists
            continue
        # only commits on the default branch land a PR; a PR's own branch commits (CONTAINS) and release-branch
        # commits are not merge results, so asking which PR merged them is the wrong question
        if not c["props"].get("on_default_branch") or any(lk["type"] == "CONTAINS" for lk in v.inn[c["id"]]):
            skipped["not a default-branch commit (a PR's own commit, or another branch)"] += 1
            continue
        msg = c["props"].get("message") or ""
        cands = _ordered(i for k in key_rx.findall(msg) for i in sorted(by_ticket.get(k, ())))
        cands += sim.top(_first_line(msg), exclude=set(cands))
        if not cands:
            skipped["no candidate PR"] += 1
            continue
        tasks.append(Task("commit_pr", c["id"], f"Which pull request, if any, did commit {c['props']['sha'][:10]} land?",
                          "the commit is that PR's change (same ticket and same change, or the message names the PR). "
                          "A PR about a different change, or another branch's backport, is a reject.",
                          [commit_item(v, c, "S1", TEXT_CAP)],
                          [pr_item(v, v.rec[i], f"C{n + 1}") for n, i in enumerate(cands[:MAX_CANDIDATES])],
                          {"link": "MERGED_AS", "from": "choice", "own": {"valid_from": c["props"].get("committed_at")}}))
    return tasks, skipped


def ticket_fix(v):
    prs = {pr["props"]["number"]: pr for pr in v.typed("PullRequest")}
    commits = v.typed("Commit")
    sim = Similar([(pr["id"], pr["props"].get("title")) for pr in prs.values()] +
                  [(c["id"], _first_line(c["props"].get("message"))) for c in commits])
    tasks, skipped = [], collections.Counter()
    for t in v.typed("Ticket"):
        p = t["props"]
        resolution = p.get("resolution") or ("Fixed" if p.get("kind") == "issue" and p.get("completed_at") else None)
        if not resolution or resolution.lower() in NOT_FIXED:
            continue
        if v.ends(t["id"], {"IMPLEMENTS", "LINKS_TO"}, {"PullRequest", "Commit"}) or any(
                v.ends(c["id"], {"LINKS_TO", "MENTIONS"}, {"PullRequest", "Commit"}, "out") for c in v.comments(t["id"])):
            continue
        sources = [ticket_item(v, t, "S1", TEXT_CAP)] + comment_items(v, t["id"], 2)
        text = " ".join(s.text for s in sources)
        shas = {s for s in SHA.findall(text) if re.search(r"[a-f]", s) and re.search(r"\d", s)}
        cands = [c["id"] for c in commits if any(c["props"]["sha"].startswith(s) for s in shas)]
        cands += [prs[int(n)]["id"] for n in _ordered(PR_REF.findall(text)) if int(n) in prs]
        cands = _ordered(cands)
        cands += sim.top(f"{p.get('title')} {v.description(t['id'])[:400]}", exclude=set(cands))
        if not cands:
            skipped["no candidate PR or commit"] += 1
            continue
        items = [(pr_item if v.rec[i]["type"] == "PullRequest" else commit_item)(v, v.rec[i], f"C{n + 1}")
                 for n, i in enumerate(cands[:MAX_CANDIDATES])]
        tasks.append(Task("ticket_fix", t["id"], f"Ticket {p.get('identifier')} is resolved as {resolution}. "
                          "Which pull request or commit, if any, made that fix?",
                          "the text shows that PR or commit is the change that fixed this ticket (a comment names it, "
                          "or it is plainly the same change). Same words alone is a reject.",
                          sources, items, {"link": "IMPLEMENTS", "from": "choice",
                                           "own": {"link_method": "llm", "valid_from": p.get("resolved_at")}}))
    return tasks, skipped


def email_topic(v):
    tickets, decisions = v.typed("Ticket"), v.typed("Decision")
    sim = Similar([(t["id"], t["props"].get("title")) for t in tickets] + [(d["id"], d["props"].get("statement")) for d in decisions])
    tasks, skipped = [], collections.Counter()
    for th in v.typed("EmailThread"):
        msgs = sorted(v.ends(th["id"], {"IN"}, {"EmailMessage"}, "in"), key=lambda m: m["props"].get("sent_at") or "")
        if not msgs or all(v.is_bot(m["id"]) for m in msgs):
            continue
        if any(v.ends(m["id"], {"DISCUSSES", "ANNOUNCES", "MENTIONS", "MADE_IN"}, {"Ticket", "Decision", "PullRequest"}) for m in msgs):
            continue
        human = [m for m in msgs if not v.is_bot(m["id"])][:3]
        sources = [Item(f"S{i + 1}", m["id"], f"Email from {v.author(m['id'])}: {m['props'].get('subject')}",
                        _clip(m["props"].get("text"), TEXT_CAP // (1 if i == 0 else 2)), "EmailMessage") for i, m in enumerate(human)]
        cands = sim.top(f"{th['props'].get('normalized_subject')} {sources[0].text[:400]}", n=4)
        if not cands:
            skipped["no candidate ticket or proposal"] += 1
            continue
        items = [(ticket_item(v, v.rec[i], f"C{n + 1}") if v.rec[i]["type"] == "Ticket" else decision_item(v, v.rec[i], f"C{n + 1}"))
                 for n, i in enumerate(cands)]
        tasks.append(Task("email_topic", human[0]["id"], "Which ticket or proposal, if any, is this mail thread about?",
                          "the thread discusses that ticket's problem or change, or is the discussion or vote of that proposal. "
                          "A thread that only shares a few words with it is a reject.",
                          sources, items, {"by_type": {"Ticket": {"link": "DISCUSSES", "from": "subject", "own": {"kind": "discuss"}},
                                                       "Decision": {"link": "MADE_IN", "from": "choice", "own": {}}},
                                           "at": human[0]["props"].get("sent_at")}))
    return tasks, skipped


def _name_tokens(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z]+", s) if len(w) > 1]


def identity(v):
    persons = v.typed("Person")
    by_token = collections.defaultdict(set)
    for p in persons:
        for w in _name_tokens(p["props"].get("full_name")):
            by_token[w].add(p["id"])
    has = {lk["b"] for p in persons for lk in v.out[p["id"]] if lk["type"] == "HAS_ACCOUNT"}
    tasks, skipped = [], collections.Counter()
    for a in v.typed("Account"):
        ap = a["props"]
        if ap.get("tool") not in ("jira", "confluence", "github", "slack", "linear") or a["id"] in has or ap.get("is_bot"):
            continue
        login, words = (ap.get("login") or "").lower(), _name_tokens(ap.get("display_name"))
        scored = collections.Counter()
        for w in words:
            for pid in sorted(by_token.get(w, ())):
                scored[pid] += 1
        for p in persons:
            pp, pw = p["props"], _name_tokens(p["props"].get("full_name"))
            local = (pp.get("work_email") or "").split("@")[0].lower()
            if login and (login in (p["props"].get("native_id", "").lower(), local) or
                          (len(pw) >= 2 and login in (pw[0][0] + pw[-1], pw[-1] + pw[0][0], pw[0] + pw[-1]))):
                scored[p["id"]] += 3
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        cands = [pid for pid, s in ranked if s >= (2 if len(words) >= 2 else 1) or s >= 3]
        if not cands:
            skipped[f"{ap.get('tool')}: no candidate person"] += 1
            continue
        src = Item("S1", a["id"], f"{ap.get('tool')} account {ap.get('login')}",
                   f"display name: {ap.get('display_name') or '-'}\nemails: {', '.join(ap.get('emails') or []) or '-'}", "Account")
        items = []
        for n, pid in enumerate(cands):
            pp = v.rec[pid]["props"]
            known = [f"{r['props'].get('tool')}:{r['props'].get('login')} ({r['props'].get('display_name') or '-'})"
                     for r in v.ends(pid, {"HAS_ACCOUNT"}, {"Account"}, "out")][:6]
            items.append(Item(f"C{n + 1}", pid, f"Person {pp.get('full_name')}",
                              f"directory id: {pp.get('native_id')}\nemail: {pp.get('work_email') or '-'}\n"
                              f"accounts already known: {', '.join(known) or '-'}", "Person"))
        tasks.append(Task("identity", a["id"], f"Which person, if any, owns {ap.get('tool')} account '{ap.get('login')}'?",
                          "the name and login leave no real doubt it is the same person. Two people can share a "
                          "first name or a surname; a partial match with nothing else behind it is a reject.",
                          [src], items, {"link": "HAS_ACCOUNT", "from": "choice",
                                         "own": {"link_method": "inferred", "verified": False}}))
    return tasks, skipped


def decision_status(v):
    tasks, skipped = [], collections.Counter()
    for d in v.typed("Decision"):
        if d["props"].get("status"):
            continue
        docs = v.ends(d["id"], {"MADE_IN"}, {"Document"}, "out")
        text = v.description(docs[0]["id"]) if docs else ""
        if not text:
            skipped["no page text"] += 1
            continue
        tasks.append(Task("decision_status", d["id"], f"What status does this page give the proposal '{d['props'].get('statement')}'?",
                          "the page states the status (a 'Current state' line, a vote result, 'accepted', 'adopted', "
                          "'under discussion', 'superseded by' ...). If the page says rejected, discarded, withdrawn or "
                          "dormant, or says nothing, reject: the schema has no value for those.",
                          [Item("S1", docs[0]["id"], f"Page: {docs[0]['props'].get('title')}", _clip(text, 2500), "Document")],
                          [Item(s, None, s) for s in DECISION_STATUS], {"field": "status"}))
    return tasks, skipped


def _linked_work(v, msgs):
    """The one ticket or PR a conversation is already linked to (by the adapters or the linker), else None."""
    found = []
    for m in msgs:
        found += v.ends(m["id"], {"DISCUSSES", "ANNOUNCES", "MENTIONS", "LINKS_TO"}, {"Ticket", "PullRequest"}, "out")
    ids = _ordered(r["id"] for r in found if not _stub(r))
    return ids[0] if len(ids) == 1 else None


def knowledge(v):
    """Conversations worth reading: mail threads, chat and forum threads, PR conversations and ticket comment
    threads with real back-and-forth."""
    tasks, skipped = [], collections.Counter()
    by_key = {}  # existing decisions by the key their statement starts with (KIP-1340, FLIP-547, ...)
    for d in v.typed("Decision"):
        if d["props"].get("identifier"):
            by_key.setdefault(d["props"]["identifier"], d["id"])
        elif (m := re.match(r"([A-Z][A-Z0-9]*-\d+)\b", d["props"].get("statement") or "")):
            by_key.setdefault(m.group(1), d["id"])

    def add(subject, title, msgs, about=None):
        msgs = [m for m in msgs if not v.is_bot(m["id"])][:MAX_SOURCES]
        if len(msgs) < 2 or sum(len(m["props"].get("text") or "") for m in msgs) < 600:
            skipped["too short to hold knowledge"] += 1
            return
        sources = [Item(f"S{i + 1}", m["id"], f"{m['type']} by {v.author(m['id'])} ({(m['props'].get('sent_at') or m['props'].get('at') or '')[:10]})",
                        _clip(m["props"].get("text"), TEXT_CAP), m["type"]) for i, m in enumerate(msgs)]
        named = _ordered(by_key[k] for k in re.findall(r"\b[A-Z][A-Z0-9]*-\d+\b", title) if k in by_key)
        tasks.append(Task("knowledge", subject["id"], title, "", sources, [],
                          {"about": subject["id"] if subject["type"] in ("Ticket", "PullRequest") else about,
                           # a decision taken here about an existing proposal is that proposal, not a new record
                           "decision": named[0] if len(named) == 1 else None}))

    for th in v.typed("EmailThread"):
        msgs = sorted(v.ends(th["id"], {"IN"}, {"EmailMessage"}, "in"), key=lambda m: m["props"].get("sent_at") or "")
        if msgs:
            add(msgs[0], f"Mail thread: {th['props'].get('normalized_subject')}", msgs, _linked_work(v, msgs))
    for th in v.typed("Thread"):  # chat threads (Slack) and forum topics (Discourse); review threads are PR comments
        if th["props"].get("kind") not in ("chat", "discussion"):
            continue
        msgs = sorted(v.ends(th["id"], {"IN"}, {"Message", "Comment"}, "in"), key=lambda m: m["props"].get("at") or "")
        tickets = [t["id"] for t in v.ends(th["id"], {"DISCUSSED_IN"}, {"Ticket"}, "in")]
        if msgs:
            label = "Forum topic" if th["props"]["kind"] == "discussion" else "Chat thread"
            add(msgs[0], f"{label}: {th['props'].get('title') or (msgs[0]['props'].get('text') or '')[:80]}", msgs,
                tickets[0] if len(tickets) == 1 else _linked_work(v, msgs))
    for pr in v.typed("PullRequest"):
        add(pr, f"Pull request #{pr['props'].get('number')}: {pr['props'].get('title')}", v.comments(pr["id"]))
    for t in v.typed("Ticket"):
        add(t, f"Ticket {t['props'].get('identifier')}: {t['props'].get('title')}", v.comments(t["id"]))
    return tasks, skipped


DOC_RECENT_DAYS = 120  # wiki pages edited this close to the newest edit are read; older pages were read before


def doc_knowledge(v):
    """Documents: stored files (docs adapter) and wiki pages edited recently, read whole for guidelines,
    decisions and how-tos. A document is one source, so the quote must be in its text."""
    tasks, skipped = [], collections.Counter()
    edits = {}
    for d in v.typed("Document") + v.typed("File"):
        cvs = [v.rec[lk["a"]] for lk in v.inn[d["id"]] if lk["type"] == "VERSION_OF"]
        when = max((c["props"].get("valid_from") or "" for c in cvs), default="")
        if cvs:
            edits[d["id"]] = when
    newest = max(edits.values(), default="")
    cutoff = ""
    if newest:
        import datetime as _dt
        cutoff = (_dt.datetime.strptime(newest[:10], "%Y-%m-%d") - _dt.timedelta(days=DOC_RECENT_DAYS)).strftime("%Y-%m-%d")
    for did, when in edits.items():
        d = v.rec[did]
        p = d["props"]
        is_stored = p["source_tool"] == "docs" or (d["type"] == "File" and p.get("kind") == "doc")
        if d["type"] == "Document" and when and when[:10] < cutoff:
            skipped["wiki page not edited recently"] += 1
            continue
        if d["type"] == "File" and not is_stored:
            continue
        text = v.description(did)
        if len(text) < 600:
            skipped["too short to hold knowledge"] += 1
            continue
        title = p.get("title") or p.get("name") or p.get("path")
        src = Item("S1", did, f"{'Page' if d['type'] == 'Document' else 'Document'}: {title}", _clip(text, DOC_TEXT_CAP), d["type"])
        tasks.append(Task("doc_knowledge", did, f"Document: {title}", "", [src], [], {"about": None, "decision": None}))
    return tasks, skipped


BUILDERS = {"pr_ticket": pr_ticket, "commit_pr": commit_pr, "ticket_fix": ticket_fix, "email_topic": email_topic,
            "identity": identity, "decision_status": decision_status, "knowledge": knowledge, "doc_knowledge": doc_knowledge}
