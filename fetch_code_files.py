"""Fetch the snapshot's files a blobless clone lacks from the code host's raw endpoint at the snapshot commit. A file
is kept only when its bytes hash to the git blob sha the tree listing gives, so what lands in code/files/ is exactly
the snapshot's content.

    python -I ekg-eval-data/fetch_code_files.py <wanted> <data>/code/files <commit sha> [owner/repo] [--count]

<wanted> is either a JSON list of {path, sha} (docs, build files, the files the evaluation questions name), or the
tree listing itself (<data>/code/tree.jsonl): then every source file a grammar can parse into symbols
(ekg_adapters/symbols.py, any language) that code/files/ does not hold yet is fetched. --count only says how many
that is. The repository is the 4th argument, else "repo" in <data>/manifest.json.

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

args = [a for a in sys.argv[1:] if not a.startswith("--")]
wanted, out, commit = Path(args[0]), Path(args[1]), args[2]
manifest = out.parent.parent / "manifest.json"
repo = args[3] if len(args) > 3 else json.loads(manifest.read_text(encoding="utf-8")).get("repo") if manifest.exists() else None
if not repo:
    sys.exit("no repository: give owner/repo as the 4th argument, or put \"repo\" in the dataset's manifest.json")
if wanted.suffix == ".jsonl":  # the whole listing: the source files with no text yet
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ekg_adapters import symbols
    rows = [json.loads(line) for line in wanted.read_text(encoding="utf-8").splitlines() if line.strip()]
    listing = [r for r in rows if r.get("sha") and symbols.has_symbols(r["path"]) and not (out / r["path"]).is_file()]
else:
    listing = json.load(open(wanted, encoding="utf-8"))
if "--count" in sys.argv:
    print(f"{len(listing)} files to fetch from {repo} at {commit}")
    sys.exit(0)
ok, bad, failed, skipped = [], [], [], []


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def one(r):
    p = r["path"]
    if p.endswith((".png", ".jpg", ".gif", ".svg", ".ico", ".jar")):
        return "skipped", p
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/{urllib.parse.quote(p)}"
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
log = out / "_fetched.json"
before = json.loads(log.read_text(encoding="utf-8")).get("files", []) if log.exists() else []
log.write_text(json.dumps({"commit": commit, "source": f"raw.githubusercontent.com/{repo}",
                           "verified_by": "git blob sha from code/tree.jsonl",
                           "files": sorted(set(before) | set(ok))}, indent=1), encoding="utf-8")
print(f"ok {len(ok)}, sha mismatch {len(bad)}, failed {len(failed)}, skipped (binary) {len(skipped)}")
print("mismatch:", bad[:10]); print("failed:", failed[:10])
