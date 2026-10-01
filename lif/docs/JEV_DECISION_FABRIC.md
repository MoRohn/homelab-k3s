# Jev Decision Fabric

Jev (TypeSafe AI, model pinned to `jev-1.13.0`) returns **typed, calibrated decisions** (choice / score / noul), not text. LIF treats it as a recommendation engine. **Policy code turns recommendations into actions. Jev never acts directly.**

## API contract (verified live)

| | |
|---|---|
| Endpoint | `POST https://api.typesafe.ai/v1/systemone` |
| Auth | `Authorization: Bearer <TYPE_SAFE_JEV_API_KEY>` |
| Body | `{"model": "jev-1.13.0", "state": <str\|object\|array>, "questions": {<key>: {type, instructions, criteria?}}}` |
| choice answer | `{choice, confidence, probabilities{label: p}}` |
| score answer | `{score (fractional), confidence, probabilities{"<level index>": p}}` |
| noul answer | `{noul: p}`. LIF derives `confidence = max(p, 1−p)` |
| Usage | `usage.input_tokens`. Cost is estimated at the configured $0.42 / M input tokens; output is free |
| Latency | ~0.3 s typical. One grouped call answered 2 decisions in ~825 ms |

Other third-party "Jev docs" domains are not TypeSafe's (see SECURITY.md, private, local only).

## Runtime (`lif/decision/`)

1. **Cache.** Key = sha256(decision ref, canonical JSON state, pinned Jev model, the first provider that would answer). A rules answer cached during an outage is not served once Jev is back.
2. **Privacy gate.** The caller's declared class (or else the definition's `data_class`) is raised by content detectors, then checked with `policy.may_send(cls, "jev")`. By default only **PUBLIC** goes out.
3. **Jev.** All pending decisions that read the same state go out as **one request**. Concurrency is capped at 16. The timeout is 4 s, with one retry on 502 or timeout. The circuit breaker opens after 5 failures and resets after 60 s. 401, 402 and 403 open it immediately.
4. **Fallback chain**, per definition `fallback:`.
   - `rules` is deterministic and may abstain.
   - `local_llm` is the local fast model picking an option letter under a grammar, with probabilities from logprobs. Its confidence is **capped at 0.95**, below auto, because it is uncalibrated.
   - If everything fails, the definition's `default` is returned and **forced to ESCALATE**.
5. **Policy gate** (`policy.gate`). Defaults come from `decision_fabric.confidence`, and each definition can override them.

| Confidence | Gate | Meaning |
|---|---|---|
| ≥ auto (0.97) | `auto` | act |
| ≥ validate (0.85) | `validate` | act only if deterministic validation also passes |
| ≥ escalate (0.70) | `local_llm` | confirm with the local model |
| below | `escalate` | larger model or a human |

## Decision registry (`config/decisions/core.yaml`)

| Decision | Type | data_class | Thresholds (auto/validate/escalate) | Fallback | Used by |
|---|---|---|---|---|---|
| model-suitability/v2 | choice: reject/review/benchmark/candidate | PUBLIC | 0.90/0.75/0.55 | rules | discovery DAG |
| improvement-probability/v1 | score (5 levels) | PUBLIC | defaults | rules | discovery DAG |
| operational-risk/v1 | score low/moderate/high | PUBLIC | defaults | rules | discovery DAG |
| candidate-vs-incumbent/v1 | noul | PUBLIC | defaults | none (default "no") | benchmark compare (advisory) |
| request-route/v1 | choice instant/fast/default/reasoning/code | CONFIDENTIAL | 0.85/0.70/0.50 | rules | gateway `local/auto` |
| output-acceptable/v1 | noul | CONFIDENTIAL | defaults | rules | `/decision/validate` |
| batch-priority/v1 | score deferrable/normal/urgent | CONFIDENTIAL | defaults | rules | batch submission |
| batch-model-size/v1 | choice fast/default/large | CONFIDENTIAL | defaults | rules | batch submission (hint only) |
| gpu-admission/v1 | choice use_fast/load_default/queue | PUBLIC (numeric telemetry) | 0.90/0.75/0.60 | rules | defined; no GPU tier yet to decide about |

Each definition is versioned (`name/version`). Logged decisions keep their ref.

## DAG runtime (`lif/decision/dag.py`)

- Nodes run the moment their dependencies finish.
- **Jev nodes that become ready together and read the same state are folded into one request.**
- `candidate-model-analysis/v1`:
  - `license`, `architecture_fit` and `comparison` are deterministic.
  - `suitability` and `operational_risk` go to Jev as one call.
  - `improvement` goes to Jev as one call, over `comparison`.
  - `recommendation` is a policy node.
