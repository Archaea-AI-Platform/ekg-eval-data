"""Code pillar, the code parse: source text of any language -> CodeSymbol, DEFINED_IN, IMPORTS, EXTENDS, CALLS.

One extractor for every language, built on tree-sitter (tree_sitter_language_pack). What a language's
symbols look like is data, not code: tree-sitter "tags" queries. A language is covered by

  1. the tags query the language pack bundles for it (definitions, calls, inheritance), and
  2. the extra patterns in QUERIES below, in the same vocabulary, for what the bundled query leaves out
     (enums, records, traits, packages, imports, extends / implements told apart).

so adding a language is adding patterns to QUERIES (or nothing, when the bundled query is enough). A
pattern the installed grammar does not accept is dropped, never an error. A file whose language has no
grammar or no query gives no symbols and is counted.

Capture vocabulary (the standard tags names, plus a few of our own):
  @definition.<kind> + @name   a symbol; kind is one of KINDS (others, e.g. constants, are dropped)
       + @receiver             the type a method is declared on, when it is not nested in it (Go)
  @namespace + @name           a namespace block: qualifies what it holds in place of the file's path; not a symbol
  @scope + @name               a block that adds members to a type of the file (Rust impl): not itself a symbol
  @package + @name             a file-wide package / namespace declaration: qualifies everything in the file
  @import (+ @name, @alias)    an import; with no @name the statement's own text is read ("a.b.{C, D}")
  @import.namespace + @name    an import of a whole namespace (C# using)
  @import.items + @item        the names a from-import brings in, attached to that statement's import
  @reference.extends | .implements | .implementation + @name    a supertype of the enclosing type
  @reference.call | .send + @name                               a call made by the enclosing function

Names: a file that declares a package is qualified by it ("org.acme.Foo.run"); any other file by its path
("python/tvm/relax/pass.py" -> "python.tvm.relax.pass.RelaxPass.run"), or by its folder when the language's
package is the folder (Go). Nesting is in the qualified name (the schema has no symbol-in-symbol link);
extract() also gives each symbol's parent.

Links are made by name, inside the repository, only where the language's own binding rules settle the name:
  IMPORTS   file/module symbol -> the imported in-repo module or type (exact qualified name, a unique
            source-root suffix for path-named modules, or a relative path that is one file)
  EXTENDS   type -> supertype found as a nested/enclosing type, an explicit import, a type of the same
            package, or exactly one wildcard-imported package (role extends | implements)
  CALLS     function -> the callee of a bare call, a this./self. call, or a call on an imported or
            same-package type name, when exactly one symbol has that name there (no type inference;
            overloads and calls through an expression are counted, not guessed)
Everything else is counted in the notes as unresolved.
"""

from __future__ import annotations

import collections
import functools
import posixpath
import re

MAX_BYTES = 1_500_000  # a larger file is generated code or data: skipped and counted

CLASS_LIKE = {"class", "interface", "trait", "enum", "struct", "record", "object", "type", "module", "namespace"}
FUNC_LIKE = {"function", "method", "constructor"}
KINDS = CLASS_LIKE | FUNC_LIKE
TYPE_LIKE = (CLASS_LIKE - {"module", "namespace"}) | {"scope"}  # what makes a function inside it a method

_JAVA_SUPER = "[(type_identifier) (generic_type) (scoped_type_identifier)] @name"
_ECMA = [
    '(import_statement source: (string (string_fragment) @name)) @import',
    '(import_statement (import_clause (named_imports (import_specifier name: (identifier) @item)))) @import.items',
    '(class_declaration name: (_) @name) @definition.class',
    '(abstract_class_declaration name: (_) @name) @definition.class',
    '(function_declaration name: (identifier) @name) @definition.function',
    '(generator_function_declaration name: (identifier) @name) @definition.function',
    '(method_definition name: (_) @name) @definition.method',
    '(variable_declarator name: (identifier) @name value: [(arrow_function) (function_expression)]) @definition.function',
    '(enum_declaration name: (identifier) @name) @definition.enum',
    '(interface_declaration name: (type_identifier) @name) @definition.interface',
    '(type_alias_declaration name: (type_identifier) @name) @definition.type',
    '(internal_module name: (_) @name) @definition.module',
    '(class_declaration (class_heritage (extends_clause value: (_) @name))) @reference.extends',
    '(abstract_class_declaration (class_heritage (extends_clause value: (_) @name))) @reference.extends',
    '(class_declaration (class_heritage (implements_clause (_) @name))) @reference.implements',
    '(abstract_class_declaration (class_heritage (implements_clause (_) @name))) @reference.implements',
    '(interface_declaration (extends_type_clause type: (_) @name)) @reference.extends',
    '(class_declaration (class_heritage (identifier) @name)) @reference.extends',
    '(call_expression function: (identifier) @name) @reference.call',
    '(call_expression function: (member_expression property: (property_identifier) @name)) @reference.call',
]
_C = [
    '(preproc_include path: (string_literal (string_content) @name)) @import',
    '(preproc_include path: (system_lib_string) @name) @import',
    '(namespace_definition name: (_) @name) @namespace',
    '(class_specifier name: (_) @name body: (_)) @definition.class',
    '(struct_specifier name: (_) @name body: (_)) @definition.struct',
    '(enum_specifier name: (_) @name body: (_)) @definition.enum',
    '(class_specifier (base_class_clause (type_identifier) @name)) @reference.extends',
    '(struct_specifier (base_class_clause (type_identifier) @name)) @reference.extends',
    '(function_definition declarator: (function_declarator declarator: (_) @name)) @definition.function',
    '(function_definition declarator: (pointer_declarator declarator: (function_declarator declarator: (_) @name))) @definition.function',
    '(call_expression function: (identifier) @name) @reference.call',
    '(call_expression function: (field_expression field: (field_identifier) @name)) @reference.call',
    '(call_expression function: (qualified_identifier name: (identifier) @name)) @reference.call',
]

