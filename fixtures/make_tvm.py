"""Write the synthetic 'tvm' fixtures: the shapes the TVM test run met (EKG Adapters - TVM Test Result).

No directory, no github/ folder, no Jira: tickets are GitHub issues (scraper shape), commits are
{subject, body, files_changed}, the mbox has no List-Id and holds git@apache.org PR notices, a CI
notice, a message with no Message-ID and a duplicate; plus a Discourse forum, a code tree with Python
files, and docs (csv, xlsx, md). One bad commit row and one unreadable forum file are there on purpose.

    python -I ekg-eval-data/fixtures/make_tvm.py      (rewrites fixtures/tvm/)
"""

import json
import shutil
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent / "tvm"
REPO = "apache/tvm"


def w(rel, text):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def jl(rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    w("manifest.json", json.dumps({"project": "tvm", "tenant": "tvm-test", "repo": REPO,
                                   "fetched_at": "2026-10-30T00:00:00Z", "window": ["2026-09-01", "2026-10-30"]}, indent=1))

    # ---- issues (scraper shape)
    w("issues/issues.json", json.dumps([
        {"number": 101, "title": "[Bug] Relax pass crashes on dynamic shapes", "state": "CLOSED", "stateReason": "COMPLETED",
         "body": "Running the Relax pass on a model with dynamic shapes crashes. Seen with #99 too.",
         "author": "alice-dev", "labels": ["type: bug", "relax"], "assignees": ["bob-k"],
         "createdAt": "2026-09-03T10:00:00Z", "closedAt": "2026-09-20T12:00:00Z",
         "linkedPullRequests": [102],
         "comments": [{"author": "bob-k", "body": "I can reproduce it; fix in #102.", "createdAt": "2026-09-04T09:00:00Z"}]},
        {"number": 99, "title": "[Feature] Dynamic shape support in TIR", "state": "OPEN", "body": "Tracking issue.",
         "author": "carol-z", "labels": ["type: feature"], "createdAt": "2026-09-01T08:00:00Z", "comments": []},
        {"number": 103, "title": "Duplicate of #101", "state": "CLOSED", "stateReason": "DUPLICATE", "body": "same crash",
         "author": "dave-q", "createdAt": "2026-09-05T08:00:00Z", "closedAt": "2026-09-05T09:00:00Z"},
        {"title": "row without a number"},
    ], indent=1))

    # ---- git commits ({subject, body, files_changed}); one bad row
    w("git/commits.jsonl", jl([
        {"sha": "a" * 40, "subject": "[Relax] Fix crash on dynamic shapes (#102)",
         "body": "Fixes #101\n\nSee RFC-0042 for the shape design.", "author": "Bob Kumar <bob@example.org>",
         "date": "2026-09-20T11:00:00Z", "files_changed": ["python/tvm/relax/pass.py", "docs/README.md"]},
        {"sha": "b" * 40, "subject": "[TIR] Add dynamic shape helper", "body": "Part of #99",
         "author": {"name": "Carol Zhang", "email": "carol@example.org", "date": "2026-09-22T11:00:00Z"},
         "files_changed": [{"filename": "python/tvm/tir/shape.py", "status": "added"}]},
        {"subject": "row without a sha"},
    ]) + "not json\n")

    # ---- mail: no List-Id, any .mbox name
    msgs = [
        ("<m1@tvm>", None, "Bob Kumar <bob@example.org>", "2026-09-04T10:00:00Z", "[DISCUSS] RFC-0042 dynamic shapes",
         "I propose we adopt RFC-0042 for dynamic shapes in Relax. This also covers #101."),
        ("<m2@tvm>", "<m1@tvm>", "Carol Zhang <carol@example.org>", "2026-09-05T10:00:00Z", "Re: [DISCUSS] RFC-0042 dynamic shapes",
         "> I propose we adopt RFC-0042\n+1, and the TIR helper is in https://github.com/apache/tvm/pull/104"),
        ("<m2@tvm>", "<m1@tvm>", "Carol Zhang <carol@example.org>", "2026-09-05T10:00:00Z", "Re: [DISCUSS] RFC-0042 dynamic shapes",
         "> I propose we adopt RFC-0042\n+1, and the TIR helper is in https://github.com/apache/tvm/pull/104"),  # duplicate
        (None, None, "Alice Dev <alice@example.org>", "2026-09-06T10:00:00Z", "Question about Relax passes",
         "Is there a guide for writing a new Relax pass?"),  # no Message-ID
        ("<n1@gh>", None, "git@apache.org", "2026-09-20T12:00:00Z", "[PR] [Relax] Fix crash on dynamic shapes (#102)",
         "bob-k opened a new pull request #102:\nURL: https://github.com/apache/tvm/pull/102"),
        ("<n2@gh>", None, "git@apache.org", "2026-09-21T12:00:00Z", "[I] Something in another repo",
         "URL: https://github.com/apache/tvm-ffi/issues/7"),
        ("<n3@ci>", None, "builds@apache.org", "2026-09-21T13:00:00Z", "[CI] build failed: main #555", "build failed"),
    ]
    box = []
    for mid, reply, frm, at, subj, body in msgs:
        h = [f"From MAILER-DAEMON Mon Sep  1 00:00:00 2026", f"From: {frm}", f"Date: {at}", f"Subject: {subj}"]
        if mid:
            h.append(f"Message-ID: {mid}")
        if reply:
            h.append(f"In-Reply-To: {reply}")
        box.append("\n".join(h) + "\n\n" + body + "\n")
    w("mail/dev/2026-09.mbox", "\n".join(box))

    # ---- forum (Discourse /t/<id>.json); one unreadable file
    w("forum/categories.json", json.dumps({"category_list": {"categories": [{"id": 5, "name": "Development"}]}}))
    w("forum/topics/2001.json", json.dumps({"id": 2001, "title": "How do I write a Relax pass?", "category_id": 5,
        "created_at": "2026-09-07T10:00:00Z", "post_stream": {"posts": [
            {"id": 1, "post_number": 1, "username": "alice-dev", "name": "Alice Dev", "created_at": "2026-09-07T10:00:00Z",
             "cooked": "<p>Is there a guide for writing a new Relax pass? I hit #101 on the way.</p>"},
            {"id": 2, "post_number": 2, "username": "bob-k", "name": "Bob Kumar", "created_at": "2026-09-07T12:00:00Z",
             "reply_to_post_number": 1,
             "cooked": "<p>Start from python/tvm/relax/pass.py. See <a href=\"https://github.com/apache/tvm/pull/102\">the fix</a>.</p>"},
        ]}}))
    w("forum/topics/2002.json", json.dumps({"id": 2002, "title": "Empty topic", "post_stream": {"posts": []}}))
    w("forum/topics/broken.json", "{not json")

    # ---- code tree
    w("code/python/tvm/__init__.py", "from tvm import relax\n")
    w("code/python/tvm/relax/__init__.py", "from tvm.relax import pass_ as _p\n")
    w("code/python/tvm/relax/pass.py", "import tvm.tir.shape\n\n\nclass RelaxPass:\n    def run(self, mod):\n        return mod\n\n\ndef make_pass():\n    return RelaxPass()\n")
    w("code/python/tvm/tir/shape.py", "def dynamic(n):\n    return n\n")
    w("code/README.md", "# TVM\n\nAn end to end compiler stack. Bugs go to GitHub issues, e.g. #99.\n")

    # ---- docs: csv, md, xlsx
    w("docs/release-plan.csv", "item,issue,owner\nDynamic shapes,#99,carol\nRelax crash,#101,bob\n")
    w("docs/contributing.md", "# Contributing\n\nEvery PR must reference an issue (e.g. Fixes #101). Run the linter before review.\n")
    xlsx = OUT / "docs" / "roadmap.xlsx"
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   '<sheet name="Q4" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<si><t>Feature</t></si><si><t>Issue</t></si><si><t>Dynamic shapes</t></si><si><t>#99</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                   '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row></sheetData></worksheet>')
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
