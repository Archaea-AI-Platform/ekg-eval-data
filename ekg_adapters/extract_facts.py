"""Reading documents for everything the schema supports: the records a text names or states, and the links between them.

One call per window of a document (fill_tasks.doc_windows). The prompt's schema section is written from the
registry (extract_schema.Catalog), so a new type or link in the schema reaches the prompt without a change here.

The model only proposes. Code then:
  - checks every quote is word for word in the passage it names (R52), and that a named entity's name is in it too;
  - resolves every name against the graph (View.resolve: name, key, alias, email, login, and the records of the
    same identity). Exactly one record: use it. Several: the model is asked to choose among them (an
    `entity_choice` task, checked like any other gap), or the name is skipped and counted. None: a record is
    created only when its type may be created from a document (Catalog.creatable) and it validates;
  - checks each link's two ends against the registry, following a person to their one account (or back) when the
    link needs the other kind;
  - writes with method llm_judge, the quoted piece as evidence (R71), the origin link the schema requires (R41),
    `_stated` citations on every value, and a confidence from fill.CONFIDENCE.
What cannot be written is counted by reason in the report (`written.documents_and_tables`), never dropped silently.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import re

from . import common
from .extract_schema import LOOKUP_FIELDS, PERSONAL_NAME, placeholder
from .fill_tasks import Item, Task, doc_windows, name_key

DOCUMENTED_IN = "DOCUMENTED_IN"  # the registry's link from a record to the document that documents it (written besides its origin)
COVERAGE_ROUNDS = 2         # follow-up readings of what an answer left uncovered, at most this many per window
MIN_PARAGRAPH_CHARS = 80    # a shorter paragraph (a heading, a page number) is read with the one after it
MAX_CHOICES = 8  # more candidates than this for one name is not a question a passage can settle: skipped, counted
ASK = ("List every record and every link this text states, using only the record types and link types below.\n"
       "{catalog}\n\n"
       "RULES\n"
       "- records: everything of a listed type the text names (a person, a team, a role, a service, a topic, a ticket ...) "
       "or states (a decision, a rule, a requirement, a how-to, a task someone took on, an open question ...). "
       "Give each a ref (r1, r2 ...) of your own.\n"
       "- name: exactly as the text writes it (the full name, the key, the address). Empty for a type that is not named by a field.\n"
       "- statement: for a type that is \"stated in\" a field, one sentence in your own words saying it; else empty.\n"
       "- fields: only values the text gives for that type's listed fields; dates as YYYY-MM-DD. Leave out what it does not say.\n"
       "- quote: copy word for word, from ONE passage, the words (under 300 characters) that name or state it; "
       "source: that passage's label.\n"
       "- links: from and to are refs of records in your list. quote and source: the words that state the link "
       "itself, not just the two names near each other.\n"
       "- Report only what the text states. Skip navigation, boilerplate, and examples that only illustrate.\n"
       "- There is no limit on how many: report all of them. Empty lists are right when nothing qualifies.\n"
       'Reply with one JSON object: {{"records": [{{"ref", "type", "name", "statement", "fields": [{{"field", "value"}}], '
       '"quote", "source"}}], "links": [{{"type", "from", "to", "fields": [{{"field", "value"}}], "quote", "source"}}]}}')


def _header():
    from .fill import HEADER
    return HEADER


def overhead(catalog) -> int:
    """Characters of a doc_facts prompt that are not the document: instructions, the schema, the reply format."""
    return len(_header()) + len(ASK.format(catalog=catalog.describe())) + len(json.dumps(schema(catalog, ["P1"]))) + 400


def build(v):
    """One task per window of every document with text. A table piece a table_map task reads (v.claimed) is left
    out; one no mapping reads is read here as text."""
    from . import fill_docs
    cat = v.catalog()
    v.overhead.setdefault("doc_facts", overhead(cat))
    docs, skipped = v.docs()
    tasks, skipped = [], collections.Counter(skipped)
    for doc in docs:
        units = fill_docs.text_units(doc, v.claimed)
        if not units:
            skipped["only tables (read by table_map)"] += 1
            continue
        for ctx, items in doc_windows(v, "doc_facts", doc, units):
            tasks.append(Task("doc_facts", doc.rec["id"], f"Document: {doc.title}", "", items, [],
                              {"doc": doc.rec["id"], "quality": doc.quality, "room": v.room("doc_facts"), "title": doc.title},
                              context=ctx))
    return tasks, skipped


def prompt(t: Task, catalog, sources=None):
    from .fill import _text_block
    return "\n".join([_header(), ""] + _text_block(t, sources) + ["", ASK.format(catalog=catalog.describe())])


def schema(catalog, labels):
    pair = {"type": "object", "additionalProperties": False, "required": ["field", "value"],
            "properties": {"field": {"type": "string"}, "value": {"type": "string"}}}
    rec = {"type": "object", "additionalProperties": False,
           "required": ["ref", "type", "name", "statement", "fields", "quote", "source"],
           "properties": {"ref": {"type": "string"}, "type": {"type": "string", "enum": catalog.creatable + catalog.resolvable},
                          "name": {"type": "string"}, "statement": {"type": "string"},
                          "fields": {"type": "array", "items": pair}, "quote": {"type": "string"},
                          "source": {"type": "string", "enum": labels}}}
    link = {"type": "object", "additionalProperties": False, "required": ["type", "from", "to", "fields", "quote", "source"],
            "properties": {"type": {"type": "string", "enum": list(catalog.links)}, "from": {"type": "string"},
                           "to": {"type": "string"}, "fields": {"type": "array", "items": pair},
                           "quote": {"type": "string"}, "source": {"type": "string", "enum": labels}}}
    return {"type": "object", "additionalProperties": False, "required": ["records", "links"],
            "properties": {"records": {"type": "array", "items": rec}, "links": {"type": "array", "items": link}}}


def _check(got, catalog, first_part, records, links, dropped, fixed):
    """Hold each answered part to the text: quotes word for word in a passage shown (the passage the model named,
    or the one other passage that holds it), names in the text, types and links the schema offers. What passes is
    appended to records / links; what does not is counted in `dropped`."""
    from .fill import find_quote, locate
    known = set(catalog.creatable) | set(catalog.resolvable)
    for part, (shown, ans) in enumerate(got, first_part):
        by_label = {s.label: s for s in shown}
        refs = {}

        def where(x):
            src, moved = locate(x.get("quote"), by_label.get(x.get("source")), shown)
            if moved:
                fixed["quote found in another passage of the window: cited there"] += 1
            return src
        for x in ans.get("records") or []:
            if not isinstance(x, dict):
                continue
            rtype, name, statement = x.get("type"), (x.get("name") or "").strip(), (x.get("statement") or "").strip()
            src = where(x) if rtype in known else None
            if rtype not in known:
                dropped["check: not a type the schema offers here"] += 1
            elif src is None:
                dropped["check: quote not in its source"] += 1
            elif name and placeholder(name):
                dropped["check: placeholder name (-, n/a ...)"] += 1
            elif not name and not (statement and catalog.text_field(rtype)):
                dropped["check: record with no name and nothing stated"] += 1
            elif name and rtype not in catalog.stated and find_quote(name, [src]) is None:
                dropped["check: name not in its source"] += 1  # an entity is named by the text, never by the model
            else:
                ref = f"{part}:{x.get('ref')}"
                refs[ref] = True
                records.append({"ref": ref, "type": rtype, "name": name, "statement": statement, "quote": x["quote"],
                                "fields": [[f.get("field"), f.get("value")] for f in x.get("fields") or [] if isinstance(f, dict)],
                                "source_id": src.id, "label": src.label})
        for x in ans.get("links") or []:
            if not isinstance(x, dict):
                continue
            a, b = f"{part}:{x.get('from')}", f"{part}:{x.get('to')}"
            if x.get("type") not in catalog.links:
                dropped["check: not a link the schema offers here"] += 1
            elif a not in refs or b not in refs or a == b:
                dropped["check: link to a record that is not in the list (or was dropped)"] += 1
            elif (src := where(x)) is None:
                dropped["check: quote not in its source"] += 1
            else:
                links.append({"type": x["type"], "from": a, "to": b, "quote": x["quote"], "source_id": src.id, "label": src.label,
                              "fields": [[f.get("field"), f.get("value")] for f in x.get("fields") or [] if isinstance(f, dict)]})
    return first_part + len(got)


# ---------------------------------------------------------------- completeness, guaranteed by code

def paragraphs(item):
    """A passage's paragraphs (cut at blank lines). One shorter than MIN_PARAGRAPH_CHARS (a heading, a page
    number) belongs to the paragraph after it, so it is never re-read alone."""
    out, carry = [], ""
    for p in re.split(r"\n\s*\n", item.text or ""):
        p = p.strip()
        if not p:
            continue
        p = f"{carry}\n\n{p}" if carry else p
        if len(p) < MIN_PARAGRAPH_CHARS:
            carry = p
            continue
        out.append(p)
        carry = ""
    if carry:
        if out:
            out[-1] = f"{out[-1]}\n\n{carry}"
        else:
            out.append(carry)
    return out


def _covered(paras, quotes):
    """Which paragraphs an accepted quote sits in or spans into: each quote is found in the passage's text (as
    quotes are compared, fill._norm) and every paragraph its span overlaps is covered."""
    from .fill import _norm
    hit = set()
    for joined in ("", "-"):     # a line-end hyphen joined, or kept: as find_quote compares
        normed = [_norm(p, joined) for p in paras]
        starts, at = [], 0
        for p in normed:
            starts.append(at)
            at += len(p) + 1
        text = " ".join(normed)
        for q in quotes:
            q = _norm(q, joined).strip(" \"'“”‘’…").rstrip(".")
            i = text.find(q) if len(q) >= 3 else -1
            if i < 0:
                continue
            hit |= {k for k, s in enumerate(starts) if s < i + len(q) and i < s + len(normed[k])}
    return hit


def uncovered(sources, records, links):
    """[(passage, [paragraphs no accepted item quotes])] in reading order: what the answers so far left unread."""
    quotes = collections.defaultdict(list)
    for x in records + links:
        quotes[(x["label"], x["source_id"])].append(x["quote"])
    out = []
    for s in sources:
        paras = paragraphs(s)
        hit = _covered(paras, quotes[(s.label, s.id)])
        left = [p for k, p in enumerate(paras) if k not in hit]
        if left:
            out.append((s, left))
    return out


FOLLOW_UP = ("NOT COVERED YET: an earlier reading of this document listed nothing from the passages below. Read them "
             "again and list every record and link they state, by the rules below. If they hold nothing to keep (a "
             "heading, a page number, navigation, an illustration), reply with empty lists.")


def follow_up_prompt(t: Task, catalog, sources):
    from .fill import _text_block
    return "\n".join([_header(), ""] + _text_block(t, sources) + ["", FOLLOW_UP, "", ASK.format(catalog=catalog.describe())])


async def settle(t: Task, ask, catalog):
    """Ask one window, check the answer in code, then re-read exactly what yielded no accepted item: the passages
    (or the paragraphs of a long passage) no accepted quote sits in, in smaller follow-up calls, at most
    COVERAGE_ROUNDS times. What still yields nothing is recorded as read, nothing kept. Returns the ledger entry."""
    from . import fill_budget
    from .fill import _error, ask_split
    make_schema = lambda labels: schema(catalog, labels)
    got = await ask_split(ask, t.sources, lambda ss: prompt(t, catalog, ss), "ekg_doc_facts", make_schema)
    if isinstance(got, dict):
        return {"status": "error", "reason": _error(got)}
    records, links, dropped, fixed = [], [], collections.Counter(), collections.Counter()
    part = _check(got, catalog, 0, records, links, dropped, fixed)
    rounds = []
    room = max(t.write.get("room") or sum(len(s.text) for s in t.sources), 2000) // 2   # a follow-up is a smaller window
    for n in range(1, COVERAGE_ROUNDS + 1):
        left = uncovered(t.sources, records, links)
        if not left:
            break
        again = [Item(s.label, s.id, f"{s.title} (not covered yet)", "\n\n".join(ps), s.type) for s, ps in left]
        windows = fill_budget.pack(again, room, size=lambda it: len(it.title) + len(it.text) + 16)
        before = len(records) + len(links)
        answers = await asyncio.gather(*[ask_split(ask, w, lambda ss: follow_up_prompt(t, catalog, ss), "ekg_doc_facts", make_schema)
                                         for w in windows])
        failed = sum(1 for a in answers if isinstance(a, dict))
        for a in answers:
            if not isinstance(a, dict):
                part = _check(a, catalog, part, records, links, dropped, fixed)
        rounds.append({"round": n, "passages": len(again), "paragraphs": sum(len(ps) for _, ps in left),
                       "chars": sum(len(it.text) for it in again), "calls": len(windows), "calls_failed": failed,
                       "items_kept": len(records) + len(links) - before})
    left = uncovered(t.sources, records, links)
    with_items = {x["source_id"] + "|" + x["label"] for x in records + links}
    coverage = {"passages": len(t.sources), "passages_with_items": sum(1 for s in t.sources if f"{s.id}|{s.label}" in with_items),
                "paragraphs": sum(len(paragraphs(s)) for s in t.sources), "paragraphs_read_nothing_kept": sum(len(ps) for _, ps in left),
                "passages_read_nothing_kept": [s.label for s in t.sources if f"{s.id}|{s.label}" not in with_items],
                "chars": sum(len(s.text) for s in t.sources), "items": len(records) + len(links), "rounds": rounds}
    entry = {"dropped": dict(dropped), "coverage": coverage}
    if fixed:
        entry["fixed"] = dict(fixed)
    if not records:
        return {"status": "rejected", "reason": "llm: nothing stated" if not dropped else "check: " + ", ".join(dropped), **entry}
    return {"status": "accepted", "records": records, "links": links, **entry}


# ---------------------------------------------------------------- resolving names

_FAMILY = {}


def family(reg, rtype, identity):
    """The other record types one identity can also be known as (a Person: its Account, Alias, EmailAddress)."""
    if rtype not in _FAMILY:
        _FAMILY[rtype] = _family(reg, rtype, identity)
    return _FAMILY[rtype]


def _family(reg, rtype, identity):
    out = set()
    for name in identity:
        lk = reg.link(name)
        for mine, theirs in ((lk.from_types, lk.to_types), (lk.to_types, lk.from_types)):
            if any(reg.is_a(rtype, x) for x in mine if x != "any"):
                out |= {n for n in reg.records for x in theirs if x != "any" and reg.is_a(n, x)}
    return out - {rtype}


def find(w, name, rtype, tool):
    """Records the text may mean by `name` when it calls it a `rtype`: that type first, else the same identity
    under another type."""
    v = w.v
    v._index()
    same = v.resolve(name, {rtype}, tool)
    if same or is_name(rtype):
        return same
    return v.resolve(name, family(common._reg, rtype, v._identity), tool)


def is_name(rtype):
    """An address or an alias is a name FOR something, not the thing: one that is missing can simply be made,
    whoever else is already known by it."""
    return common._reg.is_a(rtype, "Alias")


def other_identity(w, rec, allowed, tool):
    """The record of an allowed type that is the same identity as `rec` (a person's one account), or None."""
    v = w.v
    v._index()
    seen, frontier, found = {rec["id"]}, [rec], []
    for _ in range(2):
        nxt = []
        for r in frontier:
            for lk in v.out[r["id"]] + v.inn[r["id"]]:
                other = lk["b"] if lk["a"] == r["id"] else lk["a"]
                if lk["type"] in v._identity and other not in seen:
                    seen.add(other)
                    nxt.append(v.rec[other])
        found = [r for r in nxt if allowed(r["type"])]
        if found:
            break
        frontier = nxt
    if len(found) > 1:
        same_tool = [r for r in found if r["props"].get("source_tool") == tool]
        found = same_tool if len(same_tool) == 1 else found
    return found[0] if len(found) == 1 else None


def _describe(v, r):
    p = r["props"]
    spec = common._reg.record(r["type"])
    from .extract_schema import LOOKUP_FIELDS, names_of
    names = [str(p[f]) for f in names_of(spec) if p.get(f)]
    lines = [f"{f}: {p[f]}" for f in [*LOOKUP_FIELDS, "title", "kind", "tool", "status", "native_id"] if p.get(f)]
    near = [f"{lk['type']} {v.rec[lk['b']]['type']} {_label(v.rec[lk['b']])}" for lk in v.out[r["id"]][:12]]
    near += [f"{v.rec[lk['a']]['type']} {_label(v.rec[lk['a']])} {lk['type']} this" for lk in v.inn[r["id"]][:6]]
    return f"{r['type']} {' / '.join(names) or p.get('native_id')}", "\n".join(lines + (["linked: " + "; ".join(near)] if near else []))


def _label(r):
    p = r["props"]
    return str(next((p[f] for f in ("full_name", "name", "title", "identifier", "address", "login", "display_name") if p.get(f)),
                    p.get("native_id")))


# ---------------------------------------------------------------- writing

def _props(w, cat, x, src, counts):
    """The stated values of one record mention, as props with their citations."""
    rtype, quote = x["type"], x["quote"]
    reg_fields, offered = common._reg.record(rtype).fields, cat.fields(rtype)
    props, stated = {}, {}

    def put(f, val, how="value"):
        props[f] = val
        stated[f] = {"value": val, "by": {src["id"]: {how: quote}}}

    nf, tf = cat.name_field(rtype), cat.text_field(rtype)
    if nf and x["name"]:
        put(nf, x["name"])
    if tf and (x["statement"] or (not nf and x["name"])):
        put(tf, x["statement"] or x["name"])
    from .extract_schema import coerce
    for f, val in x["fields"]:
        if f not in offered:
            counts["field the schema does not offer for this type: left out"] += 1
            continue
        got = coerce(reg_fields[f], val)
        if got is None:
            if str(val or "").strip():
                counts["field value that does not fit its type: left out"] += 1
            continue
        value, precision = got
        put(f, value, "day" if precision == "day" else "time" if reg_fields[f].type == "time" else "value")
        if precision:
            props[f"{f}_precision"] = precision
    if stated:
        props["_stated"] = stated
    return props


def apply(w, t: Task, e):
    """Write one window's checked records and links. Names that match several records become entity_choice
    tasks on w.pending; their links wait for the answer."""
    from .fill import find_quote
    cat, v = w.v.catalog(), w.v
    doc = w.by_id[t.subject]
    tool, quality, c = doc["props"]["source_tool"], t.write.get("quality", 1.0), w.facts
    items = {s.id: s for s in t.sources}
    state = {}   # ref -> {"rec", "how"} | {"wait": [...links]} | None
    for x in e["records"]:
        src = w.by_id[x["source_id"]]
        rtype, ref = x["type"], x["ref"]
        # a stated item (a decision, a rule ...) is an existing record only when the text itself writes that
        # record's name or key; a title the model worded is not an identity
        named = x["name"] and (rtype not in cat.stated or find_quote(x["name"], [s for s in t.sources if s.id == src["id"]]))
        found = []
        if rtype not in cat.stated:   # an email or an id the text gives (word for word) identifies it before its name
            shown = [s for s in t.sources if s.id == src["id"]]
            for f, val in x["fields"]:
                val = str(val or "").strip()
                if (f in LOOKUP_FIELDS or str(f).endswith("_id")) and _IDENTIFYING.search(val) and find_quote(val, shown):
                    found = v.by_field(rtype, f, val) if str(f).endswith("_id") else find(w, val, rtype, tool)
                    if found:
                        break
        found = found or (find(w, x["name"], rtype, tool) if named else [])
        if not found and named and rtype not in cat.stated and cat.name_field(rtype) == PERSONAL_NAME:
            found = v.by_name_prefix(x["name"], {rtype})   # "Lionel" -> the one "Lionel eng-orders" whose team is eng-orders
        bare = named and rtype not in cat.stated and thin(cat, rtype, x)
        if bare and not found:   # "Emma": the one record whose personal name starts with it, if there is one
            found = v.by_first_name(x["name"], {rtype})
        if len(found) == 1:
            state[ref] = {"rec": found[0], "how": "one"}
            c[f"{rtype}: resolved to the one existing record"] += 1
        elif len(found) > 1:
            if len(found) > MAX_CHOICES:
                state[ref] = None
                c[f"{rtype}: name matches more than {MAX_CHOICES} records, not linked"] += 1
                continue
            cands = []
            for i, r in enumerate(found):
                title, text = _describe(v, r)
                cands.append(Item(f"C{i + 1}", r["id"], title, text, r["type"]))
            item = items.get(src["id"]) or next(iter(items.values()))
            state[ref] = {"wait": [], "x": x}
            w.pending.append(Task("entity_choice", t.subject, f"Which {rtype} does this text mean by \"{x['name']}\"?",
                                  "the text itself makes clear which one it is (a full name, an address, a team, a role or a "
                                  "date only one of them fits). If it could be more than one of them, reject.",
                                  [Item("S1", item.id, item.title, item.text, item.type)], cands,
                                  {"state": state, "ref": ref, "window": t, "quality": quality}))
            c[f"{rtype}: name matches several records, the model is asked to choose"] += 1
            continue
        elif rtype not in cat.creatable:
            state[ref] = None
            c[f"{rtype}: no such record in the graph (this type is not created from a document)"] += 1
            continue
        elif bare:
            state[ref] = None
            c[f"{rtype}: named too thinly to create (one bare name; no full name, email, login or id)"] += 1
            continue
        else:
            state[ref] = _create(w, cat, t, x, src, doc, quality)
        if state[ref] and state[ref]["how"] != "new":
            _mention(w, t, state[ref], src, quality)
    for l in e["links"]:
        ends = [state.get(l["from"]), state.get(l["to"])]
        waiting = [s for s in ends if s and "wait" in s]
        if any(s is None for s in ends):
            c[f"{l['type']}: an end could not be resolved, not linked"] += 1
        elif waiting:
            for s in waiting:
                s["wait"].append(l)
        else:
            _write_link(w, t, l, ends[0], ends[1], tool, quality)
    return None


_IDENTIFYING = re.compile(r"[\d@._]")  # a value with one of these is an id, a login or an address, not a bare name


def thin(cat, rtype, x):
    """Is this mention of a person-like record (one named by a personal name, PERSONAL_NAME in the registry) too
    thin to create a record from: one bare word ("Emma"), and nothing else that identifies it? Identifying is a
    full name (two or more words), or an email, login or id (a value with a digit, @, . or _), in the name itself
    or in a stated field the record is looked up or keyed by (LOOKUP_FIELDS, a field ending in _id)."""
    from .extract_schema import LOOKUP_FIELDS, PERSONAL_NAME
    if cat.name_field(rtype) != PERSONAL_NAME:
        return False

    def identifies(val):
        s = str(val or "").strip()
        return len(s.split()) > 1 or bool(_IDENTIFYING.search(s))
    if identifies(x["name"]):
        return False
    return not any(identifies(val) for f, val in x["fields"] if f in LOOKUP_FIELDS or str(f).endswith("_id"))


def _create(w, cat, t, x, src, doc, quality):
    """A new record for a mention nothing in the graph answers to; returns its state, or None (counted)."""
    from .fill import DEFAULT_STATUS
    rtype, c = x["type"], w.facts
    stated = rtype in cat.stated
    if not stated and src["type"] != "Piece":
        c[f"{rtype}: not created, the document has no piece to cite it from"] += 1
        return None
    props = _props(w, cat, x, src, c)
    if rtype in DEFAULT_STATUS and not props.get("status"):   # the text states none: the knowledge writer's default
        props["status"] = DEFAULT_STATUS[rtype]
    native = "llm:" + hashlib.sha1((f"{src['id']}|{x['quote']}" if stated else name_key(x["name"])).encode()).hexdigest()[:16]
    rec, why = w.new_record(rtype, "derived", native, props, src)
    if why:
        c[f"{rtype}: not created, {why}"] += 1
        return None
    st = {"rec": rec, "how": "new"}
    if stated:  # its origin (R41): the first origin link the registry allows, at the passage or else its document
        origin = cat.origin_for(rtype, [src["type"], doc["type"]])
        target = src if origin and origin[1] == src["type"] else doc
        why = f"schema: no origin link from a {rtype} to a {doc['type']}" if not origin else \
            w._link(origin[0], rec, target, [src["id"]], t.kind, "new", quality)
    else:
        why = w._link("MENTIONS", src, rec, [src["id"]], t.kind, "new", quality, kind="name")
    if why:
        w.drop_record(rec)
        c[f"{rtype}: not created, {why[:100]}"] += 1
        return None
    c[f"{rtype}: created"] += 1
    if stated and (not origin or origin[0] != DOCUMENTED_IN):
        # where the registry lets a record say which document documents it (Knowledge DOCUMENTED_IN its File)
        target = next((r for r in (doc, _section(w, src)) if r and common._reg.link_allows(DOCUMENTED_IN, rtype, r["type"])), None)
        if target is not None:
            why = w._link(DOCUMENTED_IN, rec, target, [src["id"]], t.kind, "new", quality)
            c[f"{rtype}: {DOCUMENTED_IN} {'written' if why is None else 'not written, ' + why[:100]}"] += 1
    return st


def _section(w, piece):
    """The DocSection a piece is PART_OF, or None."""
    return next((w.by_id[lk["b"]] for lk in w.v.out[piece["id"]] if lk["type"] == "PART_OF" and w.by_id[lk["b"]]["type"] == "DocSection"), None)


def _mention(w, t, st, src, quality):
    """The passage MENTIONS the record it names (the registry allows this from a Piece to any type)."""
    if src["type"] == "Piece" and st["rec"]["id"] != src["id"]:
        w._link("MENTIONS", src, st["rec"], [src["id"]], t.kind, st["how"], quality, kind="name")


def _write_link(w, t, l, a, b, tool, quality):
    cat, reg, c = w.v.catalog(), common._reg, w.facts
    ltype = l["type"]
    ra, rb = a["rec"], b["rec"]
    if not reg.link_allows(ltype, ra["type"], rb["type"]):
        # the right identity, the wrong kind of record for this link: follow it to its one account (or person)
        lk = reg.link(ltype)
        if not any(reg.is_a(ra["type"], x) for x in lk.from_types):
            ra = other_identity(w, ra, lambda ty: any(reg.is_a(ty, x) for x in lk.from_types), tool)
        if ra is not None and not any(reg.is_a(rb["type"], x) for x in lk.to_types):
            rb = other_identity(w, rb, lambda ty: any(reg.is_a(ty, x) for x in lk.to_types), tool)
        if ra is None or rb is None:
            c[f"{ltype}: the schema does not allow it between a {a['rec']['type']} and a {b['rec']['type']}, not linked"] += 1
            return
    if ra["type"] not in cat.creatable and rb["type"] not in cat.creatable:
        c[f"{ltype}: between two tool records, left to their adapters"] += 1
        return
    own, offered = {}, cat.link_fields(ltype)
    for f, val in l["fields"]:
        if f in offered and str(val).strip() in offered[f]:
            own[f] = str(val).strip()
        elif str(val or "").strip():
            c["link field the schema does not offer or value not allowed: left out"] += 1
    how = min((a["how"], b["how"]), key=["chosen", "new", "one"].index)
    why = w._link(ltype, ra, rb, [l["source_id"]], t.kind, how, quality, **own)
    c[f"{ltype}: {'written' if why is None else 'not written, ' + why[:100]}"] += 1


def apply_choice(w, choice: Task, e):
    """The model chose one of several records for a name: use it, and write the links that waited for it."""
    st = choice.write["state"][choice.write["ref"]]
    t, quality = choice.write["window"], choice.write["quality"]
    rec = w.by_id[next(cand.id for cand in choice.candidates if cand.label == e["choice"])]
    waited = st.pop("wait", [])
    x = st.pop("x")
    st.update(rec=rec, how="chosen")
    tool = w.by_id[t.subject]["props"]["source_tool"]
    _mention(w, t, st, w.by_id[x["source_id"]], quality)
    state = choice.write["state"]
    for l in waited:
        a, b = state.get(l["from"]), state.get(l["to"])
        if a and b and "wait" not in a and "wait" not in b:
            if not l.get("_done"):
                l["_done"] = True
                _write_link(w, t, l, a, b, tool, quality)
    return None


def not_chosen(w, choice: Task):
    """After the choices are in: a name the model could not settle stays unlinked, and is counted."""
    st = choice.write["state"].get(choice.write["ref"])
    if st and "wait" in st:
        choice.write["state"][choice.write["ref"]] = None
        w.facts[f"{st['x']['type']}: several records match and the text does not say which, not linked"] += 1
        for l in st["wait"]:
            if not l.get("_done"):
                l["_done"] = True
                w.facts[f"{l['type']}: an end could not be resolved, not linked"] += 1