# language -> extra patterns, tried before the bundled tags query (the first pattern to claim a node wins)
QUERIES = {
    "java": [
        '(package_declaration [(scoped_identifier) (identifier)] @name) @package',
        '(import_declaration [(scoped_identifier) (identifier)] @name) @import',
        '(enum_declaration name: (identifier) @name) @definition.enum',
        '(record_declaration name: (identifier) @name) @definition.record',
        '(annotation_type_declaration name: (identifier) @name) @definition.interface',
        '(constructor_declaration name: (identifier) @name) @definition.constructor',
        f'(class_declaration superclass: (superclass {_JAVA_SUPER})) @reference.extends',
        f'(class_declaration interfaces: (super_interfaces (type_list {_JAVA_SUPER}))) @reference.implements',
        f'(enum_declaration interfaces: (super_interfaces (type_list {_JAVA_SUPER}))) @reference.implements',
        f'(record_declaration interfaces: (super_interfaces (type_list {_JAVA_SUPER}))) @reference.implements',
        f'(interface_declaration (extends_interfaces (type_list {_JAVA_SUPER}))) @reference.extends',
    ],
    "scala": [
        '(package_clause name: (package_identifier) @name) @package',
        '(import_declaration) @import',
        '(object_definition name: (identifier) @name) @definition.object',
        '(trait_definition name: (identifier) @name) @definition.trait',
        '(enum_definition name: (identifier) @name) @definition.enum',
        '(function_declaration name: (identifier) @name) @definition.method',
        '(extends_clause (_) @name) @reference.extends',  # every type of "extends A with B"
        '(call_expression function: (field_expression field: (identifier) @name)) @reference.call',
    ],
    "python": [
        '(import_statement name: (dotted_name) @name) @import',
        '(import_statement name: (aliased_import name: (dotted_name) @name alias: (identifier) @alias)) @import',
        '(import_from_statement module_name: (_) @name) @import',
        '(import_from_statement name: (dotted_name) @item) @import.items',
        '(import_from_statement name: (aliased_import name: (dotted_name) @item)) @import.items',
        '(class_definition superclasses: (argument_list [(identifier) (attribute)] @name)) @reference.extends',
    ],
    "javascript": _ECMA,
    "typescript": _ECMA,
    "tsx": _ECMA,
    "go": [
        '(import_spec name: (package_identifier) @alias path: (interpreted_string_literal (interpreted_string_literal_content) @name)) @import',
        '(import_spec path: (interpreted_string_literal (interpreted_string_literal_content) @name)) @import',
        '(type_spec name: (type_identifier) @name type: (struct_type)) @definition.struct',
        '(type_spec name: (type_identifier) @name type: (interface_type)) @definition.interface',
        '(method_declaration receiver: (parameter_list (parameter_declaration type: [(type_identifier) @receiver '
        '(pointer_type (type_identifier) @receiver) (generic_type type: (type_identifier) @receiver) '
        '(pointer_type (generic_type type: (type_identifier) @receiver))])) name: (field_identifier) @name) @definition.method',
    ],
    "csharp": [
        '(using_directive [(qualified_name) (identifier)] @name) @import.namespace',
        '(namespace_declaration name: (_) @name) @namespace',
        '(file_scoped_namespace_declaration name: (_) @name) @package',
        '(struct_declaration name: (identifier) @name) @definition.struct',
        '(enum_declaration name: (identifier) @name) @definition.enum',
        '(record_declaration name: (identifier) @name) @definition.record',
        '(constructor_declaration name: (identifier) @name) @definition.constructor',
        '(struct_declaration (base_list (_) @name)) @reference.implementation',
        '(record_declaration (base_list (_) @name)) @reference.implementation',
        '(invocation_expression function: (identifier) @name) @reference.call',
        '(invocation_expression function: (member_access_expression name: (identifier) @name)) @reference.call',
    ],
    "kotlin": [
        '(package_header (identifier) @name) @package',
        '(import_header (identifier) @name) @import',
        '(object_declaration (type_identifier) @name) @definition.object',
        '(class_declaration (delegation_specifier (constructor_invocation (user_type (type_identifier) @name)))) @reference.extends',
    ],
    "rust": [
        '(use_declaration) @import',
        '(struct_item name: (type_identifier) @name) @definition.struct',
        '(enum_item name: (type_identifier) @name) @definition.enum',
        '(trait_item name: (type_identifier) @name) @definition.trait',
        '(impl_item type: (_) @name) @scope',
        '(impl_item trait: (_) @name) @reference.implements',
    ],
    "c": _C,
    "cpp": _C,
    "ruby": [
        '(call method: (identifier) @_m arguments: (argument_list (string (string_content) @name)) '
        '(#any-of? @_m "require" "require_relative")) @import',
        '(module name: (_) @name) @definition.module',
        '(class name: (_) @name) @definition.class',
        '(method name: (_) @name) @definition.method',
        '(singleton_method name: (_) @name) @definition.method',
        '(class superclass: (superclass (_) @name)) @reference.extends',
    ],
    "php": [
        '(namespace_definition name: (namespace_name) @name) @package',
        '(namespace_use_clause [(qualified_name) (name)] @name) @import',
        '(class_declaration name: (name) @name) @definition.class',
        '(interface_declaration name: (name) @name) @definition.interface',
        '(trait_declaration name: (name) @name) @definition.trait',
        '(enum_declaration name: (name) @name) @definition.enum',
        '(method_declaration name: (name) @name) @definition.method',
        '(function_definition name: (name) @name) @definition.function',
        '(class_declaration (base_clause (_) @name)) @reference.extends',
        '(class_declaration (class_interface_clause (_) @name)) @reference.implements',
        '(function_call_expression function: (name) @name) @reference.call',
        '(member_call_expression name: (name) @name) @reference.call',
        '(scoped_call_expression name: (name) @name) @reference.call',
    ],
}

