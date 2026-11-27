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
  doc_facts        every document, read whole         -> the records and links the schema supports (extract_facts.py)
  table_map        every table, one call each         -> a column mapping, applied to every row by code (extract_tables.py)
  doc_knowledge    every document, read whole         -> the six knowledge types only, extract then judge. doc_facts
                                                         covers them in the same read, so this kind is not run by default

Nothing here has a fixed character cap. A text is shown whole; what one call can carry comes from the
model's context window (fill_budget.sizes), and a conversation or document longer than that is read in
consecutive windows cut between messages or pieces, so nothing is left unread.
"""

from __future__ import annotations

import collections
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field

from . import common, extract_schema, fill_budget, fill_docs

MAX_CANDIDATES = 5      # how many candidates the search proposes for one gap (a search setting, not a text limit)
MIN_CONVERSATION = 600  # characters: a thread shorter than this, or with one message, is not a conversation

NOT_FIXED = {"duplicate", "won't fix", "invalid", "not a bug", "not a problem", "cannot reproduce", "abandoned",
             "later", "information provided", "won't do", "incomplete", "auto closed", "workaround"}
NO_TICKET = re.compile(r"^\s*\[?(minor|hotfix|trivial)\]?\b", re.I)  # the project's own "needs no ticket" markers
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
PR_REF = re.compile(r"(?:/pull/|(?<![\w/])#)(\d{1,6})\b")
GUIDELINE_STRENGTH = ["must", "should", "avoid"]
# kinds that answer with an item (and a quote) for every fact they read: their text is bounded by the reply too.
# Judge kinds (a choice among candidates, a table's column mapping) answer in a few lines whatever they read.
ANSWER_GROWS_WITH_TEXT = {"doc_facts", "knowledge", "doc_knowledge"}
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
    context: str = ""               # shown before the text: where this window sits (document title, outline, part)
    extra: str = ""                 # anything else the answer depends on (a table's full content hash)

    @property
    def id(self) -> str:
        """Changes when the subject, its candidates or their text change, so a stale verdict is asked again."""
        h = hashlib.sha1(f"{self.kind}|{self.subject}".encode())
        for it in self.sources + self.candidates:
            h.update(f"|{it.label}:{it.id}:{it.title}:{it.text}".encode())
        if self.context or self.extra:
            h.update(f"|{self.context}|{self.extra}".encode())
        return "t_" + h.hexdigest()[:20]

    def labels(self) -> list[str]:
        return [c.label for c in self.candidates]


class View:
    """Read-only lookups over the written graph."""

    def __init__(self, records, links, budget=None, doc_sample=None, doc_quality=None, doc_only=None):
        self.budget = budget or fill_budget.sizes()   # what one call may carry
        self.overhead = {}                            # kind -> characters of instructions and schema around the text
        self.doc_sample, self.doc_quality, self.doc_only = doc_sample, doc_quality or {}, doc_only
        self.cut = collections.Counter()              # texts larger than a whole window, cut at line ends, by kind
        self.quoted = collections.Counter()           # characters of quoted earlier messages left out (dequote), by kind
        self.claimed = set()                          # table pieces a table_map task reads: the reading kinds leave them out
        self._fields = {}                             # (type, field) -> {value: [record ids]}, built when first asked
        self._docs = self._names = self._identity = self._catalog = None
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

    # ---- sizing
    def room(self, kind, share=1.0):
        """Characters of text one call of this kind may carry: the window less its instructions, and for a kind
        whose answer grows with what it reads, also no more than the reply can answer (fill_budget.read_chars)."""
        room = self.budget.room(self.overhead.get(kind, 4000))
        if kind in ANSWER_GROWS_WITH_TEXT:
            room = min(room, self.budget.read_chars)
        return int(room * share)

    # ---- documents (fill_docs), read once
    def docs(self):
        if self._docs is None:
            self._docs = fill_docs.documents(self, self.doc_sample, self.doc_quality, self.doc_only)
        return self._docs

    def catalog(self):
        if self._catalog is None:
            self._catalog = extract_schema.Catalog(common._reg, [t for t, rs in self.by_type.items() if rs])
            self._catalog.budget = self.budget   # the prompts' schema sections are sized by this view's budget
        return self._catalog

    # ---- finding a record by what a text calls it
    def _index(self):
        if self._names is None:
            self._names = collections.defaultdict(list)
            self._prefix = collections.defaultdict(dict)   # leading words of a longer personal name -> {record id: None}
            self._tail = collections.defaultdict(dict)     # the last part of a qualified name ("owner/name") -> {id: None}
            reg = common._reg  # the links that tie one identity's records together (a person, its accounts, its addresses)
            self._identity = {n for n, lk in reg.links.items() if lk.relation in ("ALIAS_OF", "HAS_ACCOUNT")}
            for r in self.rec.values():
                self.add_name(r)
        return self._names

    def add_name(self, r):
        """Index a record under every name it goes by (its name fields, and the lookup fields: emails, handles ...)."""
        if self._names is None:
            return
        spec = common._reg.records.get(r["type"])
        if spec is None:
            return
        p = r["props"]
        rooted = extract_schema.has_path(common._reg, r["type"])   # "/src/a.py" is also found as "src/a.py"
        for f in [*extract_schema.names_of(spec), *extract_schema.LOOKUP_FIELDS]:
            val = p.get(f)
            for x in val if isinstance(val, list) else [val]:
                for y in {x, extract_schema.repo_path(x)} if rooted else {x}:
                    if isinstance(y, str) and (k := name_key(y)) and r["id"] not in self._names[k]:
                        self._names[k].append(r["id"])
        for f in extract_schema.names_of(spec):
            if isinstance(p.get(f), str) and QUALIFIED.fullmatch(k := name_key(p[f])):
                self._tail[k.rsplit("/", 1)[1]][r["id"]] = None
        full = p.get(extract_schema.PERSONAL_NAME)
        words = name_key(full).split() if isinstance(full, str) else []
        for n in range(1, len(words)):   # "emma", "emma van" for "Emma van Dijk"
            self._prefix[" ".join(words[:n])][r["id"]] = None

    def by_field(self, rtype, field, value):
        """Records of `rtype` whose `field` holds this value (a pull request's number, a commit's sha): equal, or,
        for a value of 7 characters or more written shorter than it is stored (a short sha), the one stored value
        that starts with it."""
        idx = self._fields.get((rtype, field))
        if idx is None:
            idx = self._fields[(rtype, field)] = collections.defaultdict(list)
            for r in self.by_type[rtype]:
                self.index_field(r, field, idx)
        k = _field_key(extract_schema.repo_path(value) if field == extract_schema.PATH_FIELD else value)
        hits = idx.get(k, [])
        if not hits and len(k) >= 7:
            longer = [ids for stored, ids in idx.items() if stored.startswith(k)]
            hits = longer[0] if len(longer) == 1 else []
        return [self.rec[i] for i in hits]

    def index_field(self, r, field, idx=None):
        """Add one record to the (type, field) index (a record this stage just made)."""
        idx = self._fields.get((r["type"], field)) if idx is None else idx
        val = r["props"].get(field)
        if field == extract_schema.PATH_FIELD:   # "/src/a.py" and "src/a.py": one path (repo_path)
            val = extract_schema.repo_path(val)
        if idx is not None and val is not None and r["id"] not in idx[_field_key(val)]:
            idx[_field_key(val)].append(r["id"])

    def named_otherwise(self, name, person_types):
        """What a phrase given as a person's name is instead, by the graph's own records, or None. A personal name
        is neither the name of a record of another kind (a team, a seat, a service, a topic ...), nor holds one as a
        run of two or more of its words ("Shipment & Operations Product Manager" holds the team "Shipment &
        Operations"), nor is or holds a job title a person record of the graph holds ("Product Manager"). The
        records of the person's own identity (`person_types`: the person, its accounts, its addresses) do not count."""
        self._index()
        words = name_key(name).split()
        runs = [" ".join(words[i:j]) for i in range(len(words)) for j in range(len(words), i, -1)
                if j - i >= 2 or j - i == len(words)]
        titles = None
        for run in runs:
            other = next((self.rec[i] for i in self._names.get(run, ()) if self.rec[i]["type"] not in person_types), None)
            if other is not None:
                return f"the name of a {other['type']}"
            if titles is None:   # the other names person records hold (their job titles), as name keys: the whole
                # title and every run of two or more of its words ("Product Manager / Payment & Billing" holds
                # "product manager", the title as a directory writes it with the person's area)
                titles = set()
                for t in person_types:
                    if t not in common._reg.records or extract_schema.PERSONAL_NAME not in common._reg.record(t).fields:
                        continue
                    for r in self.by_type.get(t, ()):
                        for f in extract_schema.OTHER_NAMES:
                            if isinstance(r["props"].get(f), str) and (tw := name_key(r["props"][f]).split()):
                                titles.add(" ".join(tw))
                                titles.update(" ".join(tw[i:j]) for i in range(len(tw)) for j in range(i + 2, len(tw) + 1))
            if run in titles:
                return "a job title a person holds"
        return None

    def by_name_prefix(self, name, types):
        """Records of `types` whose personal name is `name` followed by more words, where those words are the
        value of another of that record's fields or the name of a record linked to it: "Lionel" is the record
        "Lionel eng-orders" whose team (or id) is eng-orders. The rest of a name that is nothing of its own record
        ("Noah" and "Noah Patel") is no match: a name alone never makes two records one."""
        self._index()
        key = name_key(name)
        words = key.split()
        if not words:
            return []
        out = []
        for i in self._prefix.get(key, ()):
            r = self.rec[i]
            full = name_key(r["props"].get(extract_schema.PERSONAL_NAME) or "")
            if r["type"] in types and full.startswith(key + " ") and full[len(key) + 1:] in self.own_values(r):
                out.append(r)
        return out

    def own_values(self, r):
        """Every value a record holds besides its name, and the names of the records linked to it, as name keys."""
        vals = {name_key(str(v)) for k, v in r["props"].items()
                if k != extract_schema.PERSONAL_NAME and not k.startswith("_") and isinstance(v, (str, int)) and not isinstance(v, bool)}
        for lk in self.out[r["id"]] + self.inn[r["id"]]:
            other = self.rec.get(lk["b"] if lk["a"] == r["id"] else lk["a"])
            spec = common._reg.records.get(other["type"]) if other else None
            if spec is not None:
                vals |= {name_key(str(other["props"][f])) for f in extract_schema.names_of(spec) if other["props"].get(f)}
        return vals - {""}

    def by_name_tail(self, name, types):
        """Records of `types` whose name is a qualified one ("owner/name", no spaces) ending in this one-part name:
        "payments-platform" -> the ONE "acme-commerce/payments-platform". Several such records: none (ambiguous)."""
        self._index()
        k = name_key(name)
        if not k or not SIMPLE.fullmatch(k):
            return []
        hits = [self.rec[i] for i in self._tail.get(k, ()) if self.rec[i]["type"] in types]
        return hits if len(hits) == 1 else []

    def linked(self, a, b):
        """Is record `a` linked to `b` or to one of the records of b's identity (its accounts, addresses, aliases)?"""
        self._index()
        ids, frontier = {b["id"]}, [b["id"]]
        for _ in range(2):
            nxt = []
            for i in frontier:
                for lk in self.out[i] + self.inn[i]:
                    o = lk["b"] if lk["a"] == i else lk["a"]
                    if lk["type"] in self._identity and o not in ids:
                        ids.add(o)
                        nxt.append(o)
            frontier = nxt
        return any((lk["b"] if lk["a"] == a["id"] else lk["a"]) in ids for lk in self.out[a["id"]] + self.inn[a["id"]])

    def by_first_name(self, name, types):
        """Records of `types` whose personal name (full_name) starts with this one word: 'Emma' -> 'Emma Stone'."""
        self._index()
        k = name_key(name)
        return [self.rec[i] for i in self._prefix.get(k, ()) if self.rec[i]["type"] in types and len(k.split()) == 1]

    def resolve(self, name, types, prefer_tool=None):
        """The records of `types` a text means by `name`: found by name, key, alias, email or login, directly or
        through the records of the same identity (an address -> its account -> its person). Several records of
        one person (their accounts in different tools) count as one: the one in `prefer_tool`, else the first by key."""
        hits = [self.rec[i] for i in self._index().get(name_key(name), ())]
        if extract_schema.repo_path(name) != name:   # a rooted path ("/src/a.py"): also a record of a type with a path
            hits += [self.rec[i] for i in self._names.get(name_key(extract_schema.repo_path(name)), ())
                     if extract_schema.has_path(common._reg, self.rec[i]["type"]) and self.rec[i] not in hits]
        found = [r for r in hits if r["type"] in types]
        seen, frontier = {r["id"] for r in hits}, hits
        for _ in range(2):
            if found:
                break
            nxt = []
            for r in frontier:
                for lk in self.out[r["id"]] + self.inn[r["id"]]:
                    other = lk["b"] if lk["a"] == r["id"] else lk["a"]
                    if lk["type"] in self._identity and other not in seen:
                        seen.add(other)
                        nxt.append(self.rec[other])
            found, frontier = [r for r in nxt if r["type"] in types], nxt
        if len(found) > 1:
            owners = {tuple(sorted(lk["a"] for lk in self.inn[r["id"]] if lk["type"] == "HAS_ACCOUNT")) for r in found}
            if len(owners) == 1 and owners != {()}:
                found.sort(key=lambda r: (r["props"].get("source_tool") != prefer_tool, r["props"]["_key"]))
                found = found[:1]
        return found

    def is_bot(self, rid):
        for r in self.ends(rid, {"AUTHORED_BY", "FROM"}, direction="out"):
            p = r["props"]
            if p.get("is_bot") or p.get("kind") == "automation" or (p.get("login") or "").endswith("[bot]"):
                return True
        return False


QUALIFIED = re.compile(r"[^\s/]+(?:/[^\s/]+)+")   # a name written with its namespace: owner/name, group/sub/name
SIMPLE = re.compile(r"[^\s/]+")


def _field_key(value):
    """A field value as the field index keys it: 174 and "174" alike, case and outer spaces aside."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip().casefold()


def name_key(s):
    """A name as the index keys it: letters as typed apart from case, width and spacing; no @ or quotes around it."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip(" \t\"'“”‘’<>()[],;:").lstrip("@").casefold()