- Test `test_dag_runs_independent_nodes_concurrently` proves the parallelism: 3 × 0.2 s nodes finish in under 0.45 s.

## Why discovery uses P(advance), not the argmax

On real Hugging Face metadata, Jev's four-way `model-suitability` distributions were **flat**. Examples:

- Qwen3-Embedding-4B: benchmark 0.33, reject 0.27, review 0.22, candidate 0.18, with confidence 0.11.
- A distill from an unknown publisher: argmax "benchmark" at 0.44, confidence 0.25.

Shortlisting on the argmax admitted that distill. The policy node now uses the mass on advancing, `P(advance) = P(benchmark) + P(candidate)`:

- **Shortlist** if P(advance) ≥ **0.70** (Jev alone).
- **Shortlist** if P(advance) ≥ **0.50** *and* the deterministic rule independently says benchmark/candidate (reputable org plus download volume).
- Otherwise **review**: visible, but not downloaded.

Result: the distill moved to review. Qwen2.5-Coder 1.5B/7B/14B (0.74–0.85) and embeddinggemma/mxbai (0.77–0.83) were shortlisted. Qwen3-Embedding-0.6B (0.52) was shortlisted only because the rule agreed.

**Takeaway:** Jev is useful as a ranker here, but it is not decisive on its own for this question. That is measured, not assumed.

## Service API (`decision-fabric.ai-system.svc:8080`, in-cluster only)

| Route | Body |
|---|---|
| `POST /decision/evaluate` | `{decision, state, data_class?, thresholds?}` |
| `POST /decision/batch` | `{decision, states[≤10000], data_class?, concurrency?}` → results plus decisions/sec |
| `POST /decision/score` · `/route` · `/validate` | shorthands |
| `POST /decision/workflow` | `{workflow, input, data_class?}` → per-node results, waves, timings, jev_calls |
| `POST /decision/control` | `{jev_enabled: bool}` kill switch · `{invalidate_cache: true, decision_ref?}` |
| `GET /decision/status` · `/workflows` · `/definitions` · `/recent` | 24 h stats; the inspector feed (typed records only, never reasoning) |

Decisions are persisted to `decisions.db` for 30 days.

## Outage behaviour

| Situation | Effect |
|---|---|
| Jev down or no internet | The breaker opens, rules answer, and `LIFJevCircuitOpen` alerts after 10 min. Inference is unaffected: the gateway uses Jev only for `local/auto` on PUBLIC prompts |
| Jev disabled (`provider: rules`, or the kill switch) | Rules plus local LLM only |
| Credits exhausted (402) | Treated as an outage until the reset |

## Measured: Jev vs LLM-first (spec §50)

Workload: AG News test split, first 200 stories, 4-way topic classification (the shape of news story
triage; public data, so Jev is allowed). Reproduce with `benchmarks/jev-vs-llm/run.py`; raw results in
`benchmarks/jev-vs-llm/results-*.json`.

| | A: LLM-first (local/fast, 4B CPU) | B: Jev + selective local LLM |
|---|---|---|
| Accuracy | 0.850 | **0.880** |
| Wall time (200 items) | 367.8 s | **2.6 s** |
| Throughput | 0.54 items/s | **75.6 items/s** |
| Latency p50 / p95 | 3,548 / 6,010 ms | **170 / 583 ms** |
| Local LLM calls | 200 | **18** (9 % escalated by the policy gate) |
| LLM prompt tokens / generated tokens | 18,740 / 456 | 1,707 / 52 |
| Jev calls / spend | 0 / $0 | 200 / **$0.037** (ESTIMATE from billed input tokens) |
| External LLM calls | 0 | 0 |

- Jev alone was right on 182 auto-resolved items at 0.885. The 18 escalated items scored 0.833 on the LLM.
- LLM avoidance: **91 %**.
- Conclusion for this workload: the fabric is both more accurate and about 140× faster in wall time, at
  a small external cost. It is **not** evidence for workloads with private data (Jev is not allowed to
  see those) or for generative tasks.

## Kill switch

Settings → `jev_disabled` (or `POST /v1/settings {"jev_disabled": true}`):

- The controller publishes it in `/v1/routing`.
- The gateway and batch engine apply it on their 15 s refresh, and the decision-fabric service gets it
  via `/decision/control`.
- Measured: every service was on rules within ~15 s, and back on Jev within ~15 s of re-enabling.

## Outage behaviour (observed, not simulated)

On 2026-10-01 the in-cluster Secret briefly held a mis-parsed key, so Jev returned 401:

- The breaker opened and every decision ran on rules.
- Discovery run 1 completed on rules, and inference was unaffected.
- An egress-blocked "internet loss" test gave the same result.