# What the grammar cannot say, per language (a language not listed takes the defaults):
#   implicit_self  a bare call inside a type may name a member of that type (default False: it needs this./self.)
#   module         "dir": the package is the folder, so names are qualified by the folder (default: by the file)
#   index          the file stem that stands for its folder (a/__init__.py is module "a")
#   binds          "first": a plain dotted import binds its first segment, not its last (import a.b -> a)
FACTS = {
    "java": {"implicit_self": True}, "scala": {"implicit_self": True}, "kotlin": {"implicit_self": True},
    "csharp": {"implicit_self": True}, "cpp": {"implicit_self": True}, "ruby": {"implicit_self": True},
    "swift": {"implicit_self": True}, "groovy": {"implicit_self": True}, "dart": {"implicit_self": True},
    "go": {"module": "dir"},
    "python": {"index": "__init__", "binds": "first"}, "rust": {"index": "mod"},
    "javascript": {"index": "index"}, "typescript": {"index": "index"}, "tsx": {"index": "index"},
}
# languages that share one namespace, so a name in one may resolve to a symbol of another
FAMILIES = [{"java", "scala", "kotlin", "groovy"}, {"javascript", "typescript", "tsx"}, {"c", "cpp", "objc"}]
FAMILY = {lang: i for i, fam in enumerate(FAMILIES) for lang in fam}
SELF_PREFIX = {"this.", "self.", "this->", "$this->", "self::", "static::", "this::", "@"}
RECEIVER = re.compile(r"([A-Za-z_][\w]*)\s*(?:\.|::|->|\?\.)")
_DIRECTIVE = re.compile(r"\(#[\w-]+!\s[^()]*\)")  # tags-query directives (#strip! ...) that only matter to a tag printer
_IMPORT_WORD = re.compile(r"^\s*(?:pub\s+)?(?:import|use|using|from|include|require)\b\s*(?:static\s+)?")


def language_of(path: str):
    """The grammar name for a path, or None when no grammar knows the file type."""
    try:
        from tree_sitter_language_pack import detect_language_from_path
        return detect_language_from_path(path)
    except Exception:  # the pack is missing: no language is known
        return None