def _stub(r):
    p = r["props"]
    return (r["type"] in ("Ticket", "PullRequest") and "title" not in p) or p.get("_stub")


# ---------------------------------------------------------------- candidate search

STOP = set("""the and for with from into that this when not are was use add adds added fix fixes fixed make
remove update support minor should can will which via its also more one new all when then than has have
pull request issue""".split())
COMMON_SHARE = 0.05  # a word in more than this share of the titles (the product's own name, say) is not a rare word


def tokens(text):
    return {w for w in re.findall(r"[a-z][a-z0-9_]{2,}", (text or "").lower()) if w not in STOP}


class Similar:
    """Titles that share rare words with a text. It only proposes candidates; the LLM decides."""

    def __init__(self, entries):
        self.entries = [(rid, tokens(text)) for rid, text in entries]
        df = collections.Counter(t for _, ts in self.entries for t in ts)
        n = max(len(self.entries), 1)
        self.idf = {t: math.log(n / c) for t, c in df.items()}
        self.common = {t for t, c in df.items() if n >= 100 and c > n * COMMON_SHARE}
        self.index = collections.defaultdict(list)
        for i, (_, ts) in enumerate(self.entries):
            for t in ts:
                self.index[t].append(i)

    def top(self, text, n=3, min_shared=2, exclude=()):
        q = tokens(text) - self.common
        scores, shared = collections.Counter(), collections.Counter()
        for t in q:
            for i in self.index.get(t, ()):
                scores[i] += self.idf[t]
                shared[i] += 1
        ranked = sorted(scores, key=lambda i: (-round(scores[i], 6), i))  # ties in input order: same candidates every run
        best = [i for i in ranked if shared[i] >= min_shared and self.entries[i][0] not in exclude]
        return [self.entries[i][0] for i in best[:n]]


