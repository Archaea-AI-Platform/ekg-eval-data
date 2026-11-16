# Question sets and how to run them

| File | What it is |
|---|---|
| `kafka_shared_queries.jsonl` | The 119 Kafka questions (the same rows as `kyro-knowledge-eval/eval/sets/2_trimmed.jsonl`). |
| `acme_queries.jsonl` | The 8 ACME questions from `ACME_Associative_Intelligence_Test_Queries.pdf`, with gold answers derived from the eight ACME source PDFs. |
| `verify_acme_queries.py` | Checks every quote an ACME gold fact rests on against the source PDFs, and the questions' wording against the questions PDF. |
| `kafka_questions.json` | This kit's own generated set (`make_eval_set.py`). |
| `results/<run>/` | One folder per run: `results.jsonl`, `summary.json`, `run.json`. |

Both `.jsonl` sets have one question per line in the shape `eval_grading.py` reads: `id`, `pillar`, `category`,
`difficulty`, `question`, `gold_answer`, `acceptable_variants`, `answer_checklist` (`[{id, fact, required}]`),
`must_not_say`, `grading` (`script` | `judge`), `expected_behavior`, `as_of`, `asker`.

## Running a set against a graph

The model settings (Muse Glimmer on OpenRouter, for the agent and the judge) are in `../../glimmer.env`, outside
every repository; `../../with_env.sh` (or `with_env.ps1`) loads it and runs a command. From the `ekg` folder:

```bash
PY=.venv/Scripts/python.exe

# the ACME set, against the graph built from ekg-eval-raw/acme-docs
./with_env.sh $PY -I ekg-eval-data/run_eval.py --questions ekg-eval-data/eval/acme_queries.jsonl \
    --graph ekg-eval-out/acme-docs/graph.sqlite --out ekg-eval-data/eval/results/acme_glimmer

# the Kafka 119, against any graph.sqlite
./with_env.sh $PY -I ekg-eval-data/run_eval.py --questions ekg-eval-data/eval/kafka_shared_queries.jsonl \
    --graph <path to graph.sqlite> --out ekg-eval-data/eval/results/kafka_glimmer_<name>
```

* `--concurrency` (8) questions at a time; `--tenant` reads one tenant of a combined graph; `--asker` asks every
  question as one person (default: each question's own asker, else the public).
* A run can be stopped and started again with the same command: finished questions are kept in
  `<out>/results.jsonl` as they finish and are not asked twice. A question that timed out, failed or was not
  graded is asked again.
* A folder holds one run. Another graph, model or question file into the same `--out` is refused; give another
  `--out`, or `--fresh` to start that folder over (it deletes what the folder holds).

## The ACME set

`ACME_Associative_Intelligence_Test_Queries.pdf` states the eight questions and **no expected answers**. The gold
answers and checklists here were written from the eight source PDFs (six product-requirement documents, the
GitHub activity export and the LDAP directory export). Each checklist fact carries its evidence: the document
and the sentence as the document has it. `verify_acme_queries.py` re-checks all of them:

```bash
$PY -I ekg-eval-data/eval/verify_acme_queries.py --questions-pdf <kyro-knowledge-eval>/ACME_Associative_Intelligence_Test_Queries.pdf
# 8 questions, 49 checklist facts (29 required), 89 quotes checked in 8 documents: all found
```

What is a judgement, not a quotation: which facts are `required`. A question such as "which teams are affected"
is open-ended; the required facts are the teams whose documents state a rule for exactly that case, and the
further teams (Analytics, Shipment & Operations in ACME-03) are optional facts. The questions carry no `as_of`:
they are about the state the exports describe (directory as of 2026-10-28, code activity through 2026-10-27,
kept as `data_as_of`), and the graph is read as it stands, because its records are dated by when they were
stored. The questions PDF is the answer key's source and is not part of `ekg-eval-raw/acme-docs`.
