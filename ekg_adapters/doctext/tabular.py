"""CSV and TSV -> DocText: one table. The delimiter is sniffed, the encoding tried in order."""

from __future__ import annotations

import csv
import io

from .model import Block, DocText, nonws
from .text import decode


def read_csv(data, title=None, delimiter=None, fmt="csv") -> DocText:
    doc = DocText(title=title, format=fmt)
    q = doc.quality
    text = decode(data, q)
    if delimiter is None:
        sample = text[:65536]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = "\t" if fmt == "tsv" else ","
            if sample.strip() and delimiter not in sample:
                q.dropped["no delimiter found: read as one column"] += 1
    big = csv.field_size_limit()
    csv.field_size_limit(2**31 - 1)   # a long cell is a long cell, not an error
    try:
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)]
    except csv.Error as e:   # an unbalanced quote: fall back to a plain split, and say so
        q.warn(f"not well-formed {fmt} ({e}): split on the delimiter without quote handling")
        rows = [[c.strip() for c in ln.split(delimiter)] for ln in text.splitlines()]
    finally:
        csv.field_size_limit(big)
    rows = [r for r in rows if any(r)]
    if rows:
        width = max(len(r) for r in rows)
        if any(len(r) != width for r in rows):
            q.dropped["rows shorter than the widest row (padded)"] += sum(1 for r in rows if len(r) != width)
        rows = [r + [""] * (width - len(r)) for r in rows]
        q.source_chars = sum(nonws(c) for r in rows for c in r)
        doc.blocks.append(Block("table", rows=rows, header=len(rows) > 1))
    return doc.finish()