def _clean(text):
    """The text whole, with runs of blank lines closed up."""
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())


_QUOTED = re.compile(r"^\s*(?:>\s?)+")
QUOTE_MARK = "[quoted lines of an earlier message, shown above, are left out]"


def dequote(texts, counter=None, kind=""):
    """A conversation's messages, oldest first, without the lines that only quote an earlier one of them.

    A reply usually carries the messages before it again, each line marked ">". Such a line is left out only
    when the same line is in an earlier message of this same conversation, which is shown on its own: nothing
    is lost, and the conversation is not read once per reply. A quoted line from anywhere else stays."""
    seen, out = set(), []
    for text in texts:
        kept, own, gap = [], [], False
        for line in _clean(text).split("\n"):
            m = _QUOTED.match(line)
            body = re.sub(r"\s+", " ", line[m.end():] if m else line).strip().casefold()
            if m and (not body or body in seen):
                if body and not gap:
                    kept.append(QUOTE_MARK)
                    gap = True
                if counter is not None:
                    counter[kind] += len(line) + 1
                continue
            if body:
                gap = False
                own.append(body)
            kept.append(line)
        seen.update(own)
        out.append("\n".join(kept).strip())
    return out


def _first_line(text):
    return (text or "").strip().split("\n", 1)[0]


