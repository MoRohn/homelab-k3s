# Decision Engineering benchmark

`run.py` compares LLM-first decisions with the intelligence cascade on the labelled suites in
[`decision-packages/`](../../decision-packages) (`tests.jsonl`, synthetic, declared PUBLIC).

| Arm | Executor |
|---|---|
| baseline | each case → `local/fast` (Qwen3-4B, CPU) picks a label from the raw state, which is what LIF agents do today |
| jev | state compiler → `jev-1.13.0`, once per case |
| escalation | cases below T, or exit answers → `local/reasoning` with the Jev distribution; answers below 0.7 → safe default |

Exit answers (`none`, `uncertain`, …) are never counted as automatic. The cascade escalates them.

Thresholds are not calibrated (about 27 cases per decision cannot prove an error bound), so results are a sweep over
T ∈ {0.7, 0.8, 0.9, 0.95, 0.98}. Every row is computed from cached per-case answers (`recompute PATH`).

The run yields to the primary workload. It pauses while that workload is HIGH or IMMINENT, and, for any arm using local
inference, while host MemAvailable is below 8 GiB.

## 2026-10-01: Jev arm (`results-20261001-0925.json`, n = 267, 10 decisions)

| Decision | n | Jev accuracy | common / edge | mean conf | auto at T=0.9 | auto accuracy at T=0.9 |
|---|---:|---:|---:|---:|---:|---:|
| context-relevance/v1 | 26 | 100% | 100% / 100% | 0.954 | 25 | 100% |
| escalation-needed/v1 | 26 | 96.2% | 94.4% / 100% | 0.872 | 17 | 100% |
| goal-satisfied/v1 | 29 | 86.2% | 84.2% / 90.0% | 0.883 | 20 | 100% |
| result-keep/v1 | 26 | 92.3% | 94.1% / 88.9% | 0.881 | 18 | 100% |
| result-usable/v1 | 29 | 96.6% | 100% / 90.9% | 0.938 | 25 | 100% |
| retry-worthwhile/v1 | 26 | 100% | 100% / 100% | 0.890 | 17 | 100% |
| tool-selection/v1 | 26 | 96.2% | 94.1% / 100% | 0.883 | 15 | 100% |
| change-satisfies-intent/v1 | 26 | 84.6% | 94.1% / 66.7% | 0.854 | 14 | 100% |
| model-route/v1 | 27 | 100% | 100% / 100% | 0.911 | 13 | 100% |
| model-shortlist/v1 | 26 | 92.3% | 100% / 77.8% | 0.895 | 17 | 100% |
| **pooled** | **267** | **94.4%** | | | **181 (67.8%)** | **100%** (95% upper error bound 2.1%) |

| | Value |
|---|---|
| Jev calls / input tokens / cost | 267 / 117,434 / $0.049 (at the configured $0.42/M) |
| Jev latency p50 / p95 | 167 / 209 ms (sequential) |
| Mean compiled state | 42.5 tokens. The question (instructions and criteria) is about 90% of the tokens billed |
| GPU | none |
| Kimi K3 | not configured |

Findings:

- **Thresholds differ by decision (§22).**
  - At T=0.7, goal-satisfied auto-acts on 26 cases at 88.5% and change-satisfies-intent on 22 at 90.9%.
  - context-relevance is already 100% at T=0.7.
  - A single global threshold would be wrong for most of them.
- **Most misses are spec ambiguities, not Jev errors.** I read all 15 misses:
  - About 10 sit on boundaries the criteria leave open:
    - goal-satisfied, 4 cases: "uncertain" vs "not_done" when nothing is evidenced.
    - result-keep, 2 cases: whether "Checked." is a drop or insufficient information.
    - change-satisfies-intent: whether an added test counts as a behaviour change.
    - model-shortlist: whether "code" means code generation.
    - tool-selection: read vs code.
    - escalation-needed: an arithmetic question that belongs in code.
  - About 5 are genuine Jev errors, for example "status ok" with the resize still pending, and a CLIP text encoder accepted for a sentence-embedding category. Every one of them had confidence ≤ 0.84, so none would auto-act at T=0.9.
  - The next step is v2 *criteria* (see the v2 backlog in docs/DECISION_ENGINEERING.md), then a re-test.
- **The benchmark found a spec bug.** model-route/v1 declared `human` and `none` as *exits*, so the cascade escalated real routing answers. [model-route/v2](../../decision-packages/model-operations/model-route/v2.yaml) adds a true exit.
- **Fan-out saves less here than expected.** States are small; most input tokens are the question text, and that is billed per question even in a grouped request.

Not yet measured:

- **The baseline and escalation arms.** The 2026-10-01 attempt was stopped at once: driving tier0 at about 6 GiB MemAvailable caused about 1.1 GB/s page-ins and 25% iowait. The memory gate above came from that. Rerun when the host has headroom:

  ```bash
  ARMS=baseline,jev,escalation .venv/bin/python benchmarks/decision-engineering/run.py
  ```
- **The labels.** They are LLM-authored and not yet reviewed by a person.
