# model-advance

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Should discovery download and benchmark this model? | noul | yes / no | `lif/models/discovery.py`, observed in shadow through `POST /de/observe` | low (reversible: a download and a benchmark) |

## Why this shape

- **One judgment.** It replaces a 4-way suitability distribution plus hand-tuned P(advance) thresholds (0.70, or 0.50 plus a rule).
- **Comparisons are computed in code.** `comparison.newer_than_current` and `comparison.larger_than_current` arrive as booleans (rule 6).
- **Hardware fit, license and size limits stay deterministic.** They are filtered before this decision runs.

## Ground truth

Real outcomes are rare: few models reach a benchmark. Labels come from `lif/decision/autolabel.py` (`model-advance-rules-v1`): the criteria above checked in code for every observed case, scored 26/26 on `tests.jsonl`. No case goes to a human review queue (`observe.review: auto`).