# ---------------------------------------------------------------- items

def ticket_item(v, t, label):
    p = t["props"]
    return Item(label, t["id"], f"Ticket {p.get('identifier')}: {p.get('title') or ''}", _clean(v.description(t["id"])), "Ticket")


def pr_item(v, pr, label):
    p = pr["props"]
    title = f"Pull request #{p.get('number')}: {p.get('title') or ''} (branch {p.get('head_ref') or '?'}, {p.get('state') or '?'})"
    return Item(label, pr["id"], title, _clean(v.description(pr["id"])), "PullRequest")


def commit_item(v, c, label):
    p = c["props"]
    return Item(label, c["id"], f"Commit {p['sha'][:10]}: {_first_line(p.get('message'))}", _clean(p.get("message")), "Commit")


def comment_items(v, rid, start):
    """Every comment, whole (a judge prompt larger than the window is fitted in fill.judge_prompt)."""
    return [Item(f"S{start + i}", c["id"], f"Comment by {v.author(c['id'])} ({(c['props'].get('at') or '')[:10]})",
                 _clean(c["props"]["text"]), c["type"]) for i, c in enumerate(v.comments(rid))]


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
        sources = [pr_item(v, pr, "S1")] + comment_items(v, pr["id"], 2)
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
                          [commit_item(v, c, "S1")],
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
        sources = [ticket_item(v, t, "S1")] + comment_items(v, t["id"], 2)
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
        texts = dequote([m["props"].get("text") for m in human], v.quoted, "email_topic")
        sources = [Item(f"S{i + 1}", m["id"], f"Email from {v.author(m['id'])}: {m['props'].get('subject')}",
                        texts[i], "EmailMessage") for i, m in enumerate(human)]
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
    statuses = list(common._reg.record("Decision").fields["status"].enum)  # the schema's own values
    for d in v.typed("Decision"):
        if d["props"].get("status"):
            continue
        docs = v.ends(d["id"], {"MADE_IN"}, {"Document"}, "out")
        text = v.description(docs[0]["id"]) if docs else ""
        if not text:
            skipped["no page text"] += 1
            continue
        tasks.append(Task("decision_status", d["id"], f"What status does this page give the proposal '{d['props'].get('statement')}'?",
                          "the page states the status (a status or state line, a vote result, 'accepted', 'adopted', "
                          "'under discussion', 'superseded by' ...). If the page says nothing, or gives a status that is "
                          "none of the values, reject.",
                          [Item("S1", docs[0]["id"], f"Page: {docs[0]['props'].get('title')}", _clean(text), "Document")],
                          [Item(s, None, s) for s in statuses], {"field": "status"}))
    return tasks, skipped


