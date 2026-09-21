# Bridgette semantic retrieval and evaluation

Bridgette uses one production query path. Ranked semantic retrieval is always
added to the existing planner context; it is not an execution allowlist and it
does not remove any catalog resource, field, metric, join, typed-plan guard, or
guarded fallback. The temporary `off`/`shadow`/`on` rollout modes are therefore
not part of the completed implementation.

The versioned knowledge source is
`backend/app/data/query/bridgette_semantic_aliases_v1.json`. It augments the
complete SNBI dictionary, official code meanings, semantic catalog, joins, and
metric registry. Every API evidence event records the retrieval version and
knowledge hash.

The complex bank contains 600 cases. Together with the original 100-case SNBI
bank and 15 deck-area examples, the default live suite contains 715 cases.

Run the deterministic regression tests:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_semantic_retrieval.py backend/tests/test_bridgette_complex_question_bank.py -q
```

Run a real local API band against the configured project database and provider:

```powershell
backend\.venv\Scripts\python.exe scripts\run_bridgette_live_evaluation.py `
  --base-url http://127.0.0.1:8000 `
  --bank backend\app\data\query\bridgette_complex_question_bank_v1.json `
  --offset 0 --limit 100 --concurrency 1 `
  --output tmp\bridgette-live-complex-001-100.json
```

Use offsets 100, 200, 300, 400, and 500 for the remaining bands. A receipt
records no authentication value, answer prose, or result rows. It distinguishes
verified typed-plan answers from bounded provisional guarded-SQL answers and
checks requested time scope plus required semantic evidence. Provider billing
or credit failures are external test blockers and must not be counted as agent
regressions.
