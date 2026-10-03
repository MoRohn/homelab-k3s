# Live information (web grounding)

Local models were trained months ago. On their own, they answer "Who won last night?" with "As of my last update…". The gateway prevents this: it detects questions that need current information, looks them up, and has a local model answer from the results, citing its sources. Code: `lif/web/`. Gateway glue: `lif/gateway/app.py` (`_web_plan`, `_ground`, `_stream`).

## Pipeline

```
chat request ─ web mode? ─ off ──────────────────────────────────────────────▶ model (unchanged)
                  │ auto / required
                  ▼
     needs-live-data decision (rules; Jev only for PUBLIC, grouped with request-route)
        │ no                                         │ yes
        ▼                                            ▼
  date in system message                build query (dates in the user's timezone)
  + disclaimer guard on the answer       ├─ exact feeds: ESPN (sports), Open-Meteo (weather)
                                         └─ SearXNG (self-hosted meta-search)
                                              → rank → enough? ─ no → read top pages (SSRF-safe)
                                         evidence block (≤ 2,400 chars) on the last user turn
                                              ▼
                                local/web (falls back to local/default), never local/instant
```

| Step | Where | What it does |
|---|---|---|
| Web mode | `_web_plan` | `local/auto` defaults to `auto`. Other aliases are `off` unless the request sets `"lif": {"web": "auto" \| "required"}` or `X-LIF-Web`. The console sends `auto` for every text mode |
| Decision | `needs-live-data` (`config/decisions/core.yaml`) | Rules (`lif/web/freshness.py`) for private prompts. For PUBLIC prompts, one Jev request answers `needs-live-data` and `request-route` together. A confident rules "yes" (≥ 0.8) is never overruled |
| Query | `freshness.build_query` | Strips filler and resolves "last night", "on Sunday", "this weekend" to dates in the caller's timezone. A short follow-up ("what about tomorrow?") is rewritten into a standalone query by `local/instant` (3 s budget; skipped during IMMINENT) |
| Feeds | `sports.py`, `weather.py` | ESPN site API: final scores, live status and the next game for named teams; pro standings and college polls (the playoff ranking once published, else AP) for "best team", "standings", "ranked" questions. Open-Meteo: current conditions and the daily forecast. Neither needs a key |
| Search | `search.py` | SearXNG JSON API. Ranking uses BM25-style term overlap, engine agreement, recency and source quality, with at most 2 results per domain |
| Read pages | `fetch.py` | Only when the snippets don't hold the answer: top 3 pages, 2 s, ~70-word passages. Blocks private/cluster addresses (re-checked on every redirect), non-80/443 ports and non-HTML content |
| Answer | `ground.py` | System message: today's date only (byte-identical all day, so the prompt cache holds). Last user turn: current time, `<web_results>`, the grounding rules, then the question |
| Guard | `_stream`, `_regenerate` | For questions that weren't looked up, the gateway holds the first sentence. If it is a knowledge-cutoff disclaimer, the gateway discards it, looks the question up, and answers again |

## Privacy

| Data | Leaves the box? |
|---|---|
| The conversation, files, images | Never |
| The built search query (≤ 200 chars) | Yes, to SearXNG → public engines; shown in the console as "Looked up “…” · only this query left Labzilla" |
| Team ids, league, date | Yes, to `site.api.espn.com` |
| Place name, coordinates | Yes, to `open-meteo.com` |
| A query containing a key, email, phone number, card-like number | Never: the lookup is `blocked` |
| A RESTRICTED prompt (detectors or the caller's label) | Never looked up (`privacy.web_search_allowed`) |
| A personal question with no public topic ("my appointment tomorrow") | Never looked up (classifier) |

Owner decision 2026-10-03: lookups are allowed under "Local only", because only the query is sent. The knowledge decision is `web-search-query-only`.

## Response metadata

Non-streaming responses carry `lif.web`. Streaming responses carry `X-LIF-Web`, `X-LIF-Web-Sources` and `X-LIF-Web-Query` (URL-encoded), and `lif.web` in the final usage chunk.

| Field | Meaning |
|---|---|
| `status` | `ok`, `empty`, `error`, `blocked`, or `not_needed` (no lookup) |
| `query` | Exactly what was sent to search |
| `sources` | `[{n, title, url}]`: the `[n]` the answer cites |
| `providers` | e.g. `["espn", "searxng"]` |
| `decision` | `{decision, confidence, provider, action}`. `provider: guard` means the answer was redone after a disclaimer |
| `ms`, `timings` | Total lookup time, and time per source |

Grounded answers are never served from the deterministic (temperature 0) cache.

## Configuration (`lif.yaml` → `web`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch |
| `searxng_url` | `http://searxng.ai-system.svc:8080` | Or `LIF_SEARXNG_URL` |
| `default_for_auto` | `true` | `local/auto` looks things up when the decision says so |
| `default_timezone` / `default_location` | `UTC` / `""` | Used when the caller sends no `X-LIF-Timezone` or names no place |
| `alias` | `local/web` | Model for grounded answers. Empty → `local/default` |
| `deadline_sec` | 4.0 | All lookups for one question, including page reads |
| `max_evidence_chars` | 2400 | ~600 prompt tokens, ~4 s of prompt reading for the 4B on the CPU |
| `disclaimer_guard`, `disclaimer_window_chars` | `true`, 220 | Hold the opening until a sentence ends (≥ 30 chars) or this many chars |

## Deployment

| Piece | Manifest |
|---|---|
| SearXNG (pinned digest, 192–512 Mi, gateway-only NetworkPolicy) | `deploy/k8s/base/17-searxng.yaml` |
| Its secret_key | `scripts/create-secrets.sh` → Secret `ai-system/searxng` |
| `local/web` chain | Promote a model to it (`local-ai models promote <id> --alias local/web`). Until then `local/default` answers |

Remove SearXNG: `kubectl delete -f deploy/k8s/base/17-searxng.yaml`. With it gone, lookups report `error` and the model says it couldn't confirm, instead of guessing.

## Metrics

| Metric | Labels |
|---|---|
| `lif_web_lookups_total` | `kind`, `status` |
| `lif_web_lookup_seconds` | `source` (`total`, `espn`, `open-meteo`, `search`, `read`, `rewrite`) |
| `lif_web_disclaimer_guard_total` | `outcome` (`caught`, `regenerated`, `failed`) |

## Measured

Grounded-QA benchmark: `benchmarks/web-grounding/` (19 questions, facts checked 2026-10-03; results in `results-*.json`).

| What (2026-10-03, production state HIGH, all residents loaded) | n | Result |
|---|---|---|
| Lookup time (feeds + SearXNG), dry run before the classifier fixes | 13 | 0.34–1.52 s |
| Lookups whose evidence held the expected fact | 13 | 13 |
| Screenshot question end to end through the gateway (4B, streaming) | 1 | 6.1 s total, 0.49 s lookup, correct with 2 cited sources |

Model accuracy and latency per arm come from the benchmark run, which waits until the primary workload is LOW or MODERATE.