def _linked_work(v, msgs):
    """The one ticket or PR a conversation is already linked to (by the adapters or the linker), else None."""
    found = []
    for m in msgs:
        found += v.ends(m["id"], {"DISCUSSES", "ANNOUNCES", "MENTIONS", "LINKS_TO"}, {"Ticket", "PullRequest"}, "out")
    ids = _ordered(r["id"] for r in found if not _stub(r))
    return ids[0] if len(ids) == 1 else None


def windowed(v, kind, items, share=1.0):
    """Items in consecutive windows that fit one call of this kind, cut only between items. An item larger
    than a whole window (one huge message or piece) is cut at line ends into items with the same record id."""
    room = v.room(kind, share)
    flat = []
    for it in items:
        if len(it.title) + len(it.text) + 16 <= room:
            flat.append(it)
            continue
        v.cut[kind] += 1
        repeat = fill_docs.header_line(it.text) if it.type == "table" else ""
        parts = fill_budget.split_text(it.text, room - len(it.title) - 40, repeat)
        flat += [Item(it.label, it.id, f"{it.title} (part {i + 1} of {len(parts)})", part, it.type) for i, part in enumerate(parts)]
    for n, it in enumerate(flat):  # labels are unique across the whole text, so a window's answer names its item
        it.label = f"{it.label[0]}{n + 1}"
    return fill_budget.pack(flat, room, size=lambda it: len(it.title) + len(it.text) + 16)


