# Decision Engineering

Decision Engineering finds the small decisions hidden inside expensive generative calls and moves the suitable ones to typed Jev decisions. Each decision runs in shadow first, gets calibrated per decision, and is routed by calibrated confidence. Hard cases escalate to a local reasoning model, then Kimi K3, then a human.

It works in both directions. When the evidence says Jev is the wrong tool, it recommends returning the operation to code or to generation.

```
OBSERVE → TRACE → DECOMPOSE → CLASSIFY (CODE / JEV / GENERATIVE / HUMAN) → COMPILE CANDIDATE → TEST
  → SHADOW → CALIBRATE → OWNER PROMOTES → ROUTE BY CONFIDENCE → ESCALATE → OUTCOME → RECALIBRATE / RECOMMEND
```

| Need | Executor | Where |
|---|---|---|
| Exact result (counts, dates, limits, schema, permissions, exit codes) | **Code** | rules, `state_compiler`, `ToolPolicy`, acceptance checks |
| Bounded judgment (finite labels, confidence useful) | **Jev** (`jev-1.13.0`, pinned) | `cascade.py` → `DecisionFabric` |
| Creation (text, code, plans) | **Local model** → Kimi K3 if allowed | `sdk.Intelligence.generate()` |
| Uncertain bounded judgment | **Local reasoning → Kimi K3** | `escalation.py` |
| Irreversible / security / financial / publishing | **Human or hard policy** | `ToolPolicy`, `HumanReviewProvider`, `risk: critical` |

## Module map

All modules are in `lif/decision/`. There are no new pods: the runtime sits inside the existing `decision-fabric` service (spec §72).

| Spec | Module | What it does |
|---|---|---|
| §2, §21–23, §45, §78 | `cascade.py` | Code → Jev → per-decision zones → escalation chain → human queue or safe default. Handles rollout slices and dynamic choices |
| §18–20, §74–77 | `escalation.py` | `EscalationProvider` with `LocalReasoningProvider`, `KimiK3Provider`, `FrontierProvider` and `HumanReviewProvider`. Token bucket, TPM bucket, 429 reset headers, circuit breakers, context-size guard, label validation |
| §27, §77 | `pricing.py`, `config/providers.yaml` | Prices and limits are data. An unknown price is reported as unknown, never as $0 |
| §11, §31–33 | `registry.py`, `types.py` | Versioned specs, lifecycle stages, deterministic promotion gates, releases (pins), rollback |
| §12, §36–37 | `lint.py` | `decision lint`, covering 25 checks |
| §24–28, §61 | `calibration.py` | Reliability bands, ECE, coverage(T), error(T) with a Wilson 95% upper bound, consequence-driven thresholds, sample adequacy, what-if simulator |
| §29–30, §52 | `shadow.py` | Shadow evaluation, outcome feedback joined by state hash |
| §13–14 | `state_compiler.py` | Type filter → graph filter → secret redaction and untrusted-text wrapping → relevance ranking under a token budget. Knowledge objects come from the Knowledge Work layer |
| §15–17, §50 | `fanout.py` (+ `dag.py`) | Dependency waves. Independent decisions with mergeable state go in one Jev request. Logs the tokens saved |
| §79–80 | `sdk.py` | `decide()` vs `generate()`, `decide_many()`, `outcome()`, `RemoteIntelligence` |
| §39–44 | `agent_loop.py` | Dynamic tool sets, deterministic `ToolPolicy`, done-check AND acceptance checks, and `goal_satisfied()` |
| §47–49 | `swarm.py` | Provider-neutral swarm (Kimi has no swarm API): code filter → Jev triage → small-set synthesis |
| §34, §69, §100 | `experiments.py` | Decision tests, regression comparison, baseline-vs-candidate benchmark |
| §67–68 | `autotune.py` | Recommendations in both directions; never applied automatically |
| §53–54, §88 | `provenance.py` | Provenance records, plus the optional bridge into the Knowledge Work layer (durable facts only) |
| §82 | `instrument.py` | `TraceWriter`, which writes redacted LIF traces |
| §3–8, §51, §83–85 | `mining/` | Trace ingestion, segmentation, four-bucket classifier, miner, compiler, audit and optimize |
| §52, §102 | `jobs.py` | In-process cycle every 6 h: mine in-cluster traces → recalibrate → recommend |
| §62–66, §97 | `de_api.py` + Control Center **Decision Engineering** | API under `/de` (via the controller at `/v1/de`) and 11 UI tabs |

