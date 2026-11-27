"""What a requirement or rule waits on: DEPENDS_ON (kind gate / relies_on) to the records that state those checks.

A requirement such as "Do not advance an order for warehouse release until financial authorization, fraud/risk
clearance and inventory allocation pass the configured gates" names its gates in words, not by id. The document
reading lists only what its own window states, so it cannot link to the requirement, rule or service elsewhere in the
graph that states the gate. This pass does, after every document is read:

  candidates  code proposes, for each stated requirement or rule (the registry's from-types of DEPENDS_ON that a
              document states), the records of the link's to-types that share the most rare words with it (Similar:
              words of any script, weighted by rarity). Code only proposes; it never links.
  question    one call per window of items (bounded by the budget like every prompt): for each item, which of ITS
              candidates its own words say it waits on (gate) or relies on, with the item's words that name it.
  check       the candidate is one of that item's own, the kind is one the registry names for DEPENDS_ON, and the
              quote is word for word in the item's statement. A candidate that only shares words is the model's to
              refuse; nothing is linked without a quote.
  write       DEPENDS_ON item -> candidate, method llm_judge, the item's own source passage as evidence.

No target is ever created: an item whose gates no record states gets no link (counted).
"""

from __future__ import annotations

import collections
import math
import re

from . import common
from .fill_tasks import Item, Task

LINK = "DEPENDS_ON"
KINDS = ("gate", "relies_on")       # of the registry's named kinds, the two a statement of conditions can state
MAX_CANDIDATES = 6                  # proposals per item (a search setting, not a text limit)
MIN_SHARED = 2                      # rare words a candidate must share with the item to be proposed
ITEMS_PER_CALL = 12                 # items per question; the window is cut smaller when the budget says so
_WORD = re.compile(r"\w{3,}")

ASK = ("Each ITEM below is a requirement or rule from the organisation's documents. Under it are CANDIDATES: other "
       "requirements, rules and services the organisation's records hold.\n"
       "For each item, list the candidates its own words say it waits on or depends on:\n"
       "- gate: something the item says must pass, be met, be cleared or be done before what it governs may go ahead;\n"
       "- relies_on: something else the item says it needs or relies on.\n"
       "Only when the item's words name what that candidate states (the same check, condition or service). A candidate "
       "that only shares words with the item is not one. Most items depend on none of their candidates: an empty list "
       "is right then.\n"
       "quote: the item's own words (word for word from the item, under 200 characters) that name the condition the "
       "candidate states.\n"
       'Reply with one JSON object: {"links": [{"item", "candidate", "kind", "quote"}]}. item and candidate are labels '
       "as shown (I1, I1.C2 ...).\n\n")


def _words(text):
    return {w.casefold() for w in _WORD.findall(text or "") if not w.isdigit()}


def _text(rec, cat):
    p = rec["props"]
    return " ".join(str(p.get(f) or "") for f in (cat.name_field(rec["type"]), cat.text_field(rec["type"])) if f)


def _source(rec):
    for st in (rec["props"].get("_stated") or {}).values():
        for src in (st.get("by") or {}):
            return src
    return None


def build(w, budget_chars):
    """The tasks: windows of stated items, each with its own candidates. Returns (tasks, counts)."""
    reg, cat = common._reg, w.v.catalog()
    lk = reg.link(LINK)
    froms = [t for t in cat.stated if any(reg.is_a(t, x) for x in lk.from_types if x != "any")]
    tos = {t for t in reg.records if any(reg.is_a(t, x) for x in lk.to_types if x != "any")}
    recs = list(w.g.records.values())
    subjects = [r for r in recs if r["type"] in froms and _text(r, cat).strip() and _source(r)]
    pool = [r for r in recs if r["type"] in tos and _text(r, cat).strip()]
    words = {r["id"]: _words(_text(r, cat)) for r in pool + subjects}
    df = collections.Counter(t for r in pool for t in words[r["id"]])
    idf = {t: math.log(max(len(pool), 1) / c) for t, c in df.items()}
    index = collections.defaultdict(list)
    for r in pool:
        for t in words[r["id"]]:
            index[t].append(r)
    from .extract_facts import own_id
    ids = {r["id"]: own_id(r, cat) for r in pool + subjects if r["type"] in cat.stated}
    counts, rows = collections.Counter(), []
    for s in subjects:
        score, shared = collections.Counter(), collections.Counter()
        for t in words[s["id"]]:
            for r in index.get(t, ()):
                # never the item itself, nor the same numbered item another document restates (its own id)
                if r["id"] != s["id"] and not (ids.get(s["id"]) and ids.get(r["id"]) == ids.get(s["id"])):
                    score[r["id"]] += idf[t]
                    shared[r["id"]] += 1
        best = sorted((i for i in score if shared[i] >= MIN_SHARED), key=lambda i: (-round(score[i], 6), i))[:MAX_CANDIDATES]
        if not best:
            counts["items with no candidate (no record shares enough words): not asked"] += 1
            continue
        rows.append((s, [w.by_id[i] for i in best]))
    rows.sort(key=lambda x: (_source(x[0]) or "", x[0]["id"]))   # an item's neighbours in its document ask together
    tasks, cur, size = [], [], len(ASK)
    for s, cands in rows:
        n = len(_text(s, cat)) + sum(len(_text(c, cat)) + 40 for c in cands) + 60
        if cur and (len(cur) >= ITEMS_PER_CALL or size + n > budget_chars):
            tasks.append(_task(cur, cat))
            cur, size = [], len(ASK)
        cur.append((s, cands))
        size += n
    if cur:
        tasks.append(_task(cur, cat))
    counts["items asked"] = len(rows)
    return tasks, counts


