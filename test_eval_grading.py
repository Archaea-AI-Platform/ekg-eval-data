"""Grading rules for evaluation answers (eval_grading.py), with a stand-in judge: no LLM, no network.

    python -I ekg-eval-data/test_eval_grading.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_grading import as_of_time, grade  # noqa: E402


def q(**kw):
    base = {"id": "T", "question": "?", "gold_answer": "gold", "variants": [], "must_not_say": [], "checklist": [],
            "grading": "script", "behavior": "answer", "must": [], "none": False}
    return {**base, **kw}


class Judge:
    def __init__(self, reply):
        self.reply = reply

    async def guided_json(self, messages, schema, max_tokens=700):
        self.prompt = messages[0]["content"]
        return self.reply


def main() -> int:
    checks = []
    run = lambda qq, a, j=None: asyncio.run(grade(qq, a, j))
    checks.append(("a variant anywhere passes, case and markup ignored",
                   run(q(variants=["Luke Chen"]), "It was **luke chen** on 2026-09-23.")["pass"] is True))
    checks.append(("a number must appear whole", run(q(variants=["123"]), "1234 messages")["pass"] is False
                   and run(q(variants=["123"]), "There were 123 messages.")["pass"] is True))
    checks.append(("must_not_say fails an otherwise right answer",
                   run(q(variants=["yes"], must_not_say=["no colon follows"]), "Yes, but no colon follows the key")["pass"] is False))
    facts = [{"id": "c1", "fact": "A", "required": True}, {"id": "c2", "fact": "B", "required": False}]
    j_ok = Judge({"checklist": [{"id": "c1", "met": True}, {"id": "c2", "met": False}], "abstained": False,
                  "contradicts_gold": False, "reason": "states A"})
    checks.append(("judge: every required fact met passes (optional ones do not matter)",
                   run(q(grading="judge", checklist=facts), "A.", j_ok)["pass"] is True and "FACTS:" in j_ok.prompt))
    j_miss = Judge({"checklist": [{"id": "c1", "met": False}], "abstained": False, "contradicts_gold": False, "reason": "no A"})
    r = run(q(grading="judge", checklist=facts), "B.", j_miss)
    checks.append(("judge: a missing required fact fails and is named", r["pass"] is False and r["missing"] == ["A"]))
    j_abs = Judge({"checklist": [{"id": "c1", "met": True}], "abstained": True, "contradicts_gold": False, "reason": "declines"})
    checks.append(("abstain question: declining passes", run(q(grading="judge", behavior="abstain", checklist=facts),
                                                             "The data cannot answer this.", j_abs)["pass"] is True))
    checks.append(("abstain question: a confident answer fails",
                   run(q(grading="judge", behavior="abstain", checklist=facts), "Alice owns it.",
                       Judge({"checklist": [{"id": "c1", "met": False}], "abstained": False, "contradicts_gold": True,
                              "reason": "invents"}))["pass"] is False))
    checks.append(("no judge client: not graded, never a pass", run(q(grading="judge"), "x")["pass"] is None))
    checks.append(("a script question with no variants goes to the judge", run(q(), "x", j_ok)["method"] == "judge"))
    checks.append(("this kit's own format: every must group", run(q(grading="rules", must=[["#42", "42"], ["bob"]]), "PR #42 by Bob")["pass"] is True
                   and run(q(grading="rules", must=[["#42"], ["bob"]]), "PR #42")["pass"] is False))
    checks.append(("as_of: a day means the end of that day", as_of_time("2026-10-29") == "2026-10-29T23:59:59Z"))
    checks.append(("PR 20011, PR-20011 and #20011 are the same reference",
                   run(q(variants=["PR 20011"]), "Fixed in #20011 since 2025-08-19")["pass"] is True
                   and run(q(variants=["#20011"]), "pull request 20011")["pass"] is True))
    checks.append(("a list passes with every item in any order",
                   run(q(variants=["IsolationLevel, TopicPartition, Uuid"]), "Uuid, TopicPartition and IsolationLevel")["pass"] is True
                   and run(q(variants=["IsolationLevel, TopicPartition, Uuid"]), "Uuid and TopicPartition")["pass"] is False))
    fb = run(q(variants=["exact phrase"], checklist=facts), "A.", j_ok)
    checks.append(("a script miss goes to the judge, labelled apart", fb["pass"] is True and fb["method"] == "script->judge"))
    checks.append(("...but never past must_not_say", run(q(variants=["x y z"], checklist=facts, must_not_say=["A."]), "A.", j_ok)["pass"] is False))
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