@functools.lru_cache(maxsize=None)
def _grammar(lang: str):
    """(parser, query cursor factory, None) for a language, or (None, None, why not). Compiled once per process."""
    try:
        import tree_sitter_language_pack as pack
        from tree_sitter import Query, QueryCursor
    except Exception:
        return None, None, "tree-sitter is not installed"
    try:
        bundled = pack.get_tags_query(lang) or ""
    except Exception:
        bundled = ""
    extra = QUERIES.get(lang, [])
    if not bundled and not extra:
        return None, None, "no symbol query for the language"
    try:
        language, parser = pack.get_language(lang), pack.get_parser(lang)
    except Exception:  # not downloaded and no network, or an unknown name
        return None, None, "grammar not available"
    good = []
    for pattern in extra:  # a pattern written for another version of the grammar is dropped
        try:
            Query(language, pattern)
            good.append(pattern)
        except Exception:
            pass
    bundled = _DIRECTIVE.sub("", bundled)
    for source in ("\n".join(good + [bundled]), "\n".join(good)):
        if not source.strip():
            continue
        try:
            query = Query(language, source)
        except Exception:
            continue
        caps = {query.capture_name(i) for i in range(query.capture_count)}
        if not any(c.partition(".")[2] in KINDS for c in caps if c.startswith("definition.")):
            break  # a query with nothing we call a symbol (a config or markup language's keys and headings)
        return parser, (lambda: QueryCursor(query)), None
    return None, None, "no symbol query for the language"


def has_symbols(path: str) -> bool:
    """Whether this file type is one the parse reads symbols from (a grammar and a symbol query exist for it)."""
    lang = language_of(path)
    return lang is not None and _grammar(lang)[0] is not None


def module_name(path: str, lang: str) -> str:
    """The qualifier a file's symbols get when the file declares no package: its path, dotted."""
    facts = FACTS.get(lang, {})
    folder, _, name = path.rpartition("/")
    if facts.get("module") == "dir":
        return folder.replace("/", ".")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    parts = [p for p in folder.split("/") if p] + ([] if stem == facts.get("index") and folder else [stem])
    return ".".join(parts)


def _clean(text: str) -> str:
    """A captured name as a dotted name: generics and call arguments dropped, :: and \\ as dots."""
    text = re.sub(r"[<\[(].*", "", text, flags=re.S).strip().strip("`'\"")
    return re.sub(r"\s*(?:::|\\)\s*", ".", text).strip(".") if "/" not in text else text


def _signature(node, source: bytes) -> str:
    """The declaration without its body, on one line."""
    body = node.child_by_field_name("body") or next(
        (c for c in node.children if c.is_named and re.search(r"body$|block$", c.type)), None)
    end = body.start_byte if body is not None and body.start_byte > node.start_byte else node.end_byte
    head = source[node.start_byte:end].decode("utf-8", "replace")
    if body is None:
        head = head.split("\n", 1)[0]
    return re.sub(r"\s+", " ", head).strip().rstrip("{:=").strip()[:300]


def _import_text(text: str):
    """An import statement read as text: "import x.y.{Z, W => V}" -> ("x.y", ["Z", "W"], wildcard)."""
    t = _IMPORT_WORD.sub("", text.strip()).rstrip(";").strip()
    items, wildcard = [], False
    if "{" in t:
        base, _, rest = t.partition("{")
        for item in rest.rsplit("}", 1)[0].split(","):
            item = re.split(r"=>|\bas\b", item)[0].strip()
            if item in ("_", "*"):
                wildcard = True
            elif re.fullmatch(r"[\w.:]+", item or ""):
                items.append(_clean(item))
        t = base
    else:
        t = re.split(r"\s+as\s+|=>", t)[0].strip()
    t = _clean(t.rstrip(".:"))
    if t.endswith(("._", ".*")) or t in ("_", "*"):
        t, wildcard = t[:-2], True
    return t, items, wildcard


