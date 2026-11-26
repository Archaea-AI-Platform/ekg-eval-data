"""Write a graph's vector sidecar (one vector per text record) for meaning-based search beside keyword search.

    python -I build_vectors.py --graph <graph.sqlite> --out <new folder>/<name>.npz

Needs EKG_EMBED_URL (the embedding service, called by plain HTTP) and RUNPOD_API_KEY. Re-runnable: a record already
in the output file with the same text is not embedded again. Batches of at most 32 texts, at most 2 in flight.
Then run the eval with EKG_VECTORS=<that file>.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "query-master"))

import numpy as np  # noqa: E402

from app.ekg.query import Graph  # noqa: E402
from app.ekg.query.vectors import DOC_PREFIX, embed, records_to_embed  # noqa: E402

BATCH = 32
IN_FLIGHT = 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    graph_dir = Path(a.graph).resolve().parent
    if out.resolve().parent == graph_dir and "ekg-eval-out" in str(graph_dir):
        sys.exit("write the sidecar to a new folder, not into an existing graph folder")
    g = Graph.from_sqlite(a.graph)
    todo = records_to_embed(g)
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()[:16]
    old = {}
    if out.is_file():
        z = np.load(out, allow_pickle=False)
        old = {(i, h): v for i, h, v in zip(z["ids"], z["shas"], z["vectors"])}
    keep = {rid: old[(rid, sha(t))] for rid, t in todo if (rid, sha(t)) in old}
    need = [(rid, t) for rid, t in todo if rid not in keep]
    print(f"{len(todo)} texts; {len(keep)} kept, {len(need)} to embed", flush=True)
    batches = [need[k:k + BATCH] for k in range(0, len(need), BATCH)]
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(IN_FLIGHT) as ex:
        for k, (b, vecs) in enumerate(zip(batches, ex.map(lambda b: embed([t for _, t in b], DOC_PREFIX), batches))):
            for (rid, _), v in zip(b, vecs):
                keep[rid] = v
            print(f"batch {k + 1}/{len(batches)} ({time.perf_counter() - t0:.1f} s)", flush=True)
    ids = [rid for rid, _ in todo]
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out, ids=np.array(ids), shas=np.array([sha(t) for _, t in todo]),
             vectors=np.stack([keep[i] for i in ids]).astype("float32"))
    print(f"wrote {out}: {len(ids)} vectors", flush=True)


if __name__ == "__main__":
    main()