## Decision packages

Packages live in [`decision-packages/`](../decision-packages). Each `v<N>.yaml` is source code: it is never edited in place, and a change is a new version. The packages for the primary workload are private.

| Decision | Primitive | Package | First target (§90) | Status |
|---|---|---|---|---|
| context-relevance | noul | agent-core (reused by research and knowledge-work, §87) | context/source relevance | designed, 26 tests |
| goal-satisfied | choice: done / not_done / blocked / uncertain | agent-core | completion detection | designed, 29 tests |
| tool-selection | choice (dynamic) + none | agent-core | tool routing | designed, 26 tests |
| model-route | choice: local_fast / local_reasoning / kimi / human / none | model-operations | model routing | designed, 27 tests |
| result-usable, result-keep, retry-worthwhile, escalation-needed | noul / choice | agent-core | n/a | designed, 26–29 tests |
| model-shortlist | noul | model-operations (§93 stage 2) | n/a | designed, 26 tests |
| change-satisfies-intent | noul | coding (§95) | n/a | designed, 26 tests |

The test cases are synthetic. An LLM subagent wrote them, and a person has not reviewed them yet; spot-check about 3 cases per decision before treating them as ground truth.

**v2 backlog.** These ambiguities were found while writing the tests, and each is fixed with a new version:

- **escalation-needed:** "several sources" has no number.
- **result-keep:** an error message that contains diagnostic detail fits both keep and drop.
- **retry-worthwhile:** an out-of-memory error is in neither list.
- **tool-selection:** read and code overlap when the task is to open a workspace file.
- **change-satisfies-intent:** the criteria don't say whether refactors and docs count as behaviour changes.
- **model-route:** an image with a trivial label task fits both local_fast and kimi.

**Legacy `config/decisions/core.yaml`.** Lint finds no errors. Every entry warns `no-state-schema` and `no-state-path`, and the choice entries also warn `no-exit`. They stay `production` unchanged; fixes become v2 entries in shadow.

## Lifecycle, gates and pinning

```
discovered → designed → tested → shadow → calibrated → low_risk_automation (10%) → expanded_automation (50%) → production (100%)
                                         rollback from any stage · retired
```

| Gate | Blocks when |
|---|---|
| any stage ≥ tested | `decision lint` reports errors |
| ≥ shadow | fewer than 20 labelled tests, accuracy below 0.9, or a regression versus the previous version beyond `promotion.max_regression` |
| ≥ calibrated | the calibration sample is not adequate (see below) |
| automation stages | the HIGH threshold is below the calibrated recommendation; the actor is a machine (`autotuner`, `decision-miner`, …); or the decision is `risk: critical` or `reversible: false`, which can never be automated |

- **A bare decision name resolves only to a version that is serving.** That means a LIVE release, or else the newest YAML version with `stage: production`. Adding `x/v2` never moves callers silently.
- **A release pins** the version, stage, rollout %, zone thresholds, Jev model and state-compiler version. A pinned Jev model that is not the one deployed makes Jev ineligible for that decision, so it falls back.
- **The cache key** includes the decision ref, state, Jev model, state compiler and choice set.
- **Rollback** demotes the serving version to `shadow` and restores the previous LIVE release, or the YAML production version if there is none. The test `test_threshold_rollback_restores_previous_threshold` covers a bad threshold deployment.
- **Audit trail.** Every transition through the Control Center or the CLI is attributed to the authenticated admin and written to the activity timeline.

**Scope.** Releases govern decisions evaluated through the decision-fabric service: `/de/decide`, the SDK, the agent loop and DAG workflows. The gateway (`local/auto`), the batch engine and the controller still build their own YAML-pinned fabric, so promoting `request-route` will not change `local/auto` until the gateway calls `/de/decide`.

## Confidence routing

The policy is set per decision (`policy:` in the spec, overridden by the release). There is no universal threshold.