def knowledge(v):
    """Conversations worth reading: mail threads, chat and forum threads, PR conversations and ticket comment
    threads with real back-and-forth. Every message is read: a conversation longer than one call can carry
    becomes consecutive windows, cut between messages."""
    tasks, skipped = [], collections.Counter()
    by_key = {}  # existing decisions by the key their statement starts with (ABC-123)
    for d in v.typed("Decision"):
        if d["props"].get("identifier"):
            by_key.setdefault(d["props"]["identifier"], d["id"])
        elif (m := re.match(r"([A-Z][A-Z0-9]*-\d+)\b", d["props"].get("statement") or "")):
            by_key.setdefault(m.group(1), d["id"])

    def add(subject, title, msgs, about=None):
        msgs = [m for m in msgs if not v.is_bot(m["id"])]
        if len(msgs) < 2 or sum(len(m["props"].get("text") or "") for m in msgs) < MIN_CONVERSATION:
            skipped["too short to hold knowledge"] += 1
            return
        texts = dequote([m["props"].get("text") for m in msgs], v.quoted, "knowledge")
        items = [Item("S", m["id"], f"{m['type']} by {v.author(m['id'])} ({(m['props'].get('sent_at') or m['props'].get('at') or '')[:10]})",
                      text, m["type"]) for m, text in zip(msgs, texts) if text]
        named = _ordered(by_key[k] for k in re.findall(r"\b[A-Z][A-Z0-9]*-\d+\b", title) if k in by_key)
        wins = windowed(v, "knowledge", items)
        for i, sources in enumerate(wins):
            part = f"part {i + 1} of {len(wins)} of this conversation; the other parts are read separately" if len(wins) > 1 else ""
            tasks.append(Task("knowledge", subject["id"], title, "", sources, [],
                              {"about": subject["id"] if subject["type"] in ("Ticket", "PullRequest") else about,
                               # a decision taken here about an existing proposal is that proposal, not a new record
                               "decision": named[0] if len(named) == 1 else None}, context=part))

    for th in v.typed("EmailThread"):
        msgs = sorted(v.ends(th["id"], {"IN"}, {"EmailMessage"}, "in"), key=lambda m: m["props"].get("sent_at") or "")
        if msgs:
            add(msgs[0], f"Mail thread: {th['props'].get('normalized_subject')}", msgs, _linked_work(v, msgs))
    for th in v.typed("Thread"):  # chat threads and forum topics; review threads are PR comments
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


