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
       "- type: the listed type whose description fits what the text says, whatever the section is called. Something "
       "a product, a system or a change has to do or must not do is a Requirement; a Guideline is a rule for how the "
       "team itself works. An item that a requirements document numbers with an id of its own (as the passage's "
       "heading or the item's first words show) is a Requirement.\n"
       "- name: exactly as the text writes it (the full name, the key, the address). Empty for a type that is not named "
       "by a field. For a stated item the text gives an id of its own (a requirement's id), the name starts with that "
       "id as written, then a few words.\n"
       "- statement: for a type that is \"stated in\" a field, the item whole, as the text states it: every clause, "
       "condition and exception, and every number, time, date, amount, id and code exactly as written (copy the "
       "text's own words; never shorten it to a summary); else empty.\n"
       "- one record per item: a passage that states several facts, rules, values, steps or table rows gives one "
       "record for each of them; never fold several into one summary record.\n"
       "- fields: only values the text gives for that type's listed fields; dates as YYYY-MM-DD. Leave out what it does not say.\n"
       "- quote: copy word for word, from ONE passage, the words (the whole sentence, list item or table row, under "
       "600 characters) that name or state it; "
       "source: that passage's label.\n"
       "- links: every relation the text states between two records in your list, in whatever words (X is owned "
       "by Y, Y is responsible for X, A depends on B, the contact for Z is W, a requirement is about a service or "
       "an area ...), as the listed link type whose "
       "description says that, in the direction its description gives. from and to are refs of records in your "
       "list. quote and source: the words that state the link itself, not just the two names near each other.\n"
       "- A text that speaks for a team (\"we\", \"our team\") states facts about the team the document is by, as its "
       "title or first heading names it: list that team by that name, and link what the text says the team owns, runs, "
       "decides or is the contact for, by the link type whose description says so.\n"
       "- A thing the text refers to by an id or a proper name of its own (a case, an order, a programme ...) for which "
       "no more specific listed type fits is a Topic named by that id alone, or that name, as written "
       "(a heading is a place in the document, not a Topic). List it "
       "wherever it is mentioned and link the items about it to it (ABOUT): what the text recounts happening to it (an "
       "incident, with its times) is a record of its own listed type, ABOUT it.\n"
       "- The document itself is a record too: when the text says who owns, maintains or wrote this document, list it "
       "with type File and the document's title exactly as shown after DOCUMENT:, and link it.\n"
       "{worth}\n"
       "- Report only what the text states. Skip navigation, licence and copyright boilerplate, and sample output that "
       "only illustrates; a passage that shows how to do something (a command and what it does) is a how-to, not an "
       "illustration.\n"
       "- There is no limit on how many: report all of them. Empty lists are right when nothing qualifies.\n"
       'Reply with one JSON object: {{"records": [{{"ref", "type", "name", "statement", "fields": [{{"field", "value"}}], '
       '"quote", "source"}}], "links": [{{"type", "from", "to", "fields": [{{"field", "value"}}], "quote", "source"}}]}}')


def worth_keeping(catalog) -> str:
    """What counts as worth keeping in prose, from the registry: each type a text may state (Catalog.stated) with the
    first sentence of the registry's description, and the meaning of each of its kinds where the registry lists
    them (Knowledge: how_to, rationale, gotcha ...). Not one word of it is written here."""
    from .extract_schema import first_sentence
    rows = []
    for t in catalog.stated:
        rt = catalog.reg.record(t)
        about = first_sentence(rt.about or rt.description or "").rstrip(";.")
        kind = rt.fields.get("kind")
        line = f"  {t}: {about}" + (f". Its kinds: {kind.about}" if kind is not None and kind.enum and kind.about else "")
        rows.append(line)
    out = ("- WORTH KEEPING: explanatory prose holds these as much as numbered lists do. Every statement of one of "
           "the types below is an item, wherever it is written (a paragraph, a list item, a note under a heading), "
           "even when nothing numbers it or calls it a rule or a requirement:\n" + "\n".join(rows))
    from .extract_schema import GRANT_LINK
    if GRANT_LINK in catalog.links:   # the registry lets a document describe access (extract_schema's docstring)
        scopes = " or ".join(t for t in catalog.links[GRANT_LINK][1] if t in catalog.access_types)
        out += (f"\n- ACCESS: what a group, role or account may reach (read, write, review, approve ...), as the text "
                f"states it, is a {GRANT_LINK} link from it to a {scopes}: the one the text names, else one named by the "
                "words that say what is reached, with the text's words about it (what it covers, what it does not) as "
                "its text. Its level is the listed value for what the text says may be done there, in the text's own words "
                "(to read or view, to comment, to write, change or push, to administer); leave the level out only when "
                "the text says nothing about what may be done.")
    return out


