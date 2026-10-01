# result-keep

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Keep or drop a worker result? | choice | `keep`, `drop`, `insufficient_information` | `swarm.py` (default `triage_decision`) | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment per worker result: would a final answer to the goal use it.
- Observable criteria: evidence/data/partial solution vs off-topic, empty, error or task echo.
- Exit `insufficient_information` (default) for cut-off or too-short results.

## Known limits

- A negative finding ("X is not the cause") is labelled `keep` as evidence; the criteria do not say so explicitly.
- An error message that carries diagnostic detail is still `drop` by the letter of the criteria.

## v2 status

`v2` (bare-status wording) **failed the regression gate** on 2026-10-01: the common slice fell from 94.1% to 88.2% at equal overall accuracy. Keep using v1; v2 stays at `designed` as a record.
