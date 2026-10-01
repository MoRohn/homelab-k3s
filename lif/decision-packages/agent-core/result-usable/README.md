# result-usable

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Did the tool result give the step what it needed? | noul | `yes`, `no` | mining compiler (`judge_result`) | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: does `result.summary` contain what `step.intent` names.
- Two observable conditions: the named information/file/value/confirmation is present and `result.status` is "ok".
- Default `no` keeps the agent from continuing on a bad result.

## Known limits

- Status values other than "ok" (e.g. "partial", "OK") are not enumerated; tests treat only "ok" as passing.
- A negative answer ("no free lease") counts as `yes` if it answers the intent.