def parse(path: str, text: str) -> dict:
    """Everything read from one file: {language, skipped, package, symbols, imports, error}. Never raises
    for a file it cannot read: `skipped` says why."""
    out = {"path": path, "language": None, "skipped": None, "package": None, "symbols": [], "imports": [],
           "error": False}
    lang = out["language"] = language_of(path)
    if lang is None:
        out["skipped"] = "unknown language"
        return out
    parser, cursor, why = _grammar(lang)
    if parser is None:
        out["skipped"] = why
        return out
    source = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
    if len(source) > MAX_BYTES:
        out["skipped"] = "file too large"
        return out
    try:
        tree = parser.parse(source)
        matches = sorted(cursor().matches(tree.root_node), key=lambda m: m[0])  # QUERIES patterns first
    except Exception:
        out["skipped"] = "parse failed"
        return out
    out["error"] = bool(tree.root_node.has_error)
    txt = lambda n: source[n.start_byte:n.end_byte].decode("utf-8", "replace")

    spans, packages, imports, items, refs, calls = {}, [], {}, collections.defaultdict(list), {}, {}
    for _, caps in matches:
        names = caps.get("name") or []
        for cap, nodes in caps.items():
            node = nodes[0]
            if cap.startswith("definition.") or cap in ("scope", "namespace"):
                kind = cap.partition(".")[2] or cap
                key = (node.start_byte, node.end_byte)
                if names and key not in spans and (kind in KINDS or kind in ("scope", "namespace")):
                    recv = caps.get("receiver")
                    spans[key] = {"node": node, "name": _clean(txt(names[0])), "kind": kind,
                                  "receiver": _clean(txt(recv[0])) if recv else None}
            elif cap == "package" and names:
                packages.append((node.start_byte, _clean(txt(names[0]))))
            elif cap in ("import", "import.namespace"):
                key = names[0].start_byte if names else node.start_byte
                if key in imports:
                    continue
                if names:
                    raw = txt(names[0])
                    wild = cap == "import.namespace" or any(
                        c.type in ("asterisk", "namespace_wildcard", "wildcard_import", "wildcard") or txt(c) == "*"
                        for c in node.children)
                    name, got = (raw if "/" in raw or raw.startswith(".") else _clean(raw)), []
                else:
                    name, got, wild = _import_text(txt(node))
                alias = caps.get("alias")
                imports[key] = {"name": name, "items": got, "wildcard": wild, "alias": txt(alias[0]) if alias else None,
                                "line": node.start_point[0] + 1, "_stmt": node.start_byte}
            elif cap == "import.items":
                items[node.start_byte] += [_clean(txt(n)) for n in caps.get("item") or []]
            elif cap in ("reference.extends", "reference.implements", "reference.implementation") and names:
                for n in names:
                    refs.setdefault(n.start_byte, (n, cap.rpartition(".")[2]))
            elif cap in ("reference.call", "reference.send") and names:
                calls.setdefault(names[0].start_byte, (node, names[0]))
    for imp in imports.values():
        for item in items.get(imp.pop("_stmt"), ()):
            if item not in imp["items"]:
                imp["items"].append(item)
    for imp in imports.values():
        imp["items"].sort()
    out["imports"] = sorted((imp for imp in imports.values() if imp["name"] or imp["items"]),
                            key=lambda i: (i["line"], i["name"]))

    package = ".".join(name for _, name in sorted(packages) if name) or None
    out["package"] = package
    prefix = package if package is not None else module_name(path, lang)
    lines = source.count(b"\n") + 1
    # the file itself: a module where the language names things by file, else just the file
    by_file = package is None and FACTS.get(lang, {}).get("module") != "dir" and         not any(s["kind"] == "namespace" for s in spans.values())
    module = {"qualified_name": (prefix or path) if by_file else path,
              "name": path.rsplit("/", 1)[-1], "kind": "module" if by_file else "file",
              "signature": None, "line_start": 1, "line_end": lines, "parent": None, "bases": [], "calls": []}
    symbols = out["symbols"] = [module]

    # one sweep in source order: definitions and scopes open, references and calls land on what is open
    events = [(s["node"].start_byte, 0, -s["node"].end_byte, "def", s) for s in spans.values()]
    events += [(n.start_byte, 1, 0, "ref", (n, role)) for n, role in refs.values()]
    events += [(name.start_byte, 1, 0, "call", (node, name)) for node, name in calls.values()]
    events.sort(key=lambda e: e[:3])
    stack, by_qn = [], {}
    cap_is_absolute = lambda item, outer: (item["kind"] == "namespace" and outer is None) or (outer is None and "." in item["name"])
    for start, _, _, what, item in events:
        while stack and stack[-1]["node"].end_byte <= start:
            stack.pop()
        if what == "def":
            outer = stack[-1] if stack else None
            if item["receiver"]:
                base = f"{prefix}.{item['receiver']}" if prefix else item["receiver"]
            elif cap_is_absolute(item, outer):
                base = None  # a namespace block, or a name written out in full (C++ "void a::B::run()")
            else:
                base = outer["qn"] if outer else prefix
            item["qn"] = f"{base}.{item['name']}" if base else item["name"]
            stack.append(item)
            if item["kind"] in ("scope", "namespace"):
                continue
            kind = item["kind"]
            in_type = bool(item["receiver"]) or (outer is not None and outer["kind"] in TYPE_LIKE)
            if kind in ("function", "method"):  # the grammar's own word is not reliable across languages
                kind = "method" if in_type else "function"
            node = item["node"]
            item["sym"] = {"qualified_name": item["qn"], "name": item["name"].rsplit(".", 1)[-1], "kind": kind,
                           "signature": _signature(node, source), "line_start": node.start_point[0] + 1,
                           "line_end": node.end_point[0] + 1, "parent": base or None, "bases": [], "calls": []}
            symbols.append(item["sym"])
            by_qn.setdefault(item["qn"], item["sym"])
            continue
        if what == "ref":
            node, role = item
            owner = stack[-1] if stack else None
            sym = owner and (owner.get("sym") or by_qn.get(owner["qn"]))  # an impl block stands for its type
            name = _clean(txt(node))
            if sym and name and (name, role) not in sym["bases"] and name != sym["name"]:
                sym["bases"].append((name, role))
            continue
        node, name = item
        owner = next((s["sym"] for s in reversed(stack) if "sym" in s), module)
        before = source[node.start_byte:name.start_byte].decode("utf-8", "replace").strip() if name.start_byte >= node.start_byte else ""
        hit = RECEIVER.fullmatch(before)
        how = ("bare", None) if not before else ("self", None) if before in SELF_PREFIX else \
            ("on", hit.group(1)) if hit else ("expr", None)
        call = (how[0], how[1], txt(name))
        if call not in owner["calls"]:
            owner["calls"].append(call)
    known = {s["qualified_name"] for s in symbols}
    for s in symbols[1:]:  # the parent is a symbol of this file, or nothing (a namespace is not a symbol)
        if s["parent"] not in known or s["parent"] == s["qualified_name"]:
            s["parent"] = None
    return out


