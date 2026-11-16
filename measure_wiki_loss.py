"""What the wiki reader loses, before and after: counted on every downloaded Confluence page.

    python -I ekg-eval-data/measure_wiki_loss.py [--data ekg-eval-raw/apache-kafka] [--history]

"before" is the reader the adapters used until now (an HTMLParser that kept headings and text, copied
below so the comparison stays runnable); "after" is ekg_adapters.doctext.read_confluence. Each row is
something the page source holds, and how many of them cannot be found in what the reader returned:

  code blocks          CDATA bodies of code / noformat macros whose text is not in the output
  page links           <ri:page ri:content-title=..> whose title is neither in the text nor a ref
  attachment names     <ri:attachment ri:filename=..>, likewise
  images               <ac:image> whose file name or address is gone
  user mentions        <ri:user ri:userkey=..> with no trace
  glued table cells    neighbouring cells whose words run together with nothing between them
  leaked parameters    macro settings (<ac:parameter>) printed as if they were text. A parameter that is
                       content (a jira key, a status word, a title, a diagram or file name) must be
                       kept instead, and is counted under "content parameters lost" when it is not.
  characters           non-space characters of text in the output, for scale
"""

import argparse
import collections
import glob
import html
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ekg_adapters.doctext import read_confluence  # noqa: E402


