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

import re

PROPER_NAMES = ("full_name", "name")                       # a type that has one is named by it and by nothing else
PERSONAL_NAME = PROPER_NAMES[0]                            # a type named by this is named like a person (see extract_facts.thin)
WRITER_LINK_FIELDS = ("method", "confidence", "evidence", "rule_id")  # set on every link by the writer, never from a text
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


class Catalog:
    def __init__(self, reg, present_types=()):
        self.reg = reg
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
        self.links = {}
        for name, lk in sorted(reg.links.items()):
            if lk.internal or lk.relation == ORIGIN_RELATION or set(lk.pillars) <= SKIP_PILLARS:
                continue
            a = [t for t in known if any(reg.is_a(t, x) for x in lk.from_types if x != "any")]
            b = [t for t in known if any(reg.is_a(t, x) for x in lk.to_types if x != "any")]
            if a and b and set(self.creatable) & (set(lk.from_types) | set(lk.to_types)):
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
            if n in rules["link_discriminators"] and not s.enum and n not in lk.known_values:
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

    def describe(self, types=None, links=None, link_values=False) -> str:
        """The prompt's schema section. link_values: list every field a link can carry (for a table, whose columns
        may hold a link's date or count), not only the named values a text states."""
        reg = self.reg
        types = set(types or self.creatable + self.resolvable)
        out = ["RECORD TYPES you may name. Types marked NEW may be created from this text; the others exist only when "
               "the organisation's tools already hold them, so name one only to link to it."]
        for t in self.creatable + self.resolvable:
            if t not in types:
                continue
            rt = reg.record(t)
            named = " or ".join(self.names(t)) or "-"
            line = f"- {t}{' NEW' if t in self.creatable else ''}: {rt.description or 'found in ' + rt.source}. Named by {named}"
            if t in self.creatable:
                if (tf := self.text_field(t)):
                    line += f"; stated in {tf}"
                if (fs := self.fields(t)):
                    line += ". Fields: " + ", ".join(self._field_text(n, s) for n, s in fs.items())
            out.append(line)
        out += ["", "LINK TYPES (from -> to). Use only these, only in this direction, only between these types."]
        for name, (a, b) in self.links.items():
            if links is not None and name not in links:
                continue
            a, b = [t for t in a if t in types], [t for t in b if t in types]
            if not a or not b:
                continue
            line = f"- {name}: {' | '.join(a)} -> {' | '.join(b)}"
            if link_values:
                if (lv := self.link_value_text(name)):
                    line += ". Fields: " + lv
            elif (lf := self.link_fields(name)):
                line += ". Fields: " + ", ".join(f"{n} ({'|'.join(vals)})" for n, vals in lf.items())
            out.append(line)
        return "\n".join(out)

    def key_fields(self, rtype):
        """The fields a table column may hold as a row's own key (a number, a sha, an id): a plain text or number
        field, or a name field. An enumeration, a flag, a date or a list cannot tell one record from another."""
        f = self.reg.record(rtype).fields
        return {n: s for n, s in self.fields(rtype).items() if s.type in ("any", "number")} | {n: f[n] for n in self.names(rtype)}

    def describe_tables(self) -> str:
        """The table prompt's schema: the document catalog, then the other tools' record types and the links
        between them, each with the fields a cell can fill."""
        reg = self.reg
        known = set(self.creatable + self.resolvable)
        out = [self.describe(link_values=True), "",
               "RECORD TYPES OF OTHER TOOLS. A table may be an export of another tool (a commit log, a merge log, a list of "
               "pull requests or tickets): its rows may then be records of one of these types. Code matches each row to "
               "the tool's own record by the row's key field (or its name) and creates one only when the graph has none."]
        for t in self.table_types:
            if t in known:
                continue
            rt = reg.record(t)
            fs = self.fields(t)
            out.append(f"- {t}: {rt.description or 'found in ' + rt.source}. Named by {' or '.join(self.names(t)) or '-'}"
                       + (". Fields: " + ", ".join(self._field_text(n, s) for n, s in fs.items()) if fs else ""))
        out += ["", "MORE LINK TYPES, for rows of those types (from -> to)."]
        for name, (a, b) in self.table_links.items():
            if name in self.links:
                continue
            line = f"- {name}: {' | '.join(a)} -> {' | '.join(b)}"
            if (lv := self.link_value_text(name)):
                line += ". Fields: " + lv
            out.append(line)
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


def coerce(spec, value):
    """A value the model wrote as text -> (the value in the field's type, its precision or None), or None when
    it does not fit. Dates are stored to the day unless a time was written."""
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
