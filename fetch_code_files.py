"""Fetch the trunk files a blobless clone lacks (docs, build and config files, and the source files the evaluation
questions name) from GitHub's raw host at the snapshot commit. A file is kept only when its bytes hash to the git
blob sha the tree listing gives, so what lands in code/files/ is exactly the snapshot's content.

    python -I ekg-eval-data/fetch_code_files.py ekg-eval-raw/apache-kafka/code/files/_wanted.json         ekg-eval-raw/apache-kafka/code/files <trunk commit sha>

code.py reads code/files/<path> as each File's text.
"""
import concurrent.futures as cf
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

listing, out, commit = json.load(open(sys.argv[1])), Path(sys.argv[2]), sys.argv[3]
ok, bad, failed, skipped = [], [], [], []


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def one(r):
    p = r["path"]
    if p.endswith((".png", ".jpg", ".gif", ".svg", ".ico", ".jar")):
        return "skipped", p
    url = f"https://raw.githubusercontent.com/apache/kafka/{commit}/{urllib.parse.quote(p)}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ekg-eval"}), timeout=60) as resp:
                data = resp.read()
            break
        except Exception as e:  # network hiccup: retry, then give up on this file
            if attempt == 3:
                return "failed", f"{p}: {type(e).__name__}"
            time.sleep(2 * (attempt + 1))
    if blob_sha(data) != r["sha"]:
        return "bad", p
    dest = out / p
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return "ok", p


with cf.ThreadPoolExecutor(6) as ex:
    for kind, p in ex.map(one, listing):
        {"ok": ok, "bad": bad, "failed": failed, "skipped": skipped}[kind].append(p)
(out / "_fetched.json").write_text(json.dumps({"commit": commit, "source": "raw.githubusercontent.com",
                                               "verified_by": "git blob sha from code/tree.jsonl",
                                               "files": sorted(ok)}, indent=1))
print(f"ok {len(ok)}, sha mismatch {len(bad)}, failed {len(failed)}, skipped (binary) {len(skipped)}")
print("mismatch:", bad[:10]); print("failed:", failed[:10])
