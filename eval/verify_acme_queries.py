"""Check a question set's gold answers against the source documents they were derived from.

Every checklist fact in eval/acme_queries.jsonl carries the sentences it rests on: {"doc": <file name>, "quote":
<text as the document has it>}. This reads the documents' text and checks that each quote is really there
(spacing and line breaks ignored), and, when the questions' own PDF is given, that every question is worded as
it is there. It reads files only; no graph and no model.

    python -I ekg-eval-data/eval/verify_acme_queries.py
    python -I ekg-eval-data/eval/verify_acme_queries.py --questions-pdf <folder>/ACME_Associative_Intelligence_Test_Queries.pdf
    python -I ekg-eval-data/eval/verify_acme_queries.py --set <file.jsonl> --docs <folder of the source files>

Exit code 0 when every quote is found, 1 otherwise. Needs pdfplumber or pypdf for PDFs.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pdf_text(path: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def text_of(path: Path) -> str:
    return pdf_text(path) if path.suffix.lower() == ".pdf" else path.read_text(encoding="utf-8", errors="replace")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--set", default=str(HERE / "acme_queries.jsonl"))
    ap.add_argument("--docs", default=str(HERE.parent.parent / "ekg-eval-raw" / "acme-docs" / "docs"))
    ap.add_argument("--questions-pdf", default=None, help="the PDF the questions come from: check each question's wording")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rows = [json.loads(line) for line in Path(a.set).read_text(encoding="utf-8").splitlines() if line.strip()]
    texts: dict = {}
    asked = norm(text_of(Path(a.questions_pdf))) if a.questions_pdf else None
    bad = facts = quotes = 0
    for r in rows:
        if asked is not None and norm(r["question"]) not in asked:
            bad += 1
            print(f"{r['id']}: the question is not worded as in {Path(a.questions_pdf).name}")
        for c in r.get("answer_checklist") or []:
            facts += 1
            if not c.get("evidence"):
                bad += 1
                print(f"{r['id']} {c['id']}: no evidence given")
            for e in c.get("evidence") or []:
                quotes += 1
                if e["doc"] not in texts:
                    p = Path(a.docs) / e["doc"]
                    texts[e["doc"]] = norm(text_of(p)) if p.is_file() else None
                if texts[e["doc"]] is None:
                    bad += 1
                    print(f"{r['id']} {c['id']}: no such document {e['doc']}")
                elif norm(e["quote"]) not in texts[e["doc"]]:
                    bad += 1
                    print(f"{r['id']} {c['id']}: not found in {e['doc']}: {e['quote'][:90]!r}")
    required = sum(1 for r in rows for c in r.get("answer_checklist") or [] if c.get("required", True))
    print(f"{len(rows)} questions, {facts} checklist facts ({required} required), {quotes} quotes checked in "
          f"{len(texts)} documents: {'all found' if not bad else f'{bad} problems'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