| Zone | Condition | Route |
|---|---|---|
| HIGH | conf ≥ `high`, not an exit label, top-2 margin ≥ `min_margin` | act (`route=auto`) |
| VALIDATE | `validate` ≤ conf < `high` and the caller's deterministic validator passes | act (`route=validated`) |
| MIDDLE | otherwise, or a flat distribution (§45), or an exit answer | `middle_route` providers in order. The first valid non-exit answer with self-confidence ≥ `accept` wins |
| LOW | conf < `low` | `low_route`: `safe_default` and/or `human` |
| Jev unavailable | any | `middle_route` then `low_route` (local-first, §78) |

- A spec with `mode: three_zone` and no calibrated `high` never acts automatically.
- `mode: two_zone` has no LOW zone.
- `mode: advisory` is forced for `risk: critical` or `reversible: false`. Jev's view is recorded, a human ticket is opened, and nothing acts.
- Callers act only when `decision.actionable`. A safe default, a pending review or an advisory decision is not actionable.

## Escalation providers

| Provider | Endpoint | Status 2026-10-01 | Notes |
|---|---|---|---|
| local-reasoning | gateway `local/reasoning` | live. Served by the 4B CPU model, so every answer carries `degraded: true` | ~15 s per escalation measured (n=1) |
| kimi-k3 | `https://api.moonshot.ai/v1`, model `kimi-k3` | **not configured**: no `MOONSHOT_API_KEY`, and `privacy.external_llm_allowed: []` | Thinking is always on. Sampling parameters are not sent. Context window 1,048,576 tokens. 429s carry `X-RateLimit-Reset`. No swarm API |
| frontier | any OpenAI-compatible endpoint | disabled placeholder | |
| human-review | SQLite queue → Control Center **Review Queue** | live | Answers become outcomes |

**Enabling Kimi:**

1. Put `MOONSHOT_API_KEY` in `.env.local`.
2. Run `scripts/create-secrets.sh`.
3. Add the data classes you accept to `privacy.external_llm_allowed`.

Every call still passes `policy.may_send`. Kimi's prices, limits and endpoint come from `platform.kimi.ai` docs as read on 2026-10-01; see `config/providers.yaml` for sources and the one unverified pricing note.

## Calibration

- **Ground truth** is observed outcomes and human answers. Agreement with the old executor is reported separately and is used only with `--use-agreement`.
- **Threshold rule.** The HIGH threshold is the lowest T whose Wilson 95% upper bound on automatic error is ≤ `max_error_by_risk[risk]`, with at least `min_auto` automatic samples. If no T qualifies, there is no threshold and the decision stays in shadow.
- **Tolerated error by risk:** low 5%, medium 2%, high 0.5%, critical never. These are policy inputs in `lif.yaml`, not measurements.
- **LOW zone.** Below this boundary the provider is no better than 50% (Wilson upper bound), so escalation is wasted spend.
- **Adequacy is evidence-driven, not calendar-driven (§30).** It requires all of:
  - at least 200 labelled samples
  - common and edge slices with at least 20 each
  - at least 20 low-confidence and 50 high-confidence samples
  - at least 5 observed failures
  - enough high-confidence samples to prove the risk bound (for low risk, zero errors needs 73)

## Mining

| Source | Reader | Notes |
|---|---|---|
| Claude Code sessions (`~/.claude/projects`) | `traces.claude_code_runs` | Primary-workload sessions are excluded by default (`traces.exclude`) |
| LIF trace format | `traces.jsonl_runs` | Written by `instrument.TraceWriter`, the agent loop and adapters |
| `decisions.db` | `traces.decisions_db_runs` | |

- **Redaction is structural.** Traces keep tool and program names, sizes, error flags, token counts and short labels that pass the privacy detectors. They never keep prompts, file contents, command arguments or outputs.
- **Honest attribution.** One LLM call often fuses a bounded decision (continue? which tool?) with generation (the tool arguments). A fused decision carries no avoidable cost and is listed as **embedded**. It only becomes savings once the agent is split into `decide()` then `generate()`, which is what `agent_loop.py` does. Heavy calls count as avoidable only when every operation in them is CODE or a separable decision.
- **Opportunity score:**

  ```
  log1p(calls/day) × log1p(avoidable tokens / 100) × latency penalty × boundedness × suitability × reversibility × consistency
  ```

  Any missing factor sinks the score.
