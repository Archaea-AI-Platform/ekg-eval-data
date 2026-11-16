"""What a document may state, worked out from query-master's registry (never listed by hand here).

The registry is the schema; this module only asks it questions. The rules, all in this file:

  creatable   a record type the stage may CREATE from a document, when the graph has no such record yet:
                (1) the schema lets it originate in a document: it is a `from` type of an origin link
                    (relation RAISED_IN: EXTRACTED_FROM, RAISED_IN, MADE_IN, DOCUMENTED_IN ...) whose `to`
                    types include Document, File, DocSection or Piece; or
                (2) its `source` in the registry is a document-like source (SOURCE_WORDS: a CSV, a catalog,
                    a directory, HR, CRM ...). The registry gives sources as prose, so this is a word match:
                    see "schema gaps" in the stage's report;
              and it has a field a text can name it by (names_of) or state it in (TEXT_FIELDS).
  resolvable  a type the stage may only LINK TO: it has a name field and the graph holds records of it
              (a ticket, a pull request, a repository ...). A document naming one refers to the tool's record.
  links       every non-internal, non-origin link the registry allows between two such types where at least
              one end names a creatable type outright (not through a core type it merely counts as): a
              document may state facts about things it can define; facts purely between tool records belong
              to those tools' adapters. An `any` end is a catch-all and does not count as a match.

Internals and access types never take part.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re

PROPER_NAMES = ("full_name", "name")                       # a type that has one is named by it and by nothing else
PERSONAL_NAME = PROPER_NAMES[0]                            # a type named by this is named like a person (see extract_facts.thin)
WRITER_LINK_FIELDS = ("method", "confidence", "evidence", "rule_id")  # set on every link by the writer, never from a text
# the discriminator the registry gives for "one record carries several separate facts of this link" (part of the fact
# key): a table column that holds it per row (the path within a repository a reviewer is for) splits facts rightly
ASPECT = "aspect"
OTHER_NAMES = ("title", "identifier", "address", "login")  # otherwise by any of these
VERSION_NAME = "version"                                   # and a core-typed record with none of them by its version (a Release)
NAME_FIELDS = PROPER_NAMES + OTHER_NAMES
TEXT_FIELDS = ("statement", "text", "summary")                                             # how a text states one
# other fields a record can be found by (the name index in fill_tasks.View reads NAME_FIELDS + these)
LOOKUP_FIELDS = ("display_name", "preferred_name", "work_email", "handle", "normalized_value", "aliases", "emails",
                 "value", "domain")
SKIP_PILLARS = {"internals", "access"}
DOCUMENT_TYPES = ("Document", "File", "DocSection", "Piece")
ORIGIN_RELATION = "RAISED_IN"
ORIGIN_ORDER = ("EXTRACTED_FROM", "RAISED_IN", "MADE_IN", "DOCUMENTED_IN")  # the first the registry allows is written
SOURCE_WORDS = re.compile(r"\b(csv|catalog|directory|hr|crm|adrs?|prds?|runbooks?|style guides?|notes)\b", re.I)
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?Z?)?$")
# the registry's descriptions a prompt's schema section can carry, most needed first (Catalog.schema_text): what
# each record type is, what each link means and which way it is stored, what a link's fields hold, what a field holds
DETAIL_LAYERS = ("types", "links", "link_fields", "fields")
# which layers each prompt carries. A document prompt fills fields from the text, so it describes them all. A table
# prompt maps columns to types, links and fields by name: it describes types and links (what each means, which way
# it is stored). With every layer (148,510 characters on the ACME graph, 35,000 tokens) the first real run thought
# for 1,200 to 11,000 tokens per mapping, 4 of 15 over 8,000; types and links alone are 77,700 characters.
LAYERS_FOR = {"documents": DETAIL_LAYERS, "tables": ("types", "links")}
LINK_HEAD = ("LINK TYPES (from -> to). Use only these, only in this direction, only between these types. Each one's "
             "description says what \"A LINK B\" means: A is the record on the left.")


def first_sentence(text) -> str:
    """A description's first sentence (up to the first '.' or ';' followed by a space), else the whole."""
    m = re.match(r"(.+?[.;])(?=\s|$)", text or "", re.S)
    return (m.group(1) if m else (text or "")).strip()