def doc_windows(v, kind, doc, units, share=1.0):
    """[(context, [Item])]: one document's units as consecutive windows for `kind`. Each window is headed by the
    document's title, its outline and where the window sits, so a passage is never read without its place."""
    room = v.room(kind, share)
    outline = fill_docs.outline_text(doc, room // 8)
    room -= len(outline) + len(doc.title) + 200
    wins, cut = fill_docs.windows(units, room)
    v.cut[kind] += cut
    out, n = [], 0
    for i, win in enumerate(wins):
        items = []
        for u in win:
            n += 1
            where = (u.heading or doc.title) + (f" ({u.kind})" if u.kind != "text" else "") + (f" (part {u.part} of this passage)" if u.part else "")
            items.append(Item(f"P{n}", u.id, where, u.text, u.kind))
        ctx = [f"DOCUMENT: {doc.title}"]
        if outline:
            ctx += ["OUTLINE OF THE WHOLE DOCUMENT", outline]
        if len(wins) > 1:
            ctx.append(f"THIS IS PART {i + 1} OF {len(wins)}, from \"{win[0].heading or doc.title}\" to \"{win[-1].heading or doc.title}\". "
                       "The other parts are read separately: report only what this part states.")
        out.append(("\n".join(ctx), items))
    return out


def doc_knowledge(v):
    """Every document, read whole for the six knowledge types (extract, then judge). Kept for comparison with
    doc_facts, which reads the same text once for these and for everything else the schema supports."""
    docs, skipped = v.docs()
    tasks, skipped = [], collections.Counter(skipped)
    for doc in docs:
        units = fill_docs.text_units(doc, v.claimed)
        if not units:
            skipped["only tables (read by table_map)"] += 1
            continue
        for ctx, items in doc_windows(v, "doc_knowledge", doc, units):
            tasks.append(Task("doc_knowledge", doc.rec["id"], f"Document: {doc.title}", "", items, [],
                              {"about": None, "decision": None, "doc": doc.rec["id"], "quality": doc.quality}, context=ctx))
    return tasks, skipped


def _doc_facts(v):
    from . import extract_facts
    return extract_facts.build(v)


def _table_map(v):
    from . import extract_tables
    return extract_tables.build(v)


BUILDERS = {"pr_ticket": pr_ticket, "commit_pr": commit_pr, "ticket_fix": ticket_fix, "email_topic": email_topic,
            "identity": identity, "decision_status": decision_status, "knowledge": knowledge,
            "table_map": _table_map, "doc_facts": _doc_facts, "doc_knowledge": doc_knowledge}
# what a run asks unless told otherwise: doc_knowledge is left out because doc_facts reads the same documents for
# the same six types and more, in one call per window instead of two
DEFAULT_KINDS = [k for k in BUILDERS if k != "doc_knowledge"]