- **Knowledge-aware mining (§51).** Only real code methods (`KNOWN_CODE_METHODS`, such as `hardware_fit`) reclassify an operation as CODE. Related knowledge objects (methods, rules, incidents) are listed for the designer to read first.
- **Measured audit.** The audit of this host's Claude Code sessions is in `private/lif/docs/DECISION_ENGINEERING_AUDIT.md` *(private, local only)*.

## CLI

| Command | Mode |
|---|---|
| `local-ai decision lint PATH` | offline; exit 1 on errors (use in CI) |
| `local-ai decision test NAME[/V] [--local --data-class PUBLIC] [--cases F]` | service, or in-process. With `--local --data-class PUBLIC`, a run that fell back off Jev is refused (exit 3) |
| `local-ai decision benchmark NAME/V` | compares to the previous version's latest test (regression policy) |
| `local-ai decision calibrate NAME/V [--use-agreement]` | service, or `--local` |
| `local-ai decision simulate NAME/V --volume N` | what-if table |
| `local-ai decision shadow NAME/V`, `promote NAME/V --to STAGE --threshold T`, `rollback NAME` | needs `LIF_INTERNAL_KEY` and a named `--actor` |
| `local-ai decision generate TRACE_PATTERN [--source DIR]` | offline; drafts go to `private/lif/decisions/` |
| `local-ai decision registry [NAME]`, `autotune NAME/V`, `inventory`, `outcome ID --outcome L`, `human [ID --answer L]` | service |
| `local-ai agent audit [AGENT] [--source D] [--save] [--upload]` | offline. `--upload` sends the redacted inventory to the UI |
| `local-ai workflow optimize [WORKFLOW]` | offline. A migration plan only; nothing is changed |

## Observability

| Metric | Meaning |
|---|---|
| `lif_agent_steps_total{agent,bucket}` | mined steps: code / jev_candidate / jev / generative / human / unknown |
| `lif_cascade_resolutions_total{decision,tier}` | which tier resolved each cascade decision |
| `lif_escalations_total`, `lif_escalation_seconds`, `lif_escalation_tokens_total`, `lif_escalation_cost_usd_total` | per provider (Kimi included) |
| `lif_provider_circuit_open`, `lif_provider_rate_limited_total` | resilience |
| `lif_jev_fanout_size`, `lif_jev_fanout_saved_input_tokens_total` | fan-out savings (ESTIMATE from state size) |
| `lif_shadow_decisions_total`, `lif_decision_outcomes_total`, `lif_human_review_queue`, `lif_heavy_generation_avoided_total` | |
| `lif:decision_offload_rate:1h`, `lif:frontier_generation_avoidance_rate:1h` | KPIs (§57–58), as recording rules |

Alerts:

| Alert | Fires when |
|---|---|
| `LIFEscalationProviderCircuitOpen` | an escalation provider's breaker is open |
| `LIFDecisionSafeDefaultsHigh` | more than 25% of a decision's results end on the safe default |
| `LIFHumanReviewBacklog` | more than 50 decisions are waiting for review |
| `LIFDecisionOutcomeErrors` | more than 10% of automatic decisions were wrong with n ≥ 20 in 24 h; suggests rollback |

The executive dashboard (Control Center → Decision Engineering → Overview) is computed from provenance and mined operations only. Empty means no data yet.

## Failure modes (§74)

| Failure | Behaviour | Test |
|---|---|---|
| TypeSafe unavailable, timeout, 5xx | breaker opens; local-first chain; safe default if all fail | `test_ready_J_jev_outage_uses_local_first_chain`, `test_jev_outage_falls_back_and_breaker_opens` |
| Rate limited (any provider) | token bucket backpressure, reset headers honoured, refusal instead of hammering | `test_kimi_429_honours_reset_header`, `test_rate_limiter_backpressure` |
| Invalid response or schema mismatch | the answer is rejected; next tier | `test_kimi_invalid_answer_is_rejected` |
| Pinned model unavailable | Jev ineligible for that decision | `test_pinned_jev_model_mismatch_skips_jev` |
| Kimi unavailable, rate limited, context too large | next tier, then human or safe default | `test_ready_I_…`, `test_kimi_context_too_large` |
| Local reasoning unavailable | safe default, never a guess | `test_ready_J_…` |
| Bad threshold deployed | rollback restores the previous release | `test_threshold_rollback_restores_previous_threshold` |
| Question version corrupted | `from_raw` rejects unknown fields; duplicate refs fail loading; lint gates promotion | `test_ready_D_…` |
| Registry or calibration store unavailable | `Cascade` without a registry serves YAML production versions; provenance write failures are logged, not raised | `test_grouped_cascade_is_one_jev_request` |
| State compiler sends irrelevant context or secrets | schema filter, ranking budget, redaction | `test_state_compiler_minimum_sufficient_state` |
| State compiler omits evidence | `MissingState` → not actionable | same |
| Prompt injection in state | untrusted text is wrapped; policy authority never comes from state (§71) | suites' injection edge cases, `ToolPolicy` |