class Catalog:
    def __init__(self, reg, present_types=()):
        self.reg = reg
        self._schema_cache = {}
        self.budget = None   # fill_budget.Budget the prompts' schema sections are sized by (set by View.catalog)
        usable = {n: t for n, t in reg.records.items() if not set(t.pillars) <= SKIP_PILLARS}
        self.origin_links = [lk for lk in reg.links.values() if lk.relation == ORIGIN_RELATION and not lk.internal
                             and set(lk.to_types) & set(DOCUMENT_TYPES)]
        stated = {n for n in usable for lk in self.origin_links if any(reg.is_a(n, ft) for ft in lk.from_types if ft != "any")}
        sourced = {n for n, t in usable.items() if SOURCE_WORDS.search(t.source or "") and names_of(t)}
        self.stated = sorted(n for n in stated if names_of(usable[n]) or self._named(usable[n], TEXT_FIELDS))
        self.creatable = sorted(set(self.stated) | sourced)
        self.not_extractable = sorted(n for n in stated if n not in self.stated)  # originate in documents, no name or text field
        self.resolvable = sorted(n for n in usable if n in set(present_types) and n not in self.creatable
                                 and names_of(usable[n]))
        known = self.creatable + self.resolvable
        stated_named = set(self.stated)
        self.links = {}
        for name, lk in sorted(reg.links.items()):
            if lk.internal or lk.relation == ORIGIN_RELATION or set(lk.pillars) <= SKIP_PILLARS:
                continue
            # an `any` end is a catch-all: it counts for the types a document states (a requirement ABOUT the service
            # it is about), never for every type, which would offer each link between everything
            a = [t for t in known if any(reg.is_a(t, x) for x in lk.from_types if x != "any")
                 or ("any" in lk.from_types and t in stated_named)]
            b = [t for t in known if any(reg.is_a(t, x) for x in lk.to_types if x != "any")
                 or ("any" in lk.to_types and t in stated_named)]
            named_ends = set(lk.from_types) | set(lk.to_types) | (stated_named if "any" in set(lk.from_types) | set(lk.to_types) else set())
            if a and b and set(self.creatable) & named_ends:
                self.links[name] = (a, b)
        used = {t for a, b in self.links.values() for t in a + b}
        self.resolvable = [t for t in self.resolvable if t in used]
        # a TABLE may be a stored export of another tool (a commit log, a list of pull requests): its rows may be
        # records of any type outside internals and access, matched to the tool's own record by a key field
        self.table_types = sorted(usable)
        self.table_links = {}
        for name, lk in sorted(reg.links.items()):
            if lk.internal or lk.relation == ORIGIN_RELATION or set(lk.pillars) <= SKIP_PILLARS:
                continue
            a = [t for t in self.table_types if any(reg.is_a(t, x) for x in lk.from_types if x != "any")]
            b = [t for t in self.table_types if any(reg.is_a(t, x) for x in lk.to_types if x != "any")]
            if a and b:
                self.table_links[name] = (a, b)

    @staticmethod
    def _named(t, names):
        return any(f in t.fields for f in names)

    # ---- one type
    def names(self, rtype):
        return names_of(self.reg.record(rtype))

    def name_field(self, rtype):
        return next(iter(self.names(rtype)), None)

    def text_field(self, rtype):
        f = self.reg.record(rtype).fields
        return next((n for n in TEXT_FIELDS if n in f), None)

    def fields(self, rtype):
        """The fields a text may fill: stored (not computed per viewer), a plain value, not one only some tools keep
        (`extra`), and not the name or text field."""
        rules = self.reg.rules
        skip = {self.name_field(rtype), self.text_field(rtype)} 
        return {n: s for n, s in self.reg.record(rtype).fields.items()
                if n not in skip and s.type != "object" and not s.computed and not s.extra and n not in rules["computed_everywhere"]
                and not any(n.endswith(x) for x in rules["computed_suffixes"])
                and n not in rules["forbidden_fields"].get(rtype, [])}

    def link_fields(self, ltype):
        """A link's own stated fields: the enumerations, and role / kind where the spec names their values."""
        lk = self.reg.link(ltype)
        out = {}
        for n, s in lk.fields.items():
            if s.enum:
                out[n] = list(s.enum)
            elif n in lk.known_values and n in ("role", "kind"):
                out[n] = list(lk.known_values[n])
        return out

    def link_value_fields(self, ltype):
        """Every field of a link a table cell can fill (a date, a count, a role ...): {name: spec}. Left out: the
        fields the writer sets on every link itself (WRITER_LINK_FIELDS), objects, computed ones, and a free-text
        discriminator (aspect, or role / kind with no named values), which would only split one fact in two."""
        lk, rules = self.reg.link(ltype), self.reg.rules
        out = {}
        for n, s in lk.fields.items():
            if n in WRITER_LINK_FIELDS or s.type == "object" or s.computed or s.extra or n in rules["computed_everywhere"]:
                continue
            if n in rules["link_discriminators"] and not s.enum and n not in lk.known_values and n != ASPECT:
                continue
            out[n] = s
        return out

    def link_value_text(self, ltype):
        lk = self.reg.link(ltype)
        return ", ".join(f"{n} ({'|'.join(lk.known_values[n])})" if not s.enum and n in lk.known_values else self._field_text(n, s)
                         for n, s in self.link_value_fields(ltype).items())

    def origin_for(self, rtype, target_types):
        """(link, target type) of the origin link the registry allows from this type to one of `target_types`
        (tried in the order given), or None."""
        by_name = {lk.name: lk for lk in self.origin_links}
        for name in [*ORIGIN_ORDER, *sorted(set(by_name) - set(ORIGIN_ORDER))]:
            for tt in target_types:
                if name in by_name and self.reg.link_allows(name, rtype, tt):
                    return name, tt
        return None

    # ---- the prompt's schema section
    def _field_text(self, n, s):
        return f"{n} ({'|'.join(s.enum)})" if s.enum else f"{n} (date)" if s.type == "time" else \
            f"{n} (number)" if s.type == "number" else f"{n} (true|false)" if s.type == "boolean" else n

    def _about_type(self, t, detail):
        rt = self.reg.record(t)
        how = detail.get("types")
        text = (rt.about or rt.description) if how else (rt.description or rt.about)
        text = first_sentence(text) if how == "first" else text
        return (text or "found in " + rt.source).rstrip(". ")

    def _fields_text(self, specs, layer, detail, plain):
        """A type's (or a link's) fields: `plain` (name and value kinds) on one line, or with the registry's
        description of each when the `layer` is in `detail`, one per line."""
        if not specs:
            return ""
        how = detail.get(layer)
        if not how:
            return ". Fields: " + plain
        rows = []
        for n, s in specs.items():
            about = first_sentence(s.about) if how == "first" else s.about
            rows.append(f"    {self._field_text(n, s)}" + (f": {about}" if about else ""))
        return ". Fields:\n" + "\n".join(rows)

    def _about_link(self, name, detail):
        how = detail.get("links")
        about = self.reg.link(name).about if how else ""
        return f" ({first_sentence(about) if how == 'first' else about})" if about else ""

    def describe(self, types=None, links=None, link_values=False, detail=None, with_links=True) -> str:
        """The prompt's schema section. link_values: list every field a link can carry (for a table, whose columns
        may hold a link's date or count), not only the named values a text states. detail: which of the
        registry's descriptions (DETAIL_LAYERS) are shown, each "full" or "first" (its first sentence); the
        rest show the short description only (see schema_text, which fits them to a budget). with_links: False
        for the record types alone (describe_tables lists the links itself, with every end a table may use)."""
        reg, detail = self.reg, detail or {}
        types = set(types or self.creatable + self.resolvable)
        out = ["RECORD TYPES you may name. Types marked NEW may be created from this text; the others exist only when "
               "the organisation's tools already hold them, so name one only to link to it."]
        for t in self.creatable + self.resolvable:
            if t not in types:
                continue
            named = " or ".join(self.names(t)) or "-"
            line = f"- {t}{' NEW' if t in self.creatable else ''}: {self._about_type(t, detail)}. Named by {named}"
            if t in self.creatable:
                if (tf := self.text_field(t)):
                    line += f"; stated in {tf}"
                fs = self.fields(t)
                line += self._fields_text(fs, "fields", detail, ", ".join(self._field_text(n, s) for n, s in fs.items()))
            out.append(line)
        if not with_links:
            return "\n".join(out)
        out += ["", LINK_HEAD]
        for name, (a, b) in self.links.items():
            if links is not None and name not in links:
                continue
            a, b = [t for t in a if t in types], [t for t in b if t in types]
            if not a or not b:
                continue
            line = f"- {name}: {' | '.join(a)} -> {' | '.join(b)}{self._about_link(name, detail)}"
            if link_values:
                line += self._link_values(name, detail)
            elif (lf := self.link_fields(name)):
                specs = {n: self.reg.link(name).fields[n] for n in lf}
                line += self._fields_text(specs, "link_fields", detail, ", ".join(f"{n} ({'|'.join(vals)})" for n, vals in lf.items()))
            out.append(line)
        return "\n".join(out)

    def _link_values(self, name, detail):
        return self._fields_text(self.link_value_fields(name), "link_fields", detail, self.link_value_text(name))

    def schema_text(self, which: str, budget: int):
        """The schema section for one kind of prompt ("documents": describe(), "tables": describe_tables()) with as
        many of the registry's descriptions as fit `budget` characters: each layer the prompt carries (LAYERS_FOR)
        in full when they fit; when not, the layers least needed give way first (record fields, then link fields,
        then links, then types), each first cut to its first sentence, then left out.
        Returns (text, {"detail": {layer: full|first}, "chars": n, "chars_without_descriptions": n}). Cached."""
        key = (which, int(budget))
        if key not in self._schema_cache:
            render = self.describe if which == "documents" else self.describe_tables
            layers = LAYERS_FOR[which]
            states = [{layer: "full" for layer in layers}]
            for layer in reversed(layers):   # the order the layers give way in
                states.append({**states[-1], layer: "first"})
                states.append({k: v for k, v in states[-1].items() if k != layer})
            for detail in states:
                text = render(detail=detail)
                if len(text) <= budget:
                    break
            bare = len(render(detail={}))
            self._schema_cache[key] = (text, {"detail": detail, "chars": len(text), "chars_without_descriptions": bare,
                                              "budget_chars": int(budget)})
        return self._schema_cache[key]

    def key_fields(self, rtype):
        """The fields a table column may hold as a row's own key (a number, a sha, an id): a plain text or number
        field, or a name field. An enumeration, a flag, a date or a list cannot tell one record from another."""
        f = self.reg.record(rtype).fields
        return {n: s for n, s in self.fields(rtype).items() if s.type in ("any", "number")} | {n: f[n] for n in self.names(rtype)}

    def describe_tables(self, detail=None) -> str:
        """The table prompt's schema: the document catalog's record types, then the other tools' record types, then
        every link a table row may state, each with every end a table may use (a pull request's ABOUT a service, not
        only a document's) and the fields a cell can fill. detail: as for describe()."""
        detail = detail or {}
        known = set(self.creatable + self.resolvable)
        out = [self.describe(link_values=True, detail=detail, with_links=False), "",
               "RECORD TYPES OF OTHER TOOLS. A table may be an export of another tool (a commit log, a merge log, a list of "
               "pull requests or tickets): its rows may then be records of one of these types. Code matches each row to "
               "the tool's own record by the row's key field (or its name) and creates one only when the graph has none."]
        for t in self.table_types:
            if t in known:
                continue
            fs = self.fields(t)
            out.append(f"- {t}: {self._about_type(t, detail)}. Named by {' or '.join(self.names(t)) or '-'}"
                       + self._fields_text(fs, "fields", detail, ", ".join(self._field_text(n, s) for n, s in fs.items())))
        out += ["", LINK_HEAD]
        for name, (a, b) in self.table_links.items():
            out.append(f"- {name}: {' | '.join(a)} -> {' | '.join(b)}{self._about_link(name, detail)}" + self._link_values(name, detail))
        return "\n".join(out)

    def summary(self) -> dict:
        return {"creatable": self.creatable, "resolvable": self.resolvable, "links": sorted(self.links),
                "origin_links": sorted(lk.name for lk in self.origin_links),
                "originate_in_documents_but_have_no_name_or_text_field": self.not_extractable}