def _task(rows, cat):
    sources, cands = [], []
    for i, (s, cs) in enumerate(rows, 1):
        sources.append(Item(f"I{i}", s["id"], s["type"], _text(s, cat), s["type"]))
        cands += [Item(f"I{i}.C{k}", c["id"], c["type"], _text(c, cat), c["type"]) for k, c in enumerate(cs, 1)]
    return Task("depends_on", rows[0][0]["id"], "What does each item wait on or depend on?", "the item's words name it",
                sources, cands, {"items": len(rows)})


# ---------------------------------------------------------------- the level a stated grant has

def level_tasks(w):
    """A grant this stage read from a text (GRANT_LINK) without the level its text may state: one small question each,
    the registry's level values to choose from, the passage it was read from as the text. The model answers only
    from that passage, with a quote; code writes the level onto the same link."""
    from .extract_schema import GRANT_LINK
    reg = common._reg
    spec = reg.link(GRANT_LINK).fields.get("level")
    if spec is None or not spec.enum:
        return []
    out = []
    for lk, _, _ in list(w.g.links.values()):
        p = lk["props"]
        if lk["type"] != GRANT_LINK or p["claim_id"] in w.old_links or p.get("level") or not p.get("evidence"):
            continue
        src = w.by_id.get(p["evidence"][0])
        if src is None or not src["props"].get("text"):
            continue
        a, b = w.by_id[lk["a"]], w.by_id[lk["b"]]
        who, where = _name(a), _name(b) + (f" ({b['props']['text']})" if b["props"].get("text") else "")
        out.append(Task("grant_level", a["id"], f"What may {who} do in {where}, as the text states it?",
                        "the text says what members may do there, in any words (to read or view: read; to comment: "
                        "comment; to write, change, push or deploy: write; to administer: admin). If it says nothing "
                        "about what may be done, reject.",
                        [Item("S1", src["id"], "", src["props"]["text"])], [Item(v, None, v) for v in spec.enum],
                        {"claim": p["claim_id"]}))
    return out


def apply_level(w, t, e):
    lk = next((x for x, _, _ in w.g.links.values() if x["props"]["claim_id"] == t.write["claim"]), None)
    if lk is None:
        return "the grant is gone"
    before = dict(lk["props"])
    lk["props"]["level"] = e["choice"]
    res = common._vlink(lk, w.by_id[lk["a"]]["type"], w.by_id[lk["b"]]["type"])
    if not res.ok:
        lk["props"] = before
        return f"schema: {res.errors[0]}"
    w.facts[f"{lk['type']} level {e['choice']}: written from its passage"] += 1
    return None


def _name(r):
    p = r["props"]
    return str(next((p[f] for f in ("full_name", "name", "title") if p.get(f)), r["type"]))


def prompt(t):
    out = [ASK]
    for s in t.sources:
        out.append(f"[{s.label}] {s.title}: {s.text}\n  CANDIDATES:")
        out += [f"  [{c.label}] {c.title}: {c.text}" for c in t.candidates if c.label.startswith(s.label + ".")]
    return "\n".join(out)


def schema():
    pair = {"type": "object", "additionalProperties": False, "required": ["item", "candidate", "kind", "quote"],
            "properties": {"item": {"type": "string"}, "candidate": {"type": "string"},
                           "kind": {"type": "string", "enum": list(KINDS)}, "quote": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["links"],
            "properties": {"links": {"type": "array", "items": pair}}}


async def settle(t, ask):
    from .fill import _error, find_quote
    ans = await ask(prompt(t), "ekg_depends_on", schema())
    if (err := _error(ans)):
        return {"status": "error", "reason": err}
    items = {s.label: s for s in t.sources}
    cands = {c.label: c for c in t.candidates}
    kept, dropped = [], collections.Counter()
    for x in ans.get("links") or []:
        s, c = items.get(x.get("item")), cands.get(x.get("candidate"))
        if s is None or c is None or not c.label.startswith(s.label + "."):
            dropped["check: not one of that item's own candidates"] += 1
        elif x.get("kind") not in KINDS:
            dropped["check: a kind the registry does not name"] += 1
        elif find_quote(x.get("quote"), [Item(s.label, s.id, "", s.text)]) is None:
            dropped["check: quote not in the item's own words"] += 1
        else:
            kept.append({"a": s.id, "b": c.id, "kind": x["kind"], "quote": x["quote"]})
    if not kept:
        return {"status": "rejected", "reason": "llm: no item depends on its candidates" if not dropped else ", ".join(dropped),
                "dropped": dict(dropped)}
    return {"status": "accepted", "links": kept, "dropped": dict(dropped)}


def apply(w, t, e):
    problems = []
    for x in e["links"]:
        a, b = w.by_id[x["a"]], w.by_id[x["b"]]
        src = _source(a)
        why = w._link(LINK, a, b, [src] if src else [], "depends_on", "chosen", 1.0, kind=x["kind"])
        w.facts[f"{LINK} ({x['kind']}): " + ("written" if why is None else "not written, " + why[:80])] += 1
        if why:
            problems.append(why)
    return None if not problems else "; ".join(problems)[:200]
