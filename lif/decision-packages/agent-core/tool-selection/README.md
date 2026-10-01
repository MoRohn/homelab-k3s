# tool-selection

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Which tool runs next? | choice | `search`, `read`, `code`, `knowledge`, `none` | `agent_loop.py` (default `select_decision`), mining compiler (`select_tool`) | low |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: the next tool, from the goal and the last step only.
- Each option has an observable trigger; `read` requires the file or record to be named in `last_result_summary`.
- Exit `none` (default) when the answer can be written or the agent should stop. At run time only usable tools are offered.

## Known limits

- Opening a workspace file overlaps `read` and `code`; tests avoid that overlap.
- `knowledge` (team runbooks/decisions) vs `search` (external) depends on whether the goal is about the team.
