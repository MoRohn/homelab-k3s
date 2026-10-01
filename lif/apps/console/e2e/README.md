# Console e2e harness

The console runs against **fake upstreams** here, so you need no cluster, GPU, secrets or inference.
Everything in this directory is for development and tests only. `.dockerignore` excludes `apps/console/e2e`, and
the fixture data lives only here, never in the product.

| File | What it is |
|---|---|
| `fake_upstreams.py` | One FastAPI app that imitates the controller, gateway, batch engine and Prometheus on one port |
| `run_dev.sh` | Starts the fake upstreams on `:8091` and the console on `:8090` (fresh SQLite DB in a temp dir) |

## Run

```sh
lif/apps/console/e2e/run_dev.sh            # scenario: healthy
lif/apps/console/e2e/run_dev.sh fallback   # or start in another scenario
```

| Item | Value |
|---|---|
| Console | `http://127.0.0.1:8090`. The first visit goes to `/setup`. |
| Setup code | `dev-setup-code` |
| UI with hot reload | `cd lif/apps/console && npm run dev` (vite proxies `/api` to `:8090`) |
| Console env | `LIF_{CONTROLLER,GATEWAY,BATCH,PROMETHEUS}_URL=http://127.0.0.1:8091`<br>`LIF_CONSOLE_INSECURE_COOKIES=1`<br>`LIF_CONSOLE_ADMIN_KEY=dev` and `LIF_CONSOLE_GATEWAY_KEY=dev`<br>`LIF_SECRETS_DIR` points at an empty temp dir<br>`LIF_KNOWLEDGE_ROOT` is unset, so the bundled read-only knowledge is used |
| Ports | Override with `FAKE_PORT` and `CONSOLE_PORT`. If a port is already busy, the script stops. It never kills another process. |
| Cleanup | Ctrl+C stops both servers and deletes the temp dir. |

To run only the fake upstreams, use `lif/.venv/bin/uvicorn --app-dir lif/apps/console/e2e fake_upstreams:app --port 8091`.

## Scenarios

To install or remove the fake vision model: `curl -XPOST localhost:8091/__vision -H 'content-type: application/json' -d '{"installed":true}'`.

To switch scenarios while running:
`curl -XPOST localhost:8091/__scenario -H 'content-type: application/json' -d '{"name":"fallback"}'`.
`GET /__scenario` returns the current scenario.

Posting the same name again resets the world. The reset rebuilds all data deterministically, relative to now, and
cancels the old world's background work. `/__scenario` keeps answering in every scenario, including `offline`.

| Scenario | What the console should show |
|---|---|
| `healthy` (default) | Everything is serving, and one batch job is running.<br>The degraded roles are honest and by design: `local/code` and `local/reasoning` are smaller than their size floors, and vision and rerank are not deployed. |
| `fallback` | The 4B server crash-loops (Prometheus shows `CrashLoopBackOff` with 7 restarts).<br>`local/default` and `local/fast` are served by the 1.7B fallback. The reason is `primary unavailable: All connection attempts failed`. `local/default` is also degraded, because 1.7B is below its 4B floor.<br>Alerts are `LIFDefaultDegraded` and `KubePodCrashLooping`. |
| `blerbz-busy` | A primary-workload production lease is live, so `/v1/gpu` reports `IMMINENT` with reason `1 production lease(s) live` and memory is low.<br>The memory guard shed `tier0-small` and `embedding`, so `local/embedding` is unavailable and `local/instant` falls back to 4B.<br>Batch jobs wait with reason `waiting: primary workload IMMINENT (1 production lease(s) live): batch/eval paused`.<br>Downloads and benchmarks are refused with 409. |
| `offline` | Every upstream answers 503. Prometheus uses its own error envelope. |

## What the fake data contains

All of it is synthetic and generic. None of it is a production measurement.

- **Models:** three seeded production servers (4B, 1.7B, embedding).
- **Candidates:**
  - one APPROVED candidate, with a benchmark and a full `screening.comparison`: CANARY vs the 4B incumbent on `local/default`
  - two shortlisted candidates from the latest discovery run
  - one REJECTED candidate
  - one GPU-only candidate, which can't be benchmarked
- **Discovery runs:** three of them, with full funnels: succeeded, failed, and succeeded with 2 shortlisted.
- **Batch jobs:** running, paused by owner, queued, completed, failed and cancelled. A ticker advances dispatchable jobs by one item per second.
- **Decision reviews:** one pending `model-advance/v1` ticket (labels yes and no) and one answered ticket.
- **Traces:** two mined runs.
- **Other data:** activity events of many kinds, plus settings, availability, storage, savings and Decision Engineering overview and providers. Kimi is off: `no MOONSHOT_API_KEY secret`.
- **Gateway chat:** synthetic answers at about 20 tok/s.
  - Streaming responses carry `X-LIF-*` headers and a final usage chunk with `lif` metadata, then `[DONE]`.
  - `local/auto` resolves through a rules stand-in; with images it goes straight to `local/vision`.
  - Images (OpenAI `image_url` content parts) sent to a text alias get the gateway's 422 (`type: not_supported`,
    `code: images_not_supported`). Without a vision model, `local/vision` gives 503 (`no local model is deployed for
    local/vision; no vision model is installed yet`).
  - A prompt containing `[fail-midstream]` emits the gateway's mid-stream error with no `[DONE]`.
  - A prompt containing `[long]` streams for about 12 s, so a second browser can watch the answer grow.
