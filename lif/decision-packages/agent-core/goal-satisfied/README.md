# goal-satisfied

| Purpose | Primitive | Labels | Used by | Risk |
|---|---|---|---|---|
| Is the goal done, still open, blocked or unclear? | choice | `done`, `not_done`, `blocked`, `uncertain` | `agent_loop.py` (done check), `swarm.py` (done check), mining compiler (`decide_continue`) | medium |

Spec: `v1.yaml`. Regression cases: `tests.jsonl` (synthetic, `slice` = common or edge).

## Why this shape

- One judgment: the goal's status, from listed artifacts, checks, remaining requirements and the last result.
- Each label names the state paths that decide it (e.g. `done` needs an artifact per requirement, empty remaining list and no failing check).
- Exit `uncertain` (also the default) for state that does not show the answer; never closes a workflow alone.

## Known limits

- An empty `remaining_requirements` with a requirement that has no artifact sits between `not_done` and `uncertain`.
- `blocked` relies on the summary wording; a slow but working service is `not_done`.
