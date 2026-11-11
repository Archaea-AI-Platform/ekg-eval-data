"""Build an evaluation question set, with gold answers, from a built graph.

Gold answers are worked out here from the stored records and links directly (plain
loops over the graph, not the query tools), so the tools and the agent are checked
against an independent reading of the data. Each question names the records its
answer rests on and the strings a correct answer must contain:

  gold.must  [[a, b], [c]]   every group must be matched by at least one of its strings
  gold.none  true            the right answer is that the graph has nothing (no PR, no ticket)

    python -I ekg-eval-data/make_eval_set.py                          # Kafka, 3 per category
    python -I ekg-eval-data/make_eval_set.py --graph ekg-eval-out/x/graph.sqlite --per 5 --out eval/x.json

Run it with query-master's venv python (it imports app.ekg.query to load the graph).
"""
import argparse
import collections
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QM = HERE.parent / "query-master"
sys.path.insert(0, str(QM))

from app.ekg.query.graph import Graph  # noqa: E402


def build(g: Graph, per: int, seed: int):
    rnd = random.Random(seed)
    R, L = g.rec, g.links
    out_l = collections.defaultdict(list)
    in_l = collections.defaultdict(list)
    for lk in L:
        out_l[lk["a"]].append(lk)
        in_l[lk["b"]].append(lk)
    real = lambda rid: len([k for k in R[rid]["props"] if not k.startswith("_")]) > 1   # not a stub
    p = lambda rid: R[rid]["props"]
    name = lambda rid: p(rid).get("display_name") or p(rid).get("login") or p(rid).get("address") or p(rid).get("full_name")
    login = lambda rid: p(rid).get("login") or p(rid).get("address")
    questions = []

    def add(cat, q, must=None, none=False, evidence=()):
        questions.append({"category": cat, "question": q, "gold": {"must": must or [], "none": none},
                          "evidence": list(evidence)})

    def pick(items, n=per):
        items = sorted(items, key=lambda x: str(x))
        rnd.shuffle(items)
        return items[:n]

    tickets = [t for t in g.by_type.get("Ticket", []) if real(t)]
    prs = [x for x in g.by_type.get("PullRequest", []) if real(x)]
    ident = lambda t: p(t).get("identifier")

    # 1. ticket -> the PR that fixed it, and its approvers
    rows = []
    for t in tickets:
        cands = {lk["a"] for lk in in_l[t] if lk["type"] == "IMPLEMENTS" and R[lk["a"]]["type"] == "PullRequest"} | \
                {lk["b"] for lk in out_l[t] if lk["type"] == "LINKS_TO" and R[lk["b"]]["type"] == "PullRequest"}
        cands = {c for c in cands if real(c) and p(c).get("state") == "merged"}
        if len(cands) == 1:
            pr = cands.pop()
            appr = [lk["b"] for lk in out_l[pr] if lk["type"] == "APPROVED_BY"]
            if appr:
                rows.append((t, pr, appr))
    for t, pr, appr in pick(rows):
        add("ticket_to_fix", f"Which pull request fixed {ident(t)}, and who approved it?",
            [[f"#{p(pr)['number']}", str(p(pr)["number"])], [x for a in appr for x in (name(a), login(a)) if x]],
            evidence=[t, pr] + appr)

    # 2. PR -> the ticket it implements
    rows = []
    for pr in prs:
        ts = {lk["b"] for lk in out_l[pr] if lk["type"] == "IMPLEMENTS" and R[lk["b"]]["type"] == "Ticket" and real(lk["b"])}
        if len(ts) == 1:
            rows.append((pr, ts.pop()))
    for pr, t in pick(rows):
        add("pr_to_ticket", f"Which Jira ticket does pull request #{p(pr)['number']} implement, and what is that ticket about?",
            [[ident(t)]], evidence=[pr, t])

    # 3. commit -> its PR and author
    rows = []
    for c in g.by_type.get("Commit", []):
        prs_c = [lk["a"] for lk in in_l[c] if lk["type"] == "MERGED_AS"]
        if len(prs_c) == 1 and real(prs_c[0]):
            auth = [lk["b"] for lk in out_l[prs_c[0]] if lk["type"] == "AUTHORED_BY"]
            if auth:
                rows.append((c, prs_c[0], auth[0]))
    for c, pr, a in pick(rows):
        add("commit_to_pr", f"Which pull request does commit {p(c)['sha'][:10]} come from, and who opened that pull request?",
            [[f"#{p(pr)['number']}", str(p(pr)["number"])], [x for x in (name(a), login(a)) if x]], evidence=[c, pr, a])

    # 4. current assignee and state of a ticket
    rows = []
    for t in tickets:
        st = [lk for lk in out_l[t] if lk["type"] == "IN_STATE" and not lk.get("valid_to")]
        asg = [lk for lk in out_l[t] if lk["type"] == "ASSIGNED_TO" and not lk.get("valid_to")]
        if len(st) == 1 and len(asg) == 1:
            rows.append((t, st[0]["b"], asg[0]["b"]))
    for t, s, a in pick(rows):
        add("ticket_status", f"What is the current status of {ident(t)}, and who is it assigned to?",
            [[p(s)["name"]], [x for x in (name(a), login(a)) if x]], evidence=[t, s, a])

    # 5. state at a past date
    rows = []
    for t in tickets:
        hist = sorted((lk for lk in out_l[t] if lk["type"] == "IN_STATE" and lk.get("valid_from") and lk.get("valid_to")),
                      key=lambda lk: lk["valid_from"])
        hist = [lk for lk in hist if lk["valid_to"][:10] > lk["valid_from"][:10]]   # held for a whole day at least
        later = [lk for lk in out_l[t] if lk["type"] == "IN_STATE" and lk.get("valid_from", "") >= (hist[0]["valid_to"] if hist else "~")]
        if hist and later and p(hist[0]["b"])["name"] != p(later[0]["b"])["name"]:
            rows.append((t, hist[0]))
    for t, lk in pick(rows):
        day = lk["valid_from"][:10]
        add("state_as_of", f"What state was {ident(t)} in at the end of {day} (UTC)?", [[p(lk['b'])['name']]], evidence=[t, lk["b"]])

    # 6. KIP status and where it was decided
    kips = [d for d in g.by_type.get("Decision", []) if p(d).get("identifier") and p(d).get("status")]
    rows = []
    for d in kips:
        threads = set()
        for lk in out_l[d]:
            if lk["type"] == "MADE_IN" and R[lk["b"]]["type"] == "EmailMessage":
                threads |= {l2["b"] for l2 in out_l[lk["b"]] if l2["type"] == "IN" and R[l2["b"]]["type"] == "EmailThread"}
        if threads:
            rows.append((d, threads))
    for d, threads in pick(rows):
        subj = [p(th)["normalized_subject"] for th in threads]
        add("kip_status", f"What is the status of {p(d)['identifier']}, and on which mailing-list thread was it discussed or voted?",
            [[p(d)["status"]], [p(d)["identifier"]]], evidence=[d] + list(threads))
        questions[-1]["gold"]["threads"] = subj

    # 7. who wrote the most commits under a directory
    dirs = collections.defaultdict(collections.Counter)
    for c in g.by_type.get("Commit", []):
        authors = [lk["b"] for lk in out_l[c] if lk["type"] == "AUTHORED_BY" and lk["props"].get("role") in (None, "author")]
        files = [p(lk["b"]).get("path", "") for lk in out_l[c] if lk["type"] == "CHANGES"]
        for d in {"/".join(f.split("/")[:4]) for f in files if f.count("/") >= 4}:
            for a in authors:
                dirs[d][a] += 1
    rows = []
    for d, cnt in dirs.items():
        top = cnt.most_common(2)
        if top[0][1] >= 4 and (len(top) == 1 or top[0][1] > top[1][1]):
            rows.append((d, top[0][0], top[0][1]))
    for d, a, n in pick(rows):
        add("top_contributor", f"Who wrote the most commits that changed files under {d}/ in this data, and roughly how many?",
            [[x for x in (name(a), login(a)) if x]], evidence=[a])
        questions[-1]["gold"]["count"] = n

    # 8. counts the agent must compute
    fixed_oct = sum(1 for t in tickets if p(t).get("resolution") == "Fixed" and str(p(t).get("resolved_at", "")).startswith("2026-10"))
    merged_oct = sum(1 for x in prs if p(x).get("state") == "merged" and str(p(x).get("merged_at", "")).startswith("2026-10"))
    add("count", "How many Jira tickets were resolved as Fixed in October 2026?", [[str(fixed_oct)]])
    add("count", "How many pull requests were merged in October 2026?", [[str(merged_oct)]])

    # 9. people: a person's accounts and addresses
    rows = []
    for pid in g.by_type.get("Person", []):
        accts = [lk["b"] for lk in out_l[pid] if lk["type"] == "HAS_ACCOUNT"]
        logins = sorted({login(a) for a in accts if login(a)})
        if len(logins) >= 2:
            rows.append((pid, logins))
    for pid, logins in pick(rows):
        add("person_accounts", f"Which tool accounts does {p(pid)['full_name']} have?", [[l] for l in logins[:2]], evidence=[pid])

    # 10. decisions and knowledge the LLM stage extracted about a ticket
    rows = []
    for t in tickets:
        kn = [lk["a"] for lk in in_l[t] if lk["type"] == "ABOUT" and R[lk["a"]]["type"] in ("Decision", "Guideline", "Requirement")]
        if kn:
            rows.append((t, kn))
    for t, kn in pick(rows):
        add("knowledge_about", f"What was decided, or which rules or requirements were stated, about {ident(t)}?",
            [[ident(t)]], evidence=[t] + kn)
        questions[-1]["gold"]["statements"] = [p(k).get("statement") for k in kn][:3]

    # 11. negatives: the right answer is "nothing in the graph"
    rows = [t for t in tickets if not any(lk["type"] in ("IMPLEMENTS", "LINKS_TO", "MERGED_AS") for lk in in_l[t] + out_l[t]
                                          if R[lk["a"]]["type"] in ("PullRequest", "Commit") or R[lk["b"]]["type"] in ("PullRequest", "Commit"))]
    for t in pick(rows, max(1, per // 2)):
        add("none_expected", f"Which pull request fixed {ident(t)}?", none=True, evidence=[t])

    for i, q in enumerate(questions, 1):
        q["id"] = f"q{i:03d}"
    return questions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", default=str(HERE.parent / "ekg-eval-out" / "apache-kafka" / "graph.sqlite"))
    ap.add_argument("--per", type=int, default=3, help="questions per category")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--asker", default="public", help="who asks (public, or a person's name, email or login)")
    ap.add_argument("--out", default=str(HERE / "eval" / "kafka_questions.json"))
    args = ap.parse_args()
    g = Graph.from_sqlite(args.graph)
    qs = build(g, args.per, args.seed)
    for q in qs:
        q["asker"] = args.asker
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"graph": args.graph, "as_of": g.newest, "questions": qs}, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(f"{len(qs)} questions -> {out}  {dict(collections.Counter(q['category'] for q in qs))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