def names_of(rt) -> list:
    """The fields a text names a record of this type by."""
    proper = [f for f in PROPER_NAMES if f in rt.fields][:1]
    return proper or [f for f in OTHER_NAMES if f in rt.fields] or ([VERSION_NAME] if VERSION_NAME in rt.fields and rt.cores else [])


PLACEHOLDER_WORDS = ("none", "null", "n/a", "na", "unknown", "tbd")


def placeholder(value) -> bool:
    """A cell or value that stands for nothing: empty, no letter or digit at all ("-", "—", "?", "…"), or one of
    the few words written for 'no value' (PLACEHOLDER_WORDS). Never a name, a key or a link end."""
    s = str(value if value is not None else "").strip()
    return not s or s.casefold() in PLACEHOLDER_WORDS or not re.search(r"[^\W_]", s)


# a month written by name: the calendar's own names and their abbreviations (Oct, October, Sept), the whole word
_MONTHS = {**{calendar.month_abbr[i].casefold(): i for i in range(1, 13)},
           **{calendar.month_name[i].casefold(): i for i in range(1, 13)}, "sept": 9}
_MONTH_DAY = re.compile(r"(?<![^\W\d_])([^\W\d_]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*[-–]\s*(\d{1,2})(?!\d))?"
                        r"(?:,?\s+(\d{4}))?(?!\d)")
