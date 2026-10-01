# context-relevance

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Does an item's text help answer the question? | noul | `yes`, `no` | research source relevance, knowledge-work context relevance, swarm reducer (per spec comment) | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: does `item.text` help answer `task.question`. Nothing about quality or trust.
- Observable criterion: the text states a fact, figure, definition, procedure, example or claim. The title is explicitly excluded, so misleading titles can be tested.
- State is three short paths; no exit label, so the default `no` is the safe answer when the text is empty.

## Known limits

- Text that contradicts the answer counts as `yes`; callers that want only supporting evidence must filter later.
- Partial relevance (one useful sentence in a long text) is `yes`; there is no degree.
