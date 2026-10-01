# Model routing

## Routing table

The routing table is the controller's `GET /v1/routing`.

- The gateway refreshes it every 15 s.
- On any failure, the gateway keeps the **last-known-good** table. On first start without the controller, it uses the seed `config/models.yaml`.
- The table contains:
  - `profiles`: endpoint, params_b, category, context, concurrency, `chat_template_kwargs`.
  - `aliases`: alias → ordered chain.
  - `canaries`: alias → `{profile, percent}`.
  - `alias_min_params_b`.

## Resolution (`Router.resolve`)

1. Unknown alias → `404`-style error ("Use GET /v1/models").
2. **Canary.** If the alias has a healthy canary, it is chosen with probability `percent`. The metadata shows `canary: true`, and an unhealthy canary is never served.
3. Otherwise the first **healthy** profile in the chain is used.
   - Health means an active `/health` probe every 10 s, plus a passive breaker: 2 failures mark the profile down for 5–60 s.
4. Using a non-first profile sets `fallback: true` with a reason.
5. `degraded: true` means the profile is below the alias's floor. Floors: `local/default` 4B, `local/code` 14B, `local/reasoning` 30B, `local/vision` 1.5B.
6. An empty or entirely unhealthy chain returns **503 with the reason**. There is never a silent substitution outside the chain. For an empty `local/vision`, the reason starts `no local model is deployed for local/vision` and adds "no vision model is installed yet".
7. **Images.** If any message has an `image_url` part, only profiles with an image projector (`mmproj` in the profile, so `vision: true`) are eligible. This applies to canaries and upstream-error retries too. An unhealthy vision model is never replaced by a text model.

| Request | Result |
|---|---|
| `local/auto` + image | `local/vision`, deterministically. No `request-route` decision, so the prompt never goes to Jev. `route_decision: {decision: "vision", provider: "rules"}` |
| Text alias (e.g. `local/default`) + image | **422** `{"error": {"type": "not_supported", "code": "images_not_supported", "lif": {"use": "local/vision"}}}`. Nothing reaches an upstream |
| `local/vision` + image, no model installed | **503** `capacity`: "no vision model is installed yet" |
| Image request with a latency budget during IMMINENT | Never rerouted to `local/instant` |

During a request, an upstream 5xx or connect error excludes that profile and retries the next one. The final response carries `reason: "upstream_error on <profile>: ..."`.

## `local/auto` (intelligence hierarchy at the gateway)

```
request → request-route decision (rules for private prompts; Jev only if X-LIF-Data-Class: PUBLIC)
   gate auto/validate → instant | fast | default | reasoning | code alias
   otherwise          → local/default
request with an image part → local/vision (code, before any decision)
```

The response's `lif.route_decision` shows `{decision, confidence, provider, action}`.

## Other request controls

| Control | Effect |
|---|---|
| `temperature: 0`, non-streaming | Deterministic cache (2,000 entries, 1 h). A hit is counted as the `deterministic` tier |
| Primary workload IMMINENT | Concurrency 1 per CPU profile **cluster-wide** (each gateway replica checks the model server's `requests_processing` before dispatch); `max_tokens` capped at 512 |
| `"lif": {"budget": {"max_latency_ms": N}}` during IMMINENT | Prefers `local/instant` |
| Per-profile concurrency | `min(profile.concurrency, yield.cpu_concurrency_normal=4)`. Extra requests queue for up to 120 s, then 429 |

`Budget.max_external_cost` and `external_allowed` are parsed, but they have no effect. **No external provider is configured.** `privacy.external_llm_allowed: []`, and private data is never sent out because local capacity is busy.

## Current aliases

See the README table. `local/rerank` has an empty chain. `local/vision` stays empty until a vision model from discovery is downloaded, benchmarked and promoted. `/v1/capabilities` shows `vision` per profile, and `/v1/models` shows `vision` per available alias.
