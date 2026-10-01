# change-satisfies-intent

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Does the change do what the task asked, and only that? | noul | `yes`, `no` | coding agent review step (per spec comment); tests remain the exit-code check | medium |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: intent coverage of `change.summary` against `task.text`; tests cover correctness separately.
- Two observable conditions: every requested behaviour appears, and no unrequested behaviour change is listed.
- Default `no`; low confidence escalates to a reviewer model.

## Known limits

- Whether tests, docs, renames or refactors count as behaviour changes is not stated; tests treat them as non-behaviour.
- Judges the summary, not the diff; an incomplete summary yields `no`.
