"""Tests for the language-neutral code parse (ekg_adapters/symbols.py): one extractor, many languages.

Part 1 reads small snippets in Java, Scala, Python, TypeScript, Go and C# and checks the symbols, their
nesting and the imports. Part 2 builds a small mixed-language repository through map_symbols and checks the
links (DEFINED_IN, IMPORTS, EXTENDS, CALLS) and that every record and link validates.

    python -I ekg-eval-data/test_symbols.py
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters import common, symbols  # noqa: E402
from ekg_adapters.access import place  # noqa: E402

JAVA = """package org.acme.pay;

import java.util.List;
import org.acme.util.Money;
import static org.acme.util.Money.zero;
import org.acme.audit.*;

public class Ledger<T> extends Book<T> implements Auditable, java.io.Serializable {
    Ledger() {}
    enum Mode { OPEN, CLOSED }
    record Entry(int amount) {}
    static class Page { void turn() {} }
    public Money total(List<T> rows) { check(); this.check(); Money.zero(); rows.size(); return zero(); }
    void check() {}
    void post(int a) {}
    void post(int a, int b) {}
}
interface Auditable extends Checked {}
"""
SCALA = """package org.acme
package stream

import org.acme.util.{Money, Clock => C}
import scala.collection._

class Pipe(n: Int) extends Stage with Logging {
  def run(a: Int): Int = { step(a); this.close(); Money.zero() }
  def step(a: Int): Int = a
  def close(): Unit = ()
}
object Pipe { def apply(): Pipe = new Pipe(1) }
trait Stage extends Base
case class Item(a: Int)
"""
PYTHON = """import os.path as osp, sys
from tvm.tir import shape as sh, expr
from . import sibling
from ..core import base

class RelaxPass(base.Pass, Mixin):
    def run(self, mod):
        self.check(mod); make_pass(); osp.join()
    def check(self, mod):
        pass

def make_pass():
    def inner():
        pass
    return inner()
"""
TS = """import { parse, format as fmt } from './dates';
import Api from '../net/api';
import * as lodash from 'lodash';

export class Cart extends Basket implements Priced {
  total(): number { this.sum(); return round(1); }
  sum() {}
}
export function round(n: number) { return n; }
export const twice = (n: number) => n * 2;
enum Mode { A, B }
interface Priced extends Named {}
type Id = string;
namespace Inner { export function deep() {} }
"""
GO = """package store

import (
	"fmt"
	kv "github.com/acme/shop/kv"
)

type Shelf struct{ Size int }
type Reader interface{ Read() }

func (s *Shelf) Fill(n int) { fmt.Println(n); helper() }
func helper() {}
"""
CSHARP = """using System;
using Acme.Core;