- **Vision:** no vision model by default, as in the real world today. `POST /__vision {"installed": true}` installs
  one (a PRODUCTION `local/vision` model with its image projector, `mmproj`); a scenario reset removes it. The console
  sees the change at its next routing poll (up to 30 s). A discovery run that includes `vision` shortlists a vision
  candidate whose profile and `gguf_pick` carry an `mmproj` file.

## Fidelity rules

The fake **reproduces real gaps instead of fixing them**, so the console's honesty paths are exercised. The source
for shapes is the planning notes, which cite the real services.

| Real behaviour kept | Where |
|---|---|
| `POST /v1/models/refresh` returns no run id. A running run has `funnel: {}` until it finishes, about 6 s later. | controller |
| Batch has no `waiting` state. A blocked job stays `queued` or `running`, and only `reason` starts with `waiting:`. | batch |
| Cancel is `DELETE /v1/batch/{id}`, not `POST …/cancel`. `POST /v1/batch/control` returns 403 without `X-LIF-Internal`. Global pause goes through controller `POST /v1/settings {batch_paused}`. | batch |
| Posting `batch_paused` or `maintenance` to settings sets the batch pause from the request body only. | controller |
| `/v1/aliases` omits `local/vision` and `local/rerank`. `/v1/routing` lists them with empty chains. | controller |
| An answered review ticket can be answered again. An answer outside the labels gets a 400. | controller `/v1/de/human/{id}` |
| `X-LIF-*` headers appear only on streaming chat. Non-streaming chat carries `body.lif`, including `blerbz` and `latency_ms`. | gateway |
| Pin, block and delete of an unknown model return `{ok: true}`. A lifecycle op on an unknown model returns 409. | controller |
| Production storage bytes are 0, because the seeded profiles have no size. The 1.7B model has no benchmark. | controller |
| The error envelope differs per service:<br>controller `{error: str}`<br>gateway `{error: {message, type, lif?}}`<br>batch `{error: {message}}`<br>Prometheus `{status: "error", errorType, error}` | all |
| Auth: controller admin paths need `Authorization: Bearer dev`, and so do gateway `/v1/capabilities` and `/v1/chat/completions`.<br>`/v1/routing`, `/v1/health`, batch and Prometheus are open. | all |

Because both services share one port, `GET /v1/models` is always the **controller** registry. The gateway's
OpenAI alias list isn't served, and the console doesn't need it.

**Prometheus is heuristic.** It evaluates a lenient PromQL subset over a synthetic metric catalogue. The subset covers:

- selectors and matchers
- `sum`, `max`, `min`, `avg` and `count`, with `by` or `without`
- `rate`, `increase` and `*_over_time`
- `histogram_quantile`
- arithmetic, comparison and `or`/`and`/`unless`

The catalogue covers `gpusched_*`, `node_memory_*`, temperatures, `lif_*` (including histograms), `kube_*`,
`container_memory_working_set_bytes`, `ALERTS` and `up`. An unknown query returns an empty vector, never an error. Range
queries follow an hourly pattern (idle, inference, primary-workload video, inference + batch), so the workload
timeline has something to show. When a console query returns nothing here, check it against the real Prometheus
before trusting the fake.

## Browser tests (Playwright)

Start `run_dev.sh` first (fresh database), then run `npm run e2e` in `lif/apps/console`. The config never starts or stops servers.

| Item | Value |
|---|---|
| Specs | `setup` (first-run §83–84; saves the admin session), `responsive` (7 sizes × 2 schemes × 17 pages: no sideways scroll, prompt on screen, nav per breakpoint), `a11y` (axe WCAG 2.0/2.1 A+AA at 390 and 1440 in both schemes, keyboard, Ask history panel and sheet, reduced motion), `tasks` (§103–105 first-time-user tasks, phone pairing, outages, live Ask history across two browsers and after a dropped connection, vision in Ask) |
| Browser | The pinned `playwright` devDependency and its cached Chromium. One worker, because the fake world and its scenario are global. |
| Memory | `global-setup.ts` refuses to launch when MemAvailable is below 3 GiB |
| Output | `E2E_OUT` (default `<tmpdir>/labzilla-console-e2e`): session state, failure screenshots. Page screenshots go to `E2E_SHOTS` (default `$E2E_OUT/shots`). Nothing is written to the repo. |
| Rerun | The setup spec signs in instead when setup already happened. Restart `run_dev.sh` for a clean world: pairing is rate-limited to 10/min. |