def _ask(catalog):
    return ASK.format(catalog=catalog_text(catalog), worth=worth_keeping(catalog))


def _header():
    from .fill import HEADER
    return HEADER


def catalog_text(catalog):
    """The document prompt's schema section, with as many of the registry's descriptions as the budget allows."""
    from . import fill
    return catalog.schema_text("documents", (getattr(catalog, "budget", None) or fill.BUDGET).schema_chars)[0]


def overhead(catalog) -> int:
    """Characters of a doc_facts prompt that are not the document: instructions, the schema, the reply format."""
    return len(_header()) + len(_ask(catalog)) + len(json.dumps(schema(catalog, ["P1"]))) + 400


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
                              {"doc": doc.rec["id"], "quality": doc.quality, "room": v.room("doc_facts"), "title": doc.title,
                               "doc_type": doc.rec["type"]},
                              context=ctx))
    return tasks, skipped


def prompt(t: Task, catalog, sources=None):
    from .fill import _text_block
    return "\n".join([_header(), ""] + _text_block(t, sources) + ["", _ask(catalog)])


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


def _check(got, catalog, first_part, records, links, dropped, fixed, title=None, doc_type=None):
    """Hold each answered part to the text: quotes word for word in a passage shown (the passage the model named,
    or the one other passage that holds it), names in the text, types and links the schema offers. What passes is
    appended to records / links; what does not is counted in `dropped`. The document itself may be listed by its
    title as shown (`title`) as a record of its own type (`doc_type`), which its passages need not write."""
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
            elif name and rtype not in catalog.stated and find_quote(name, [src]) is None and \
                    not (title and doc_type and common._reg.is_a(doc_type, rtype) and name_key(name) == name_key(title)):
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
    quotes are compared, fill._norm) and every paragraph its span overlaps is covered. A quote that joins
    several sentences (fill.quote_parts) covers the paragraph of each of them."""
    from .fill import _LABEL, _JOINS, _norm
    hit = set()
    for joined in ("", "-"):     # a line-end hyphen joined, or kept: as find_quote compares
        normed = [_norm(p, joined) for p in paras]
        starts, at = [], 0
        for p in normed:
            starts.append(at)
            at += len(p) + 1
        text = " ".join(normed)
        clean = lambda s: _norm(s, joined).strip(" \"'“”‘’…").rstrip(".")
        for quote in quotes:
            quote = _LABEL.sub("", quote or "")
            whole = clean(quote)
            spans = [whole] if len(whole) >= 3 and whole in text else \
                [p for p in (clean(x) for x in _JOINS.split(quote) if x.strip()) if len(p) >= 3]
            for q in spans:
                i = text.find(q)
                if i >= 0:
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
             "again and list every record and link they state, by the rules below. A paragraph that explains how "
             "something works or is done, why it is so, what to watch out for, what is recommended or what was "
             "decided is worth keeping (see WORTH KEEPING), whether or not it is numbered. Only when they hold nothing "
             "to keep (a heading, a page number, navigation, sample output) reply with empty lists.")


def follow_up_prompt(t: Task, catalog, sources):
    from .fill import _text_block
    return "\n".join([_header(), ""] + _text_block(t, sources) + ["", FOLLOW_UP, "", _ask(catalog)])


async def settle(t: Task, ask, catalog):
    """Ask one window, check the answer in code, then re-read exactly what yielded no accepted item: the passages
    (or the paragraphs of a long passage) no accepted quote sits in, in smaller follow-up calls, at most
    COVERAGE_ROUNDS times. What still yields nothing is recorded as read, nothing kept. Returns the ledger entry."""
    from . import fill_budget
    from .fill import _error, ask_split, unanswered
    make_schema = lambda labels: schema(catalog, labels)
    got, failed_first = await ask_split(ask, t.sources, lambda ss: prompt(t, catalog, ss), "ekg_doc_facts", make_schema)
    if not got:
        return {"status": "error", "reason": _error(failed_first[0][1])}
    records, links, dropped, fixed = [], [], collections.Counter(), collections.Counter()
    title, doc_type = t.write.get("title"), t.write.get("doc_type")
    part = _check(got, catalog, 0, records, links, dropped, fixed, title, doc_type)
    rounds = []
    room = max(t.write.get("room") or sum(len(s.text) for s in t.sources), 2000) // 2   # a follow-up is a smaller window
    unread = {s.label for ss, _ in failed_first for s in ss}   # passages no answer covers: not re-read as "nothing kept"
    for n in range(1, COVERAGE_ROUNDS + 1):
        left = [(s, ps) for s, ps in uncovered(t.sources, records, links) if s.label not in unread]
        if not left:
            break
        again = [Item(s.label, s.id, f"{s.title} (not covered yet)", "\n\n".join(ps), s.type) for s, ps in left]
        windows = fill_budget.pack(again, room, size=lambda it: len(it.title) + len(it.text) + 16)
        before = len(records) + len(links)
        answers = await asyncio.gather(*[ask_split(ask, w, lambda ss: follow_up_prompt(t, catalog, ss), "ekg_doc_facts", make_schema)
                                         for w in windows])
        failed = sum(len(f) for _, f in answers)
        for a, _ in answers:
            part = _check(a, catalog, part, records, links, dropped, fixed, title, doc_type)
        kept = len(records) + len(links) - before
        rounds.append({"round": n, "passages": len(again), "paragraphs": sum(len(ps) for _, ps in left),
                       "chars": sum(len(it.text) for it in again), "calls": len(windows), "calls_failed": failed,
                       "items_kept": kept})
        if kept == 0:   # the next round would send the very same passages again: its answer is this one's
            break
    left = [(s, ps) for s, ps in uncovered(t.sources, records, links) if s.label not in unread]
    with_items = {x["source_id"] + "|" + x["label"] for x in records + links}
    coverage = {"passages": len(t.sources), "passages_with_items": sum(1 for s in t.sources if f"{s.id}|{s.label}" in with_items),
                "paragraphs": sum(len(paragraphs(s)) for s in t.sources), "paragraphs_read_nothing_kept": sum(len(ps) for _, ps in left),
                "passages_read_nothing_kept": [s.label for s in t.sources if f"{s.id}|{s.label}" not in with_items and s.label not in unread],
                "passages_unanswered": sorted(unread, key=lambda s: (len(s), s)),
                "chars": sum(len(s.text) for s in t.sources), "items": len(records) + len(links), "rounds": rounds}
    entry = {"dropped": dict(dropped), "coverage": coverage, **unanswered(failed_first)}
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
        put(tf, whole_statement(x["statement"] or x["name"], quote) if rtype in cat.stated else x["statement"] or x["name"])
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


_WORD = re.compile(r"\w+")


def whole_statement(statement, quote):
    """What a stated record (a requirement, a rule, a fact ...) says: the model's statement when it keeps every word
    of the quote it was checked against (word for word in the source), else the quote itself. A statement worded
    by the model can drop a clause or an exact value ("regardless of when payment clears", "04:00 UTC"); the
    source's own words cannot. A quote shorter than the statement is only part of it: the statement stays. The quote's leading id (the item's own number, which the record's name carries) is
    left out of the comparison and of the statement. Words are compared as the text's letters and digits, case
    apart, so this holds in any language."""
    quote = re.sub(r"\s+", " ", str(quote or "")).strip()
    if (m := ITEM_ID.match(quote)):
        quote = quote[m.end():].lstrip(" :.-–—|/")
    words = lambda s: collections.Counter(_WORD.findall(str(s or "").casefold()))
    said, quoted = words(statement), words(quote)
    if not quote or not quoted - said or sum(quoted.values()) < sum(said.values()):
        return statement   # it keeps the quote whole, or the quote is only a part of what it says
    return quote


def fill_fields(w, cat, rec, x, src, shown):
    """A text that states a value of an existing record's field (a person's status, a service's owning team) gives
    the record that value when it has none yet: a value it already has is never changed (a different one is
    counted, not written). Held like a quote: each value must be word for word in the passage (`shown`); a field
    that identifies a record (its name, a lookup field, an id, a login or address) is never given this way, since
    identity comes from the record's own source; and the passage's scope must be one the record already carries
    (every access clause of the passage is among the record's), so no text reaches a record more people can see.
    Each value keeps its citation (_stated), and the record must still validate."""
    from .fill import find_quote
    c = w.facts
    if any(clause not in rec["scope"] for clause in src["scope"]):
        c[f"{rec['type']}: stated fields not given, the passage is visible to fewer people than the record"] += 1
        return
    identity = {cat.name_field(rec["type"]), cat.text_field(rec["type"]), *LOOKUP_FIELDS}
    ok = []
    for f, val in x["fields"]:
        if f in identity or str(f).endswith("_id") or f in ("login", "address", "emails", "email", "full_name", PERSONAL_NAME):
            c[f"{rec['type']}: an identifying field ({f}) is not given from a text"] += 1
        elif not str(val or "").strip() or find_quote(str(val), shown) is None:
            c[f"{rec['type']}: a stated field value not word for word in its passage, not given"] += 1
        else:
            ok.append([f, val])
    if not ok:
        return
    new = _props(w, cat, {**x, "name": "", "statement": "", "fields": ok}, src, c)
    stated = new.pop("_stated", {})
    p = rec["props"]
    gain = {f: new[f] for f in stated if f not in p}
    gain.update({f"{f}_precision": new[f"{f}_precision"] for f in list(gain) if f"{f}_precision" in new})
    for f in stated:
        if f in p and p[f] != new[f]:
            c[f"{rec['type']}: a stated {f} differs from the one it has, not changed"] += 1
    if not gain:
        return
    before = json.dumps(p)
    p.update(gain)
    p["_stated"] = {**p.get("_stated", {}), **{f: stated[f] for f in gain if f in stated}}
    if not common._vrec(rec).ok:
        rec["props"] = json.loads(before)
        c[f"{rec['type']}: stated fields not given, the record would not validate"] += 1
        return
    if p.get("_key") in w.old_records:
        w.patched.add(p["_key"])
    c[f"{rec['type']}: stated fields given to the existing record"] += 1


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
        if rtype == HUB_TYPE and (m := ITEM_ID.match(x["name"])) and m.end() < len(x["name"]):
            x = {**x, "name": m.group(0)}   # "O-8201 / early capture": the thing is named by its id alone
        named = x["name"] and (rtype not in cat.stated or find_quote(x["name"], [s for s in t.sources if s.id == src["id"]]))
        found = []
        if x["name"] and t.write.get("title") and name_key(x["name"]) == name_key(t.write["title"]) \
                and common._reg.is_a(doc["type"], rtype):   # the document itself, by its title as shown
            found = [doc]
            c[f"{rtype}: the document itself, by its title"] += 1
        if rtype not in cat.stated and not found:   # an email or an id the text gives (word for word) identifies it first
            shown = [s for s in t.sources if s.id == src["id"]]
            for f, val in x["fields"]:
                val = str(val or "").strip()
                if (f in LOOKUP_FIELDS or str(f).endswith("_id")) and _IDENTIFYING.search(val) and find_quote(val, shown):
                    found = v.by_field(rtype, f, val) if str(f).endswith("_id") else find(w, val, rtype, tool)
                    if found:
                        break
        found = found or (find(w, x["name"], rtype, tool) if named else [])
        if any(w.tool_access_record(r) for r in found):   # a tool's own place is never what a document names
            found = [r for r in found if not w.tool_access_record(r)]
            c[f"{rtype}: a tool's own place of that name left alone (a document describes its own scope)"] += 1
        # a numbered item stated again in the same document (its own id written in the passage: a follow-up reading
        # of a paragraph, or the next window) is the record already made for it, never a second one
        item_key = None
        if not found and rtype in cat.stated and (m := ITEM_ID.match(x["name"]))                 and find_quote(m.group(0), [s for s in t.sources if s.id == src["id"]]):
            item_key = (t.subject, rtype, m.group(0))
            if item_key in w.stated_items:
                found = [w.stated_items[item_key]]
                c[f"{rtype}: the same numbered item again in its document, the record made for it"] += 1
        if not found and named and rtype not in cat.stated:   # "payments-platform": the one "acme-commerce/payments-platform"
            found = v.by_name_tail(x["name"], {rtype})
        if not found and named and rtype not in cat.stated and cat.name_field(rtype) == PERSONAL_NAME:
            found = v.by_name_prefix(x["name"], {rtype})   # "Lionel" -> the one "Lionel eng-orders" whose team is eng-orders
        bare = named and rtype not in cat.stated and thin(cat, rtype, x)
        if bare and not found:   # "Emma": the one record whose personal name starts with it, if there is one
            found = v.by_first_name(x["name"], {rtype})
        if rtype == HUB_TYPE and not found and named:
            others = _named_as_other(v, x["name"], rtype)   # existing records first
            if len(others) == 1:   # "Order Management": the team of that name, not a topic beside it
                found = others
                c[f"{rtype}: the name is another record's: that record"] += 1
            elif others:
                state[ref] = None
                c[f"{rtype}: not created, the name is that of several other records"] += 1
                continue
            elif name_key(x["name"]) in _headings(t.sources, _outline(w, v, doc)):
                state[ref] = None
                c[f"{rtype}: not created, the name is a heading of the document (a place in it, not a thing it names)"] += 1
                continue
        if len(found) == 1:
            state[ref] = {"rec": found[0], "how": "one"}
            c[f"{rtype}: resolved to the one existing record"] += 1
            if rtype not in cat.stated and x["fields"]:
                fill_fields(w, cat, found[0], x, src, [s for s in t.sources if s.id == src["id"]])
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
        elif (why := not_a_personal_name(v, cat, rtype, x["name"])):
            state[ref] = None
            c[f"{rtype}: not created, {why}"] += 1
            continue
        else:
            state[ref] = _create(w, cat, t, x, src, doc, quality)
            if state[ref] and item_key:
                w.stated_items[item_key] = state[ref]["rec"]
        if state[ref] and state[ref]["how"] != "new" and state[ref]["rec"]["id"] != doc["id"]:
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


HUB_TYPE = "Topic"   # the registry's record for a subject things hang on: what a thing with an id or name of its own but no
                     # more specific type is made (a case, an order, a programme), so its mentions in any document meet


_NUMBERED = re.compile(r"^\s*\d+(?:\.\d+)*[.)]\s+")   # a section's own number before its heading: "6. ", "2.1) "


def _unnumbered(text):
    return name_key(_NUMBERED.sub("", str(text or "")))


def _outline(w, v, doc):
    """The document's headings, from its outline (fill_docs.Doc), at every level. A document read without one is
    counted: its other sections' headings are then not known here."""
    docs, _ = v.docs()
    found = [d for d in docs if d.rec["id"] == doc["id"]]
    if not found:
        w.facts["documents whose outline could not be found (Topic names checked against the passages' headings only)"] += 1
    return [h for d in found for _, h in d.outline]


def _headings(sources, outline=()):
    """The headings of the document (its outline) and of the passages shown, each level of each heading path, as name
    keys, as written and without a section's own number ("6. Carrier release" gives both): a name that is one of them
    is a place in the document, not a thing the text names. Only the headings lose their number, never the name
    checked against them (a name may start with a year or a count)."""
    out = {k for heading in outline for h in str(heading).split(" > ") for k in (name_key(h), _unnumbered(h))}
    for s in sources:
        title = re.sub(r"\s*\((?:[^()]*)\)$", "", str(s.title or ""))   # "... (table)", "(part 1/2 of this passage)"
        title = re.sub(r"\s*\((?:[^()]*)\)$", "", title)
        out |= {k for h in (title, *title.split(" > ")) for k in (name_key(h), _unnumbered(h))}
    return out - {""}


def _named_as_other(v, name, rtype):
    """The records of another kind of named thing (not `rtype`, not a stated item, not a document part) that the graph
    knows by exactly this name."""
    cat = v.catalog()
    kinds = (set(cat.creatable) | set(cat.resolvable)) - set(cat.stated) - {rtype}
    v._index()
    return [v.rec[i] for i in dict.fromkeys(v._names.get(name_key(name), ())) if v.rec[i]["type"] in kinds]


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


def not_a_personal_name(v, cat, rtype, name):
    """Why `name` cannot be the personal name a new person-like record (one named by PERSONAL_NAME) is made with, or
    None. The registry names such a record by a human's name; a phrase is not one when it holds a mark no name has
    (PERSON_NOISE: & / ; @ ...), or when the graph knows it, or a run of its words, as another kind of record or as a
    job title a person holds (View.named_otherwise): "Shipment & Operations Product Manager" is a role, not a person.
    Decided by the text's structure and the graph's records, never by a list of words."""
    from .extract_schema import more_than_a_name
    if cat.name_field(rtype) != PERSONAL_NAME or not name:
        return None
    if more_than_a_name(name):
        return "the name holds a mark no personal name has (a role or a list, not a person)"
    v._index()
    other = v.named_otherwise(name, {rtype} | family(common._reg, rtype, v._identity))
    return f"the name is or holds {other} (a role, not a person)" if other else None


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
        hit = next((x for x in offered.get(f, ()) if x.casefold() == str(val or "").strip().casefold()), None)
        if hit is not None:   # a listed value, whatever its letter case ("Write" is write)
            own[f] = hit
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


# ---------------------------------------------------------------- ids one record cites of another (code only)

# an item's id as documents number them: capitals and digits, dash-joined, ending in a number (PAY-12, ORD-OM-03)
ITEM_ID = re.compile(r"(?<![\w-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+(?![\w-])")
REFERENCE_RELATION = "REFERENCES"   # the registry's relation of a link that says "A refers to B" (LINKS_TO ...)


def own_id(rec, cat):
    """The id a stated record (a requirement, a decision ...) carries as its own: the id its name starts with,
    else the one its statement starts with. None when it has none."""
    p = rec["props"]
    for f in (cat.name_field(rec["type"]), cat.text_field(rec["type"])):
        m = ITEM_ID.match(str(p.get(f) or "").strip()) if f else None
        if m:
            return m.group(0)
    return None


def _quotes(rec):
    """(source id, quoted words) of every value a record's _stated citations hold."""
    for st in (rec["props"].get("_stated") or {}).values():
        for src, by in (st.get("by") or {}).items():
            for q in (by or {}).values():
                if isinstance(q, str) and q:
                    yield src, q


def _reference_link(reg, a_type, b_type):
    """A link of the registry's reference relation that joins these two types directly, else None."""
    return next((n for n, lk in sorted(reg.links.items()) if lk.relation == REFERENCE_RELATION and not lk.internal
                 and reg.link_allows(n, a_type, b_type)), None)


def link_cited_ids(w):
    """After every document is read: a stated record whose own words (its quote, its statement) write another
    record's own id (own_id) cites that record, in any document. Code only, no model call. The link is a
    reference link the registry allows between the two records when it has one; else the cited record is linked
    from the passage the citing record was quoted from (a Piece LINKS_TO any type), the citing record's quote as
    evidence. An id that several records carry is not linked (counted)."""
    cat, reg = w.v.catalog(), common._reg
    stated = set(cat.stated)
    recs = [r for r in w.g.records.values() if r["type"] in stated]
    by_id = collections.defaultdict(list)
    for r in recs:
        if (i := own_id(r, cat)):
            by_id[i].append(r)
    out = collections.Counter()
    out["records with an id of their own"] = sum(len(rs) for rs in by_id.values())
    out["ids that several records carry (one per document, not linked)"] = sum(1 for rs in by_id.values() if len(rs) > 1)
    done = set()
    for r in recs:
        mine = own_id(r, cat)
        texts = [(src, q) for src, q in _quotes(r)] + [(None, str(r["props"].get(cat.text_field(r["type"]) or "") or ""))]
        for src, text in texts:
            for cited in dict.fromkeys(ITEM_ID.findall(text)):
                if cited == mine or (r["id"], cited) in done:
                    continue
                done.add((r["id"], cited))
                targets = [x for x in by_id.get(cited, []) if x["id"] != r["id"]]
                if not targets:
                    out["cited ids no record carries"] += 1
                    continue
                if len(targets) > 1:
                    out["cited ids several records carry: not linked"] += 1
                    continue
                b = targets[0]
                if _runs_into(text, b, cited):
                    out["the quote runs on into the cited item's own words (two items quoted together): not a citation"] += 1
                    continue
                src = src if src in w.by_id else next((s for s, _ in _quotes(r) if s in w.by_id), None)
                ltype = _reference_link(reg, r["type"], b["type"])
                a = r
                if ltype is None and src is not None:   # from the passage the citing record was quoted from
                    a = w.by_id[src]
                    ltype = _reference_link(reg, a["type"], b["type"])
                if ltype is None:
                    out[f"no reference link from a {r['type']} (or its passage) to a {b['type']}"] += 1
                    continue
                if a["id"] == b["id"]:
                    continue
                why = w._link(ltype, a, b, [src] if src else [], "id_reference", "reference", 1.0, method="text_rule")
                out[f"{a['type']} {ltype} {b['type']}: " + ("written" if why is None else "not written, " + why[:80])] += 1
    return dict(out)


def _runs_into(text, b, cited):
    """Does `text`, where it writes the cited id, go on with record b's own quoted words: a quote that ran on from
    one numbered item into the next one, rather than one item citing another? Compared over the words both have
    (at least 12 characters, at most 40)."""
    from .fill import _norm
    t, key = _norm(text), _norm(cited)
    heads = [_norm(q) for _, q in _quotes(b)]
    at = t.find(key)
    while at >= 0:
        tail = t[at:]
        for q in heads:
            n = min(len(tail), len(q), 40)
            if n >= 12 and tail[:n] == q[:n]:
                return True
        at = t.find(key, at + 1)
    return False


def about_own_case(w):
    """After every document is read: a stated record (an incident, a requirement ...) whose own id (own_id: the id
    its name or statement starts with) is exactly the name of ONE Topic is ABOUT that Topic: a thing named by an id
    of its own is a Topic named by that id (the document prompt's rule), and an item that carries the id as its own
    is about it. What the text states, matched in code; no model call, nothing inferred beyond the id written."""
    cat, reg, v = w.v.catalog(), common._reg, w.v
    out = collections.Counter()
    v._index()
    topics = collections.defaultdict(list)
    for r in w.g.records.values():
        if r["type"] == HUB_TYPE and r["props"].get("name"):
            topics[r["props"]["name"]].append(r)
    for r in [x for x in w.g.records.values() if x["type"] in set(cat.stated)]:
        case = own_id(r, cat)
        hits = topics.get(case or "", [])
        if len(hits) != 1:
            if len(hits) > 1:
                out["several Topics carry the id: not linked"] += 1
            continue
        t = hits[0]
        if not reg.link_allows("ABOUT", r["type"], t["type"]):
            continue
        if any(lk["type"] == "ABOUT" and lk["b"] == t["id"] for lk in v.out.get(r["id"], [])):
            out[f"{r['type']} already ABOUT its own case"] += 1
            continue
        src = next((s for s, _ in _quotes(r) if s in w.by_id), None)
        why = w._link("ABOUT", r, t, [src] if src else [], "own_case", "reference", 1.0, method="text_rule")
        out[f"{r['type']} ABOUT the Topic of its own id: " + ("written" if why is None else "not written, " + why[:80])] += 1
    return dict(out)