## Production readiness (§99)

| | Item | Evidence |
|---|---|---|
| A | trace → atomic operations | `test_ready_A_trace_decomposed_into_atomic_operations` |
| B | miner finds bounded decisions | `test_ready_B_miner_identifies_bounded_decisions`, `test_generic_llm_cluster_with_small_output_domain_is_jev` |
| C | compiler emits a valid candidate | `test_ready_C_compiler_creates_valid_candidate` |
| D | linter catches anti-patterns | `test_ready_D_lint_catches_known_antipatterns` |
| E | candidate runs in shadow | `test_ready_E_shadow_candidates_never_change_the_answer` |
| F | calibration metrics | `test_ready_F_calibration_metrics_and_threshold_by_consequence` |
| G | high confidence → automatic | `test_ready_G_high_confidence_routes_automatically` |
| H | low confidence → Kimi or fallback | `test_ready_H_middle_zone_escalates_to_kimi_with_package` |
| I | Kimi outage → safe fallback | `test_ready_I_kimi_outage_falls_back_safely` |
| J | Jev outage → safe fallback | `test_ready_J_jev_outage_uses_local_first_chain` |
| K | price change needs no code change | `test_ready_K_pricing_change_needs_no_code_change` |
| L | questions versioned | `test_ready_L_new_version_does_not_move_bare_name` |
| M | rollback restores the previous implementation | `test_ready_M_promote_then_rollback_restores_previous` |
| N | fan-out reduces repeated state | `test_ready_N_fanout_reduces_repeated_state_submission` |
| O | Kubernetes workloads healthy | **verified 2026-10-01**: image `20261001-1030-de` deployed. All LIF pods are Running with 0 restarts, and `local-ai doctor` shows no new failures. A live `/de/decide` was answered by Jev in 405 ms (`auto`) with provenance stored, `/v1/de` requires the admin key, and the nightly backup now includes `decision-eng.db` |
| P | primary workload unaffected | **not verified**. Decision Engineering uses no GPU. But the 2026-10-01 benchmark drove the CPU tier while host MemAvailable was about 6 GiB, causing about 1.1 GB/s page-ins and 25% iowait until it was stopped; any effect on the primary workload is unknown. Since then, local-inference benchmark arms also wait for 8 GiB of MemAvailable |
| Q | completion never relies only on Jev | `test_ready_Q_completion_needs_acceptance_checks_not_just_jev` |
| R | irreversible stays human/policy | `test_ready_R_irreversible_decisions_stay_human`, `test_ready_R_tool_policy_is_deterministic` |

## Benchmark (§100)

See [`benchmarks/decision-engineering/`](../benchmarks/decision-engineering/).

Measured 2026-10-01 on the Jev arm, n = 267 labelled agent decisions across 10 decision packages.

| | Value |
|---|---|
| Jev accuracy | 94.4% overall, per decision 84.6–100% |
| Auto-resolved at T=0.9 | 181/267 (67.8%), all correct (95% upper error bound 2.1%). Exit answers are excluded because they escalate |
| Misses | 15. About 10 are spec ambiguities (v2 backlog); about 5 are genuine Jev errors, all at confidence ≤ 0.84 |
| Latency p50 / p95 | 167 / 209 ms |
| Cost | $0.049 |

**Not yet measured.** The LLM-first baseline and the local escalation arm were stopped to protect the host while memory headroom was low; see the benchmark README for the rerun command. The labels are synthetic and not yet reviewed by a person.

**Real-agent evidence.** The audit of real Claude Code sessions is private *(private, local only)*. Its structural finding: bounded decisions are about half of all steps there, but they are fused with generation, so the savings come from splitting the agent loop and reducing context, not from swapping in Jev.
