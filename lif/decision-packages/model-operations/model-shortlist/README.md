# model-shortlist

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Is the model built for the category? | noul | `yes`, `no` | model discovery stage 2 (per spec comment); hardware fit is checked in code after | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment per candidate: does it target the task named in `category.name`.
- Observable criteria: pipeline tag or summary names the task; summary does not mark it as test, toy, unclear merge or other task.
- Default `no`; licence, architecture and size are filtered in code before this.

## Known limits

- There is no pipeline tag for reranking; rerank candidates rely on the summary.
- "code" category is read as code generation; code-domain classifiers are labelled `no`.
