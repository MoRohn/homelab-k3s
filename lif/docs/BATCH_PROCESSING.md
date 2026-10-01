# Batch processing

The batch service is `lif/batch/engine.py` and `lif/batch/app.py`, running as `ai-system/batch` with SQLite on the `lif-batch` PVC. Clients reach it through the gateway (`/v1/batch*`) with a gateway key.

## API

| Route | Notes |
|---|---|
| `POST /v1/batch` | `{model (default local/batch), endpoint ("/v1/chat/completions" \| "/v1/embeddings"), items:[{custom_id, messages \| input, max_tokens?}], priority? (4–8), deadline? (unix ts), description?, idempotency_key?, timeout_sec?, max_attempts?}`. Returns 201 (new), 200 (idempotent repeat) or 400 |
| `GET /v1/batch` | list (`data`) |
| `GET /v1/batch/{id}` | state, progress counts, classification, wait reason |
| `GET /v1/batch/{id}/results` | `{custom_id, status, response \| error}`. The response includes the gateway's `lif` metadata |
| `DELETE /v1/batch/{id}` | cancel pending items; items already running finish |
| `POST /v1/batch/{id}/pause` · `/resume` | per job |
| `GET /v1/batch/stats` · `POST /v1/batch/control {paused, reason}` | engine-wide (the control route is in-cluster only, used by the controller) |

## Behaviour

| Aspect | Implementation |
|---|---|
| Durability | Jobs and items in SQLite. Each item is its own checkpoint. On start, `running` items go back to `pending` |
| Ordering | priority ascending, deadline ascending, created ascending |
| Admission | `can_run(Job(priority, device="cpu"))` per job each tick. RUN → global concurrency 2. RUN_DEGRADED → 1. QUEUE/PAUSE → not dispatched, with the reason recorded |
| Retries | 5xx, 429, timeouts and transport errors back off 2^attempt s (≤ 60 s), up to `max_attempts` (default 3). 4xx is a permanent failure |
| Deadlines | Past the deadline, remaining items become `expired` |
| Idempotency | Same `idempotency_key` → same job |
| Global pause | The controller's `batch_paused` or `maintenance` setting, or `batch.enabled: false` |
| Dispatch | Through the gateway with `X-LIF-Workload: batch:<id>`, `X-LIF-Priority` and `X-LIF-Data-Class`, so all routing and yield rules apply |

## Jev classification at submission

- One grouped call evaluates `batch-priority` and `batch-model-size`, using only the job's description and metadata (never item contents).
- The default data class is CONFIDENTIAL, so **rules** answer unless the caller declares PUBLIC.
- Without a caller priority, the result sets it: deferrable → 6, normal → 5, urgent → 4. This applies only when the gate is auto or validate; otherwise the priority is 5.
- `batch-model-size` is stored as `model_hint`. **The caller's alias is never changed.**
- A classification failure never blocks a submission.

## Tests

`tests/test_batch.py`, 15 tests, cover submit, idempotency, ordering, primary-workload pause and resume, retry, permanent failure, cancel, restart recovery and deadlines.

**Validated live (2026-10-01):**
- A 40-item topic-tagging batch (P5) went through the deployed gateway: 40/40 succeeded, accuracy 0.88 against AG News labels, served by `qwen3-4b-instruct-2507-q4km-cpu`.
- Jev classified the job from metadata only. `batch-model-size` = fast (1.0, auto) was recorded as `model_hint`. `batch-priority` = normal (0.42 → escalate), so the caller's P5 was kept.
- Re-submitting with the same `idempotency_key` returned the same job id (HTTP 200).
