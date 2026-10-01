# Cost model

**Every number this model produces is an ESTIMATE.** The UI Savings page and `lif_external_cost_avoided_usd_total` are labelled as estimates.

## Inputs (`config/lif.yaml`)

| Key | Value | Meaning |
|---|---|---|
| `cost.external_equivalent_usd_per_mtok` | input $0.15, output $0.60 | What the same tokens would cost on a comparable hosted small-model API (assumption, not a quote) |
| `decision_fabric.jev.price_usd_per_mtok_input` | $0.42 | Jev input-token price as configured. Output is free. If Jev's response includes `usage.cost_usd`, that is used instead |
| `cost.electricity_usd_per_kwh` | $0.16 | **declared, not yet used in code** |
| `cost.cpu_tier_incremental_watts` | 25 W | **declared, not yet used in code** |

## How it is computed

| Quantity | Where | Formula |
|---|---|---|
| Avoided external spend (per request) | gateway `_account()` | `prompt_tokens × 0.15/1e6 + completion_tokens × 0.60/1e6`, labelled by tier (`fast_local` / `large_local`) |
| Jev spend | Jev provider | `usage.input_tokens × 0.42/1e6` → `lif_jev_cost_usd_total` and `decisions.db` `cost_usd` |
| Jev 24 h spend | `/decision/status` `jev_cost_usd` | sum over `decisions.db` |
| Heavy-model calls avoided | `lif_tasks_total` | see OBSERVABILITY.md |

## What is NOT counted

- **Electricity and the hardware's amortized cost.** The machine runs 24/7 for the primary workload anyway; the CPU-tier power delta has not been measured.
- **Cache hits** (`tier=deterministic`). They avoid local compute, but no dollar value is attributed.
- **Embeddings** are counted at the same per-token rate as chat input. That is an overestimate for embedding APIs.
- **Quality differences.** A local 4B answer is not equivalent to a frontier model's, so "avoided spend" assumes the cheap hosted tier, not a frontier tier.
- **Availability probes and batch retries** add tokens. Filter `workload="availability-probe"` out of usage views.

## Status

No savings figure has been reported yet: there is not enough traffic. The final report states any number together with its window and these caveats.