class _LegacySections(HTMLParser):
    """The reader in use before doctext (ekg_adapters.common._Sections), unchanged."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections, self.level, self.heading, self.buf, self.in_h = [], 0, "", [], None
        self.href, self.anchor_at = None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.in_h, self.heading = int(tag[1]), ""
        elif tag in ("p", "li", "tr", "br", "div"):
            self.buf.append("\n")
        elif tag == "a" and a.get("href"):
            self.href, self.anchor_at = a["href"], len(self.buf)
        elif tag == "ri:url" and a.get("ri:value"):
            self.buf.append(f" <{a['ri:value']}>")

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            words = "".join(self.buf[self.anchor_at:])
            if self.href not in words and not self.href.startswith("#"):
                self.buf.append(f" <{self.href}>")
            self.href = None
        if self.in_h and tag == f"h{self.in_h}":
            self.level, self.in_h = self.in_h, None

    def handle_data(self, data):
        if self.in_h:
            self.heading += data
        else:
            self.buf.append(data)

    def _flush(self):
        text = re.sub(r"\n\s*\n+", "\n", "".join(self.buf)).strip()
        if text or self.heading:
            self.sections.append((self.level, html.unescape(self.heading.strip()), text))
        self.buf = []

    def close(self):
        super().close()
        self._flush()


def legacy_text(body):
    p = _LegacySections()
    p.feed(body or "")
    p.close()
    return "\n".join(f"{h}\n{t}" if h else t for _, h, t in p.sections).strip()


CDATA = re.compile(r"<ac:plain-text-body>\s*((?:<!\[CDATA\[.*?\]\]>)+)\s*</ac:plain-text-body>", re.S)
PARAM = re.compile(r"(<ac:parameter\b[^>]*>)([^<]*)(</ac:parameter>)")
MACRO_OPEN = re.compile(r"<ac:(?:structured-)?macro\b[^>]*?ac:name=\"([^\"]+)\"[^>]*>")
CELL_PAIR = re.compile(r"<t[dh]\b[^>]*>((?:(?!<t[dh]\b|<table\b).)*?)</t[dh]>\s*(?=<t[dh]\b[^>]*>((?:(?!<t[dh]\b|<table\b).)*?)</t[dh]>)", re.S)
CONTENT_PARAMS = {("jira", "key"), ("status", "title"), ("code", "title"), ("noformat", "title"), ("info", "title"),
                  ("note", "title"), ("warning", "title"), ("tip", "title"), ("panel", "title"), ("expand", "title"),
                  ("gliffy", "name"), ("drawio", "diagramName"), ("drawio-sketch", "diagramName"), ("widget", "url"),
                  ("plantuml", "title"), ("plantumlrender", "title"), ("auiprogressstepstatic", "title")}
squash = lambda s: re.sub(r"\s+", "", s)
words = lambda s: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def tag_params(body):
    """Replace every plain macro parameter value with a marker, so a leak can be counted exactly.
    Returns (body, [(marker, macro, name, is content)])."""
    marks, out, pos, macro = [], [], 0, None
    opens = [(m.start(), m.group(1).lower()) for m in MACRO_OPEN.finditer(body)]
    k = 0
    for m in PARAM.finditer(body):
        while k < len(opens) and opens[k][0] < m.start():
            macro = opens[k][1]
            k += 1
        value = m.group(2)
        if not value.strip():
            continue
        name = re.search(r'ac:name="([^"]*)"', m.group(1))
        name = name.group(1) if name else ""
        mark = f"QZPARAM{len(marks)}ZQ"
        marks.append((mark, macro, name, (macro, name) in CONTENT_PARAMS))
        out.append(body[pos:m.start()] + m.group(1) + mark + m.group(3))
        pos = m.end()
    out.append(body[pos:])
    return "".join(out), marks


def measure(body, readers, tally):
    marked, marks = tag_params(body)
    outs = {}
    for name, fn in readers.items():
        text, refs = fn(marked)
        outs[name] = (text, squash(text), refs)
    codes = []
    for m in CDATA.finditer(marked):
        code = "".join(re.findall(r"<!\[CDATA\[(.*?)\]\]>", m.group(1), re.S))
        if code.strip():
            codes.append(code)
    pages = [html.unescape(t) for t in re.findall(r'<ri:page\b[^>]*?ri:content-title="([^"]*)"', marked)]
    files = [html.unescape(t) for t in re.findall(r'<ri:attachment\b[^>]*?ri:filename="([^"]*)"', marked)]
    images = [html.unescape(m.group(1) or m.group(2)) for m in re.finditer(
        r'<ac:image\b[^>]*>\s*(?:<ri:attachment\b[^>]*?ri:filename="([^"]*)"|<ri:url\b[^>]*?ri:value="([^"]*)")', marked)]
    users = re.findall(r'<ri:user\b[^>]*?ri:userkey="([^"]*)"', marked)
    pairs = []
    for m in CELL_PAIR.finditer(marked):
        a, b = words(CDATA.sub(" ", m.group(1))), words(CDATA.sub(" ", m.group(2)))
        if a and b and "QZPARAM" not in a + b:
            pairs.append((a.split(" ")[-1], b.split(" ")[0]))
    for name, (text, flat, refs) in outs.items():
        t = tally[name]
        t["pages"] += 1
        t["characters"] += len(flat)
        for kind, items, in_refs in (("code blocks", codes, None), ("page links", pages, "page"),
                                     ("attachment names", files, "attachment"), ("images", images, "image"),
                                     ("user mentions", users, "user")):
            t[kind + " (total)"] += len(items)
            for it in items:
                found = (squash(it) in flat) if kind == "code blocks" else (it in text)
                if not found and in_refs:
                    found = it in refs.get(in_refs, ()) or (in_refs == "attachment" and it in refs.get("image", ()))
                t[kind] += not found
        if codes:
            t["code characters (total)"] += sum(len(squash(c)) for c in codes)
            t["code characters"] += sum(len(squash(c)) for c in codes if squash(c) not in flat)
        t["glued table cells (total)"] += len(pairs)
        # glued: the two words stand together in the output although the source has them in two cells
        t["glued table cells"] += sum(1 for a, b in pairs if a + b in text and a + b not in html.unescape(marked))
        for mark, macro, pname, content in marks:
            kind = "content parameters lost" if content else "leaked parameters"
            t[kind + " (total)"] += 1
            shown = mark in text or any(mark in r for rs in refs.values() for r in rs)
            t[kind] += (not shown) if content else (mark in text)


def after(body):
    doc = read_confluence(body)
    refs = collections.defaultdict(set)
    for r in doc.refs():
        refs[r.kind].add(r.target)
        if r.extra.get("page"):   # an attachment that hangs on another page names that page
            refs["page"].add(r.extra["page"])
    return doc.text, refs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=str(HERE.parent / "ekg-eval-raw" / "apache-kafka"))
    ap.add_argument("--history", action="store_true", help="also every older version body in wiki/history")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    readers = {"before": lambda b: (legacy_text(b), {}), "after": after}
    tally = {k: collections.Counter() for k in readers}
    t0, quality = time.time(), collections.Counter()
    bodies = []
    for f in sorted(glob.glob(str(Path(args.data) / "wiki" / "pages" / "*.json"))):
        bodies += [r["body"]["storage"]["value"] for r in json.load(open(f, encoding="utf-8"))["results"]]
    if args.history:
        for f in sorted(glob.glob(str(Path(args.data) / "wiki" / "history" / "*.json"))):
            bodies += [b["body"]["storage"]["value"] for b in json.load(open(f, encoding="utf-8")).get("version_bodies", [])]
    for body in bodies:
        measure(body, readers, tally)
        q = read_confluence(body).quality
        for k, v in q.unknown.items():
            quality["not understood: " + k] += v
        for k, v in q.dropped.items():
            quality["left out on purpose: " + k] += v
    rows = ["code blocks", "code characters", "page links", "attachment names", "images", "user mentions",
            "glued table cells", "leaked parameters", "content parameters lost"]
    print(f"{len(bodies):,} page bodies, {sum(len(b) for b in bodies):,} characters of source, {time.time() - t0:.0f} s\n")
    print(f"{'lost / of':<26}{'before':>24}{'after':>24}")
    for r in rows:
        cells = [f"{tally[k][r]:,} of {tally[k][r + ' (total)']:,}" for k in readers]
        print(f"{r:<26}{cells[0]:>24}{cells[1]:>24}")
    print(f"{'characters of text kept':<26}{tally['before']['characters']:>24,}{tally['after']['characters']:>24,}")
    print("\nWhat the new reader reports about these pages:")
    for k, v in sorted(quality.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {v:>7,}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
