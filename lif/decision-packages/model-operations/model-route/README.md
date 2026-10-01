# model-route

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Which model class handles the request? | choice | `local_fast`, `local_reasoning`, `kimi`, `human`, `none` | no call site in `lif/lif` yet; privacy, GPU state and budgets are applied in code after | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: task difficulty and modality only.
- Each class has observable triggers (lookup/extraction, multi-step or one-file code, multi-source or multi-file or image, legal/financial/security action or approval).
- Two exits: `human` for consequential actions, `none` for empty or greeting input.

## Known limits

- Image input with a trivial label task fits both `local_fast` and `kimi`; not covered by tests.
- Explaining a security procedure (`local_reasoning`) vs performing it (`human`) depends on wording.