def extract(path: str, text: str):
    """(symbols, imports) of one source file; ([], []) for a file type with no grammar or no query.

    A symbol: {qualified_name, name, kind, signature, line_start, line_end, parent, bases, calls}; the first is
    the file itself (kind "module", or "file" when the language names things by package). An import:
    {name, items, wildcard, alias, line}."""
    got = parse(path, text)
    return got["symbols"], got["imports"]


def _parse_chunk(chunk):
    return [parse(path, text) for path, text in chunk]


def parse_many(texts: dict, workers: int = 0) -> dict:
    """{path: parse(path, text)}. workers > 1 parses in that many processes (worth it from a few thousand files)."""
    todo = [(p, t) for p, t in texts.items() if t]
    if workers and workers > 1 and len(todo) > 200:
        import concurrent.futures as cf
        size = max(20, len(todo) // (workers * 8))
        chunks = [todo[i:i + size] for i in range(0, len(todo), size)]
        try:
            with cf.ProcessPoolExecutor(workers) as pool:
                return {r["path"]: r for part in pool.map(_parse_chunk, chunks) for r in part}
        except Exception:  # no process pool here (a frozen or sandboxed interpreter): parse in this one
            pass
    return {p: parse(p, t) for p, t in todo}


def _prefixes(name: str):
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


def map_symbols(g, repo, scope, rid, files: dict, texts: dict, workers: int = 0):
    """CodeSymbols for every file with text, and the links between them. `files` is {path: File record},
    `texts` is {path: source text}; a path in texts with no File record is ignored."""
    parsed = parse_many({p: t for p, t in texts.items() if p in files}, workers)
    notes = g.notes
    recs = {}                                   # (path, index in the file's symbols) -> record
    by_qn = collections.defaultdict(list)       # qualified name -> [(record, language, kind)]
    for path, info in parsed.items():
        if info["skipped"]:
            notes[f"code: files without symbols ({info['skipped']})"] += 1
            continue
        notes["code: source files parsed for symbols"] += 1
        notes[f"code: files parsed, {info['language']}"] += 1
        if info["error"]:
            notes["code: files with a syntax error (parsed as far as the grammar recovers)"] += 1
        seen = collections.Counter()
        for i, s in enumerate(info["symbols"]):
            native = s["qualified_name"]
            if s["kind"] in FUNC_LIKE:  # overloads share a qualified name: the parameter list tells them apart
                params = re.search(r"\(.*\)", s["signature"] or "")
                dup = sum(1 for o in info["symbols"] if o["qualified_name"] == native)
                if dup > 1 and params:
                    native += re.sub(r"\s+", "", params.group(0))[:120]
            seen[native] += 1
            if seen[native] > 1:
                native += f"~{seen[native]}"
            rec = g.rec("CodeSymbol", "git", f"{rid}:{path}#{native}", {
                "qualified_name": s["qualified_name"], "kind": s["kind"], "signature": s["signature"],
                "line_start": s["line_start"], "line_end": s["line_end"]}, scope, acl_from=files[path])
            g.link("DEFINED_IN", rec, files[path], "field")
            recs[path, i] = rec
            by_qn[s["qualified_name"]].append((rec, info["language"], s["kind"]))
            notes[f"code: symbols, {s['kind']}"] += 1
    if not recs:
        return parsed

    def same_family(a, b):
        return a == b or (a in FAMILY and FAMILY[a] == FAMILY.get(b))

    def one(qn, lang, kinds=None):
        """The one symbol with this qualified name that the language can see, or None (none, or several)."""
        hits = [r for r, l, k in by_qn.get(qn, ()) if same_family(lang, l) and (kinds is None or k in kinds)]
        return hits[0] if len(hits) == 1 else None

    def some(qn, lang, kinds=None):
        return sum(1 for _, l, k in by_qn.get(qn, ()) if same_family(lang, l) and (kinds is None or k in kinds))

    # a path-named module is imported from its source root (python/tvm/x.py as tvm.x): each unique dotted suffix
    suffixes = collections.defaultdict(list)
    for path, info in parsed.items():
        if not info["skipped"] and info["symbols"][0]["kind"] == "module":
            parts = info["symbols"][0]["qualified_name"].split(".")
            for i in range(1, len(parts)):
                suffixes[info["language"], ".".join(parts[i:])].append(recs[path, 0])
    path_tail = collections.defaultdict(list)   # "b/c.h" -> the files whose path ends with it
    for path in parsed:
        if not parsed[path]["skipped"]:
            parts = path.split("/")
            for i in range(len(parts)):
                path_tail["/".join(parts[i:])].append(path)
    exts = sorted({"." + p.rsplit(".", 1)[-1] for p in parsed if "." in p.rsplit("/", 1)[-1]})

    def module(name, lang):
        hit = one(name, lang, {"module"})
        if hit is None and len(suffixes.get((lang, name), ())) == 1:
            hit = suffixes[lang, name][0]
        return hit

    def by_path(spec, path, lang):
        """A path import ("./x", "../y/z", "a/b.h") -> the one file it names, as that file's own symbol."""
        folder = posixpath.dirname(path)
        index = FACTS.get(lang, {}).get("index")
        bases = [posixpath.normpath(posixpath.join(folder, spec))]
        if not spec.startswith("."):
            bases.append(None)  # then by a unique path ending
        for base in bases:
            tails = [spec] if base is None else [base]
            cands = []
            for t in tails:
                for c in [t] + [t + e for e in exts] + ([f"{t}/{index}{e}" for e in exts] if index else []):
                    cands += [c] if base is not None and c in parsed else path_tail.get(c, []) if base is None else []
            cands = [c for c in dict.fromkeys(cands) if not parsed[c]["skipped"] and same_family(lang, parsed[c]["language"])]
            if len(cands) == 1:
                return recs[cands[0], 0]
            if cands:
                return None
        return None

    def relative(name, info):
        """Python-style ".x" / "..pkg.y" -> the absolute dotted name, from the importing file's own module name."""
        dots = len(name) - len(name.lstrip("."))
        own = module_name(info["path"], info["language"]).split(".")
        is_index = info["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0] == FACTS.get(info["language"], {}).get("index")
        base = own[:len(own) - (dots - (1 if is_index else 0))] if dots - (1 if is_index else 0) > 0 else own
        rest = name.lstrip(".")
        return ".".join(base + ([rest] if rest else []))

    scopes = {}  # path -> what its imports bind: {"bound": {name: record}, "wild": [package, ...]}
    for path, info in parsed.items():
        if info["skipped"]:
            continue
        lang, me = info["language"], recs[path, 0]
        bound, wild = {}, []
        scopes[path] = {"bound": bound, "wild": wild}
        for imp in info["imports"]:
            name, target, exact = imp["name"], None, None
            if "/" in name or (name.startswith(".") and not re.fullmatch(r"\.+[\w.]*", name)) or re.search(r"\.\w{1,4}$", name) and one(name, lang) is None and name in path_tail:
                target = exact = by_path(name, path, lang)
            else:
                if name.startswith("."):
                    name = relative(name, info)
                exact = one(name, lang) or module(name, lang)
                target = exact
                if target is None and not imp["wildcard"]:
                    for c in _prefixes(name)[1:]:  # a member import names its type; a submodule its package
                        target = one(c, lang) or module(c, lang)
                        if target is not None:
                            break
                if imp["wildcard"]:
                    wild.append(exact["props"]["qualified_name"] if exact else name)
            for item in imp["items"]:  # from m import f / import a.b.{C, D}
                base = exact["props"]["qualified_name"] if exact else name
                hit = one(f"{base}.{item}", lang) or module(f"{base}.{item}", lang)
                if hit is not None:
                    bound[item.rsplit(".", 1)[-1]] = hit
                    if target is None:
                        target = hit
                    elif hit["props"]["kind"] == "module" and hit is not me:  # from pkg import submodule
                        g.link("IMPORTS", me, hit, "field", kind="import")
            if exact is not None and not imp["wildcard"] and not imp["items"] and "/" not in name:
                local = imp["alias"] or (name.rsplit(".", 1)[-1] if FACTS.get(lang, {}).get("binds") != "first"
                                         or "." not in name else None)
                if local:
                    bound[local] = exact
            elif exact is not None and imp["alias"]:
                bound[imp["alias"]] = exact
            if target is not None and target is not me:
                g.link("IMPORTS", me, target, "field", kind="import")
                notes["code: imports resolved to a symbol of the repository"] += 1
            elif target is None:
                notes["code: imports of something outside the repository, a package, or an ambiguous name (not linked)"] += 1

    def chain(qn, stop):
        """qn and its enclosing names, innermost first, down to (not including) the file's own qualifier."""
        out = []
        while qn and qn != stop:
            out.append(qn)
            qn = qn.rpartition(".")[0]
        return out

    def find_type(name, sym, info, kinds=CLASS_LIKE):
        lang, sc = info["language"], scopes[info["path"]]
        prefix = info["package"] if info["package"] is not None else module_name(info["path"], lang)
        head, _, rest = name.partition(".")
        if rest:  # a.b.C: fully qualified, or through an imported name
            hit = one(name, lang, kinds)
            if hit is None and head in sc["bound"]:
                hit = one(f"{sc['bound'][head]['props']['qualified_name']}.{rest}", lang, kinds)
            return hit
        for outer in chain(sym["qualified_name"], prefix):  # a nested or sibling type
            n = some(f"{outer}.{name}", lang, kinds)
            if n:
                return one(f"{outer}.{name}", lang, kinds)
        if name in sc["bound"]:
            return sc["bound"][name]
        if prefix and some(f"{prefix}.{name}", lang, kinds):  # the same package (or the same file)
            return one(f"{prefix}.{name}", lang, kinds)
        hits = [h for h in (one(f"{w}.{name}", lang, kinds) for w in sc["wild"]) if h is not None]
        return hits[0] if len(hits) == 1 else None

    for path, info in parsed.items():
        if info["skipped"]:
            continue
        lang, sc = info["language"], scopes[path]
        prefix = info["package"] if info["package"] is not None else module_name(path, lang)
        implicit = FACTS.get(lang, {}).get("implicit_self", False)
        kind_of = {s["qualified_name"]: s["kind"] for s in info["symbols"]}
        for i, s in enumerate(info["symbols"]):
            me = recs[path, i]
            for base, role in s["bases"]:
                target = find_type(base, s, info)
                if target is not None and target is not me:
                    g.link("EXTENDS", me, target, "field", kind="extends", role=role if role != "implementation" else None)
                    notes["code: supertypes resolved to a symbol of the repository"] += 1
                else:
                    notes["code: supertypes outside the repository or ambiguous (not linked)"] += 1
            for how, recv, name in s["calls"]:
                target, tried = None, False
                if how == "expr":
                    notes["code: calls through an expression (no type inference, not linked)"] += 1
                    continue
                if how == "bare":
                    for outer in chain(s["qualified_name"], prefix):
                        if kind_of.get(outer) in FUNC_LIKE or (implicit and kind_of.get(outer) in CLASS_LIKE):
                            if some(f"{outer}.{name}", lang):
                                target, tried = one(f"{outer}.{name}", lang), True
                                break
                    if not tried and name in sc["bound"]:
                        target, tried = sc["bound"][name], True
                    if not tried and prefix and some(f"{prefix}.{name}", lang):
                        target, tried = one(f"{prefix}.{name}", lang), True
                elif how == "self":
                    owner = next((o for o in chain(s["qualified_name"], prefix)
                                  if kind_of.get(o) in CLASS_LIKE and o != s["qualified_name"]), None)
                    if owner and some(f"{owner}.{name}", lang):
                        target, tried = one(f"{owner}.{name}", lang), True
                else:
                    holder = find_type(recv, s, info)
                    if holder is not None:
                        qn = f"{holder['props']['qualified_name']}.{name}"
                        if some(qn, lang):
                            target, tried = one(qn, lang), True
                if target is not None and target is not me:
                    g.link("CALLS", me, target, "inferred", 0.85, kind="call")
                    notes["code: calls resolved to a symbol of the repository"] += 1
                elif tried and target is None:
                    notes["code: calls with several candidates, e.g. overloads (not linked)"] += 1
                elif target is None:
                    notes["code: calls to something outside the repository or not settled by name (not linked)"] += 1
    return parsed
