"""Loading evaluation question sets and grading answers (used by run_eval.py and demo_app.py).

Two question formats:

  * this kit's own (make_eval_set.py): {"graph", "as_of", "questions": [{id, category, question, asker,
    gold: {must: [[any of], ...], none: bool}}]}
  * the shared Kafka set (kafka_shared_queries.jsonl, one question per line): id, pillar, category,
    difficulty, question, gold_answer, acceptable_variants, answer_checklist [{id, fact, required}],
    must_not_say, grading ("script" | "judge"), expected_behavior ("answer" | "abstain"), as_of

How an answer is graded:
  1. must_not_say: an answer containing any of these phrases fails, whatever else it says
  2. grading "script" with acceptable_variants: passes when one variant appears in the answer
     (case and spacing ignored; a number must appear as a whole number)
  3. otherwise (grading "judge", an abstain question, or a script question with no variants): an LLM
     judge reads the gold answer and the checklist and says which required facts the answer states;
     it passes when all of them are there (for an abstain question: when it says the data cannot
     answer and invents nothing)
  4. this kit's own format: every `must` group matched, or for `none` a clear "nothing in the graph"
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

NONE_RX = re.compile(r"\b(no|not|none|nothing|cannot find|could not find|isn't|is not|doesn't|does not|unknown)\b", re.I)
PR_RX = re.compile(r"(?:PR\s*|pull request\s*|#)(\d{3,6})", re.I)


def load(path) -> tuple[list[dict], dict]:
    """Questions in one shape: {id, category, pillar, difficulty, question, as_of, asker, gold_answer,
    variants, must_not_say, checklist, grading, behavior, must, none}. Returns (questions, set metadata)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows, meta = [json.loads(l) for l in text.splitlines() if l.strip()], {}
    else:
        doc = json.loads(text)
        rows, meta = (doc["questions"], {k: v for k, v in doc.items() if k != "questions"}) if isinstance(doc, dict) else (doc, {})
    out = []
    for r in rows:
        g = r.get("gold") or {}
        out.append({
            "id": r.get("id"), "category": r.get("category"), "pillar": r.get("pillar"), "difficulty": r.get("difficulty"),
            "question": r["question"], "as_of": r.get("as_of") or meta.get("as_of"), "asker": r.get("asker") or "public",
            "gold_answer": r.get("gold_answer"), "variants": r.get("acceptable_variants") or [],
            "must_not_say": r.get("must_not_say") or [], "checklist": r.get("answer_checklist") or [],
            "grading": r.get("grading") or ("rules" if g else "judge"),
            "behavior": r.get("expected_behavior") or ("abstain" if g.get("none") else "answer"),
            "must": g.get("must") or [], "none": bool(g.get("none")), "answer_type": r.get("answer_type"),
            "data_tier": r.get("data_tier"),
        })
    return out, meta


def as_of_time(s: str | None) -> str | None:
    """'2026-10-29' -> the end of that day (the question is about the state on that date)."""
    if not s:
        return None
    s = s.strip()
    if len(s) == 10:
        return f"{s}T23:59:59Z"
    return s if s.endswith("Z") else s + "Z"


def _norm(s: str) -> str:
    """Case, spacing, markdown marks and accents ignored ("Harmadás" matches "Harmadas")."""
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[*`_\"'“”‘’]", "", s)).strip().lower()


def _has(answer: str, phrase: str) -> bool:
    a, p = _norm(answer), _norm(phrase)
    if not p:
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", p):
        return re.search(rf"(?<![\d.]){re.escape(p)}(?![\d])", a) is not None
    return p in a


JUDGE_SCHEMA = {"name": "ekg_eval_judge", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["checklist", "abstained", "contradicts_gold", "reason"],
    "properties": {
        "checklist": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "required": ["id", "met"],
                                                 "properties": {"id": {"type": "string"}, "met": {"type": "boolean"}}}},
        "abstained": {"type": "boolean"},
        "contradicts_gold": {"type": "boolean"},
        "reason": {"type": "string"}}}}


def judge_prompt(q: dict, answer: str) -> str:
    facts = q["checklist"] or [{"id": "c1", "fact": q.get("gold_answer") or "", "required": True}]
    lines = [f"- {c['id']} ({'required' if c.get('required', True) else 'optional'}): {c['fact']}" for c in facts]
    return "\n".join([
        "You grade an answer to a question about a software project's engineering data. Judge only whether the",
        "ANSWER states each fact; wording may differ, and extra correct detail is fine. A fact stated wrongly is not met.",
        "Differences in accents, case or formatting of a name (Harmadás / Harmadas, mjsax / MJSAX) are not errors.",
        "", f"QUESTION: {q['question']}", "", f"GOLD ANSWER: {q.get('gold_answer') or ''}", "", "FACTS:", *lines,
        "", f"EXPECTED BEHAVIOUR: {'abstain: say the data cannot answer this, and why' if q['behavior'] == 'abstain' else 'answer'}",
        "", "<<<ANSWER", answer or "(empty)", "ANSWER>>>", "",
        "Reply as JSON: checklist = [{id, met}] for every fact above; abstained = whether the answer declines to give "
        "an answer because the data does not hold it; contradicts_gold = whether it asserts something the gold answer "
        "contradicts; reason = one sentence."])


async def grade(q: dict, answer: str, judge=None) -> dict:
    """{pass, method, missing, said_forbidden, judge?}. `judge` is a client with guided_json (needed for judge grading)."""
    answer = answer or ""
    bad = [p for p in q["must_not_say"] if _has(answer, p)]
    out = {"said_forbidden": bad}
    if q["grading"] == "rules":  # this kit's own format
        if q["none"]:
            ok = bool(NONE_RX.search(answer)) and not PR_RX.search(answer)
            return {**out, "pass": ok and not bad, "method": "rules", "missing": [] if ok else ["says the graph has nothing"]}
        missing = [g for g in q["must"] if not any(_has(answer, s) for s in g if s)]
        return {**out, "pass": not missing and bool(answer.strip()) and not bad, "method": "rules", "missing": missing}
    if q["grading"] == "script" and q["variants"] and q["behavior"] != "abstain":
        hit = [v for v in q["variants"] if _has(answer, v)]
        return {**out, "pass": bool(hit) and not bad, "method": "script", "matched": hit,
                "missing": [] if hit else [f"one of: {q['variants']}"]}
    if judge is None:
        return {**out, "pass": None, "method": "judge (not run)", "missing": []}
    try:
        v = await judge.guided_json([{"role": "user", "content": judge_prompt(q, answer)}], JUDGE_SCHEMA, max_tokens=700)
    except Exception as e:  # a judge failure is reported, never counted as a pass
        return {**out, "pass": None, "method": "judge (error)", "missing": [], "judge_error": f"{type(e).__name__}: {e}"[:200]}
    met = {c.get("id"): bool(c.get("met")) for c in v.get("checklist") or []}
    facts = q["checklist"] or [{"id": "c1", "required": True}]
    missing = [c.get("fact") or c["id"] for c in facts if c.get("required", True) and not met.get(c["id"])]
    if q["behavior"] == "abstain":
        ok = bool(v.get("abstained")) and not v.get("contradicts_gold") and (not missing or bool(v.get("abstained")))
    else:
        ok = not missing and not v.get("contradicts_gold") and not v.get("abstained")
    return {**out, "pass": ok and not bad, "method": "judge", "missing": missing,
            "judge": {"checklist": met, "abstained": v.get("abstained"), "contradicts_gold": v.get("contradicts_gold"),
                      "reason": v.get("reason")}}