namespace Acme.Shop {
    public class Order : Entity, IPriced {
        public Order() {}
        public decimal Total() { this.Check(); Check(); Tax.Rate(); return 0; }
        void Check() {}
        struct Line {}
        enum State { Open }
    }
    interface IPriced {}
}
"""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    def read(path, text):
        syms, imports = symbols.extract(path, text)
        return {s["qualified_name"]: s for s in syms}, syms, {i["name"]: i for i in imports}

    # ---- Java: package-qualified names, nesting, overloads, extends / implements, imports
    by, syms, imps = read("pay/src/main/java/org/acme/pay/Ledger.java", JAVA)
    kinds = {q: s["kind"] for q, s in by.items()}
    check("java: the file, class, constructor, enum, record, nested class, methods and interface are symbols",
          kinds.get("pay/src/main/java/org/acme/pay/Ledger.java") == "file"
          and {"org.acme.pay.Ledger": "class", "org.acme.pay.Ledger.Ledger": "constructor",
               "org.acme.pay.Ledger.Mode": "enum", "org.acme.pay.Ledger.Entry": "record",
               "org.acme.pay.Ledger.Page": "class", "org.acme.pay.Ledger.Page.turn": "method",
               "org.acme.pay.Ledger.total": "method", "org.acme.pay.Auditable": "interface"}.items() <= kinds.items())
    check("java: a method's parent is its class, a nested class's method the nested class",
          by["org.acme.pay.Ledger.total"]["parent"] == "org.acme.pay.Ledger"
          and by["org.acme.pay.Ledger.Page.turn"]["parent"] == "org.acme.pay.Ledger.Page")
    check("java: signature and lines", by["org.acme.pay.Ledger.total"]["signature"] == "public Money total(List<T> rows)"
          and by["org.acme.pay.Ledger"]["line_start"] == 8 and by["org.acme.pay.Ledger"]["line_end"] == 17)
    check("java: both overloads are kept", sum(1 for s in syms if s["qualified_name"] == "org.acme.pay.Ledger.post") == 2)
    check("java: extends and implements are told apart, generics dropped",
          by["org.acme.pay.Ledger"]["bases"] == [("Book", "extends"), ("Auditable", "implements"), ("java.io.Serializable", "implements")]
          and by["org.acme.pay.Auditable"]["bases"] == [("Checked", "extends")])
    check("java: imports, with the wildcard one marked",
          set(imps) == {"java.util.List", "org.acme.util.Money", "org.acme.util.Money.zero", "org.acme.audit"}
          and imps["org.acme.audit"]["wildcard"] and not imps["org.acme.util.Money"]["wildcard"])
    check("java: calls are recorded as bare, on this, on a name, or through an expression",
          {("bare", None, "check"), ("self", None, "check"), ("on", "Money", "zero"), ("on", "rows", "size"),
           ("bare", None, "zero")} <= set(by["org.acme.pay.Ledger.total"]["calls"]))

    # ---- Scala
    by, syms, imps = read("streams/src/main/scala/org/acme/stream/Pipe.scala", SCALA)
    kinds = {(s["qualified_name"], s["kind"]) for s in syms}
    check("scala: two package clauses chain; class, object, trait, case class and methods are symbols",
          {("org.acme.stream.Pipe", "class"), ("org.acme.stream.Pipe", "object"), ("org.acme.stream.Stage", "trait"),
           ("org.acme.stream.Item", "class"), ("org.acme.stream.Pipe.run", "method"),
           ("org.acme.stream.Pipe.apply", "method")} <= kinds)
    pipe = next(s for s in syms if s["qualified_name"] == "org.acme.stream.Pipe" and s["kind"] == "class")
    check("scala: 'extends Stage with Logging' gives both supertypes",
          [b for b, _ in pipe["bases"]] == ["Stage", "Logging"])
    check("scala: a selector import gives its items; a wildcard import is marked",
          imps["org.acme.util"]["items"] == ["Clock", "Money"] and imps["scala.collection"]["wildcard"])

    # ---- Python
    by, syms, imps = read("python/tvm/relax/pass.py", PYTHON)
    check("python: module, class, methods, function and nested function, named from the path",
          {q: s["kind"] for q, s in by.items()} == {
              "python.tvm.relax.pass": "module", "python.tvm.relax.pass.RelaxPass": "class",
              "python.tvm.relax.pass.RelaxPass.run": "method", "python.tvm.relax.pass.RelaxPass.check": "method",
              "python.tvm.relax.pass.make_pass": "function", "python.tvm.relax.pass.make_pass.inner": "function"})
    check("python: nesting", by["python.tvm.relax.pass.RelaxPass.run"]["parent"] == "python.tvm.relax.pass.RelaxPass"
          and by["python.tvm.relax.pass.make_pass.inner"]["parent"] == "python.tvm.relax.pass.make_pass")
    check("python: imports (plain, aliased, from, relative) with the names they bring in",
          set(imps) == {"os.path", "sys", "tvm.tir", ".", "..core"} and imps["os.path"]["alias"] == "osp"
          and imps["tvm.tir"]["items"] == ["expr", "shape"] and imps["."]["items"] == ["sibling"])
    check("python: base classes", by["python.tvm.relax.pass.RelaxPass"]["bases"] == [("base.Pass", "extends"), ("Mixin", "extends")])
    check("python: __init__.py is its folder's module", symbols.module_name("python/tvm/__init__.py", "python") == "python.tvm")

    # ---- TypeScript
    by, syms, imps = read("web/src/cart.ts", TS)
    check("typescript: class, methods, function, arrow function, enum, interface, type and namespace",
          {"web.src.cart.Cart": "class", "web.src.cart.Cart.total": "method", "web.src.cart.round": "function",
           "web.src.cart.twice": "function", "web.src.cart.Mode": "enum", "web.src.cart.Priced": "interface",
           "web.src.cart.Id": "type", "web.src.cart.Inner": "module", "web.src.cart.Inner.deep": "function"}.items()
          <= {q: s["kind"] for q, s in by.items()}.items())
    check("typescript: extends / implements, and nesting",
          by["web.src.cart.Cart"]["bases"] == [("Basket", "extends"), ("Priced", "implements")]
          and by["web.src.cart.Cart.total"]["parent"] == "web.src.cart.Cart"
          and by["web.src.cart.Inner.deep"]["parent"] == "web.src.cart.Inner")
    check("typescript: imports are the module paths, with the named items",
          set(imps) == {"./dates", "../net/api", "lodash"} and imps["./dates"]["items"] == ["format", "parse"])
    tsx, _ = symbols.extract("web/src/View.tsx", "export function View() { return <div>{twice(1)}</div>; }\n")
    check("tsx: parsed with the JSX grammar", [s["qualified_name"] for s in tsx] == ["web.src.View", "web.src.View.View"])

    # ---- Go
    by, syms, imps = read("shop/store/shelf.go", GO)
    check("go: struct, interface, function, and a method under its receiver type; names qualified by the folder",
          {q: s["kind"] for q, s in by.items()} == {
              "shop/store/shelf.go": "file", "shop.store.Shelf": "struct", "shop.store.Reader": "interface",
              "shop.store.Shelf.Fill": "method", "shop.store.helper": "function"}
          and by["shop.store.Shelf.Fill"]["parent"] == "shop.store.Shelf")
    check("go: imports with their alias", set(imps) == {"fmt", "github.com/acme/shop/kv"}
          and imps["github.com/acme/shop/kv"]["alias"] == "kv")

    # ---- C#
    by, syms, imps = read("src/Shop/Order.cs", CSHARP)
    check("c#: namespace-qualified class, constructor, methods, struct, enum and interface",
          {"Acme.Shop.Order": "class", "Acme.Shop.Order.Order": "constructor", "Acme.Shop.Order.Total": "method",
           "Acme.Shop.Order.Line": "struct", "Acme.Shop.Order.State": "enum", "Acme.Shop.IPriced": "interface"}.items()
          <= {q: s["kind"] for q, s in by.items()}.items() and by["Acme.Shop.Order.Total"]["parent"] == "Acme.Shop.Order")
    check("c#: usings are namespace imports; the base list is read",
          set(imps) == {"System", "Acme.Core"} and imps["Acme.Core"]["wildcard"]
          and [b for b, _ in by["Acme.Shop.Order"]["bases"]] == ["Entity", "IPriced"])
    by2, _, _ = read("src/Shop/Tax.cs", "namespace Acme.Shop;\nclass Tax { static void Rate() {} }\n")
    check("c#: a file-scoped namespace qualifies the whole file", "Acme.Shop.Tax.Rate" in by2)

    # ---- files that are not code, or not in any grammar
    check("an unknown extension gives nothing, cleanly", symbols.extract("data/blob.xyz", "\x00\x01 not code") == ([], []))
    check("a known file type with no symbol query (JSON) gives nothing", symbols.extract("conf/app.json", '{"a": 1}') == ([], []))
    check("...and says why", symbols.parse("data/blob.xyz", "x")["skipped"] == "unknown language"
          and symbols.parse("conf/app.json", "{}")["skipped"] == "no symbol query for the language")
    broken, _ = symbols.extract("a/Broken.java", "package a;\nclass Broken { void ok() {} void bad( { }\n")
    check("a file with a syntax error still gives the symbols the grammar recovers",
          "a.Broken" in {s["qualified_name"] for s in broken})

    # ---- a small repository through map_symbols: records, links, validation
    common.bind(HERE.parent / "query-master")
    common.set_tenant("acme")
    g = common.Graph("2026-10-05T00:00:00Z")
    scope, _ = place(g, "github", "repo", "acme/shop")
    repo = g.rec("Repository", "github", "acme/shop", {"name": "acme/shop"}, scope)
    texts = {
        "pay/src/main/java/org/acme/pay/Ledger.java": JAVA,
        "pay/src/main/java/org/acme/pay/Book.java": "package org.acme.pay;\npublic class Book<T> { }\n",
        "util/src/main/java/org/acme/util/Money.java":
            "package org.acme.util;\npublic class Money { public static Money zero() { return null; } }\n",
        "audit/src/main/java/org/acme/audit/Checked.java": "package org.acme.audit;\npublic interface Checked {}\n",
        "streams/src/main/scala/org/acme/stream/Pipe.scala": SCALA,
        "python/tvm/relax/pass.py": PYTHON,
        "python/tvm/relax/sibling.py": "def helper():\n    pass\n",
        "python/tvm/relax/__init__.py": "# package\n",
        "python/tvm/core/base.py": "class Pass:\n    pass\n",
        "python/tvm/tir/shape.py": "def of(x):\n    return x\n",
        "web/src/cart.ts": TS,
        "web/src/dates.ts": "export function parse(s: string) { return s; }\n",
        "web/net/api.ts": "export default class Api {}\n",
        "shop/store/shelf.go": GO,
        "shop/store/util.go": "package store\n\nfunc Count() int { helper(); return 1 }\n",
        "src/Shop/Order.cs": CSHARP,
        "src/Shop/Tax.cs": "namespace Acme.Shop;\nclass Tax { public static void Rate() {} }\n",
        "src/Core/Entity.cs": "namespace Acme.Core { public class Entity {} }\n",
        "data/blob.xyz": "not code",
        "conf/app.json": "{}",
    }
    files = {p: g.rec("File", "git", f"acme/shop:{p}", common.file_props(p), scope, acl_from=repo) for p in texts}
    symbols.map_symbols(g, repo, scope, "acme/shop", files, texts)
    recs = [r for r in g.records.values() if r["type"] == "CodeSymbol"]
    by_id = {r["id"]: r for r in g.records.values()}

    def sym(qn, kind=None):
        hits = [r for r in recs if r["props"]["qualified_name"] == qn and kind in (None, r["props"]["kind"])]
        assert hits, f"no symbol {qn}"
        return hits[0]

    def linked(ltype, a, b, **props):
        return [lk for lk, _, _ in g.links.values() if lk["type"] == ltype and lk["a"] == a["id"] and lk["b"] == b["id"]
                and all(lk["props"].get(k) == v for k, v in props.items())]

    def targets(ltype, a):
        return {by_id[lk["b"]]["props"]["qualified_name"] for lk, _, _ in g.links.values()
                if lk["type"] == ltype and lk["a"] == a["id"]}

    rec_report, link_report, _ = g.validate()
    check("repo: every record and link validates",
          all(v["checked"] == v["valid"] for v in rec_report.values()) and all(v["checked"] == v["valid"] for v in link_report.values()))
    check("repo: every symbol is DEFINED_IN its file",
          all(any(lk["type"] == "DEFINED_IN" and lk["a"] == r["id"] for lk, _, _ in g.links.values()) for r in recs)
          and linked("DEFINED_IN", sym("org.acme.pay.Ledger.total"), files["pay/src/main/java/org/acme/pay/Ledger.java"]))
    check("repo: the two Java overloads are two records", sum(1 for r in recs if r["props"]["qualified_name"] == "org.acme.pay.Ledger.post") == 2)
    ledger_file = sym("pay/src/main/java/org/acme/pay/Ledger.java")
    check("java: the file IMPORTS the in-repo class and the statically imported method; the JDK import is not linked",
          targets("IMPORTS", ledger_file) == {"org.acme.util.Money", "org.acme.util.Money.zero"})
    check("java: Ledger EXTENDS Book (same package), role extends",
          linked("EXTENDS", sym("org.acme.pay.Ledger"), sym("org.acme.pay.Book"), kind="extends", role="extends"))
    check("java: Ledger EXTENDS Auditable (same file), role implements; the JDK interface is not linked",
          linked("EXTENDS", sym("org.acme.pay.Ledger"), sym("org.acme.pay.Auditable"), role="implements")
          and targets("EXTENDS", sym("org.acme.pay.Ledger")) == {"org.acme.pay.Book", "org.acme.pay.Auditable"})
    check("java: Auditable EXTENDS Checked through the wildcard import",
          linked("EXTENDS", sym("org.acme.pay.Auditable"), sym("org.acme.audit.Checked")))
    check("java: total() CALLS check() (bare and this.), Money.zero() on the imported class and through the static import",
          targets("CALLS", sym("org.acme.pay.Ledger.total")) == {"org.acme.pay.Ledger.check", "org.acme.util.Money.zero"})
    check("java: a call on a variable (rows.size()) is not linked",
          g.notes["code: calls to something outside the repository or not settled by name (not linked)"] > 0)
    pipe = sym("org.acme.stream.Pipe", "class")
    check("scala: Pipe EXTENDS the trait of its file; run() CALLS step() and close(); Money.zero() reaches the Java class",
          linked("EXTENDS", pipe, sym("org.acme.stream.Stage"))
          and targets("CALLS", sym("org.acme.stream.Pipe.run")) == {"org.acme.stream.Pipe.step", "org.acme.stream.Pipe.close",
                                                                    "org.acme.util.Money.zero"})
    check("scala: the file IMPORTS the Java class its selector names",
          "org.acme.util.Money" in targets("IMPORTS", sym("streams/src/main/scala/org/acme/stream/Pipe.scala")))
    py = sym("python.tvm.relax.pass")
    check("python: imports resolve under the source root (tvm.tir.shape) and relatively (the package and its "
          "submodule, ..core.base); os.path and sys are not linked",
          targets("IMPORTS", py) == {"python.tvm.tir.shape", "python.tvm.relax", "python.tvm.relax.sibling",
                                     "python.tvm.core.base"})
    check("python: RelaxPass EXTENDS base.Pass through the imported module",
          linked("EXTENDS", sym("python.tvm.relax.pass.RelaxPass"), sym("python.tvm.core.base.Pass")))
    check("python: run() CALLS self.check() and the module's make_pass(); make_pass() CALLS its nested function",
          targets("CALLS", sym("python.tvm.relax.pass.RelaxPass.run")) == {"python.tvm.relax.pass.RelaxPass.check",
                                                                            "python.tvm.relax.pass.make_pass"}
          and targets("CALLS", sym("python.tvm.relax.pass.make_pass")) == {"python.tvm.relax.pass.make_pass.inner"})
    cart = sym("web.src.cart")
    check("typescript: relative imports resolve to the files they name; the package import is not linked",
          targets("IMPORTS", cart) == {"web.src.dates", "web.net.api"})
    check("typescript: total() CALLS this.sum() and the file's round(); Cart EXTENDS only what the repository has",
          targets("CALLS", sym("web.src.cart.Cart.total")) == {"web.src.cart.Cart.sum", "web.src.cart.round"}
          and targets("EXTENDS", sym("web.src.cart.Cart")) == {"web.src.cart.Priced"})
    check("go: a bare call reaches a function of the same package in another file",
          targets("CALLS", sym("shop.store.Count")) == {"shop.store.helper"}
          and targets("CALLS", sym("shop.store.Shelf.Fill")) == {"shop.store.helper"})
    check("c#: Order EXTENDS Entity through the using, and IPriced of its namespace",
          targets("EXTENDS", sym("Acme.Shop.Order")) == {"Acme.Core.Entity", "Acme.Shop.IPriced"})
    check("c#: Total() CALLS Check() and Tax.Rate() of the same namespace in another file",
          targets("CALLS", sym("Acme.Shop.Order.Total")) == {"Acme.Shop.Order.Check", "Acme.Shop.Tax.Rate"})
    check("repo: files with no grammar or no query are counted, not errors",
          g.notes["code: files without symbols (unknown language)"] == 1
          and g.notes["code: files without symbols (no symbol query for the language)"] == 1)
    serial = {p: [s["qualified_name"] for s in r["symbols"]] for p, r in symbols.parse_many(texts).items()}
    many = {f"{i}/{p}": t for i in range(15) for p, t in texts.items()}
    pooled = {p: [s["qualified_name"] for s in r["symbols"]] for p, r in symbols.parse_many(many, workers=2).items()}
    check("parsing in worker processes gives the same symbols",
          len(pooled) == len(many) and all(len(pooled[f"0/{p}"]) == len(v) for p, v in serial.items()))

    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed ({len(recs)} symbols, "
          f"{sum(1 for lk, _, _ in g.links.values() if lk['type'] in ('IMPORTS', 'EXTENDS', 'CALLS'))} import / extends / call links)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
