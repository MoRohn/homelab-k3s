# understanding-diagram-family

See `../README.md` for the purpose, the labels and how the router uses the answer.
Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic feature states, `slice` = common or edge).
The deterministic fallback is in `lif/lif/decision/rules.py`. All cases pass against it.