_DAY_MONTH = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+([^\W\d_]{3,9})\.?(?:,?\s+(\d{4}))?(?!\d)")
_ISO_DAY = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def _ymd(y, mo, d):
    try:
        return dt.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def dates_in(text, year=None):
    """Every calendar day a text writes, in order, as YYYY-MM-DD: ISO dates, and dates with the month by name
    ("Oct 03, 2026", "3 October 2026", "Oct 03-07" = two days). A date written without its year takes `year` (the
    caller's: the year of the table's or document's own full dates); without one it is not a date. Spans: [(start, end, day)]."""
    s, out = str(text or ""), []
    for m in _ISO_DAY.finditer(s):
        if (d := _ymd(*m.groups())):
            out.append((m.start(), m.end(), d))
    for m in _MONTH_DAY.finditer(s):
        mo = _MONTHS.get(m.group(1).casefold())
        y = m.group(4) or year
        if mo and y:
            for day in [m.group(2)] + ([m.group(3)] if m.group(3) else []):
                if (d := _ymd(y, mo, day)):
                    out.append((m.start(), m.end(), d))
    for m in _DAY_MONTH.finditer(s):
        mo = _MONTHS.get(m.group(2).casefold())
        y = m.group(3) or year
        if mo and y and not any(a <= m.start() < b for a, b, _ in out) and (d := _ymd(y, mo, m.group(1))):
            out.append((m.start(), m.end(), d))
    return sorted(out)


