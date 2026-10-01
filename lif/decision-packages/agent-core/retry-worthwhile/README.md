# retry-worthwhile

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Is the error transient? | noul | `yes`, `no` | no call site in `lif/lif` yet; the retry budget itself is code | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: could the same call succeed if repeated. Attempts and backoff stay in code.
- Observable criteria: listed transient signals (timeout, rate limit, 5xx, starting) and listed permanent ones (bad input, permission, missing file, syntax).
- Both clauses must hold; a message with both kinds is `no`.

## Known limits

- Errors in neither list (e.g. out of memory) fall to `no` by the letter of the criteria.
- A 5xx whose body reports bad input is `no`; status code alone is not enough.
