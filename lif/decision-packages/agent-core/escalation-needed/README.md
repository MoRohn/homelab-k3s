# escalation-needed

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Does the task need a reasoning model? | noul | `yes`, `no` | model escalation (agent-core); no call site in `lif/lif` yet | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: fast model vs reasoning model, independent of privacy or budget.
- Observable criteria: proof, multi-step derivation, dependent plan, multi-source comparison, or 3+ facts combined.
- Single state path `task.text`; default `yes` errs toward the stronger model.

## Known limits

- "Several sources" is not given a number; comparisons of two named sources are a boundary case.
- Wording such as "step by step" or "plan" on a trivial task should not trigger `yes`; tests cover this but the criteria do not say so.