def year_of(texts):
    """The year a table's or document's own full dates are in (the latest one written), as a string, or None: what a
    date written without its year ("Oct 03") is read in."""
    years = [int(y) for t in texts for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(t or ""))]
    return str(max(years)) if years else None


def period(text, from_words=(), to_words=(), year=None):
    """(valid_from, valid_to) a cell gives as a period, each YYYY-MM-DD or None: two dates are a start and an end
    ("Oct 03-07"); one date is an end when an end word of the table's own (to_words, copied from its cells, e.g.
    "Before") comes before it, else a start ("From Oct 08", "2026-10-08")."""
    found = dates_in(text, year)
    if not found:
        return None, None
    if len(found) >= 2:
        return found[0][2], found[-1][2]
    before = str(text)[:found[0][0]].casefold()
    ends = any(w and w.casefold() in before for w in to_words)
    starts = any(w and w.casefold() in before for w in from_words)
    return (None, found[0][2]) if ends and not starts else (found[0][2], None)


def coerce(spec, value, year=None):
    """A value the model wrote as text -> (the value in the field's type, its precision or None), or None when
    it does not fit. Dates are stored to the day unless a time was written; a date with its month by name is read
    too (dates_in), with `year` for one written without its year."""
    if value is None:
        return None
    s = str(value).strip()
    if placeholder(s):
        return None
    t = spec.type
    if t == "enum":
        hit = next((e for e in spec.enum if e.lower() == s.lower().replace(" ", "_")), None)
        return (hit, None) if hit else None
    if t == "time":
        m = _DATE.match(s)
        if not m:
            found = dates_in(s, year)   # the whole value is one written date ("Oct 03, 2026"), nothing else in it
            if len(found) == 1 and not re.search(r"[^\W_]", s[:found[0][0]] + s[found[0][1]:]):
                return (f"{found[0][2]}T00:00:00Z", "day")
            return None
        y, mo, d, hh, mm, ss = m.groups()
        if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
            return None
        return (f"{y}-{mo}-{d}T{hh}:{mm}:{ss or '00'}Z", None) if hh else (f"{y}-{mo}-{d}T00:00:00Z", "day")
    if t == "boolean":
        return (s.lower() in ("true", "yes", "y", "1"), None) if s.lower() in ("true", "false", "yes", "no", "y", "n", "1", "0") else None
    if t == "number":
        try:
            n = float(s.replace(",", "").removeprefix("#"))   # "#174": a numbered thing (a pull request, an issue)
        except ValueError:
            return None
        return (int(n) if n.is_integer() else n, None)
    if t == "array":
        return ([x.strip() for x in re.split(r"[;\n]", s) if x.strip()], None)
    return (s, None)
