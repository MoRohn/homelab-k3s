"""Prometheus metric catalogue shared by every LIF service (scraped via ServiceMonitor).

Resolution tiers — the Heavy-Model-Avoidance KPI is computed from `lif_tasks_total`:
  deterministic  answered by code (cache hit, rule, validation)
  jev            answered by a Jev decision above its auto threshold
  fast_local     answered by a local model with params_b < heavy_params_b
  large_local    answered by a local model with params_b >= heavy_params_b
  external       answered by an external provider
  rejected       no tier could serve it (budget, privacy, capacity)
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HEAVY_PARAMS_B = 14.0          # "heavy model" = a local model at or above this size

LAT = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300)

tasks = Counter("lif_tasks_total", "AI-eligible tasks by resolution tier", ["workload", "tier"])

# ── gateway / inference ──
requests = Counter("lif_requests_total", "Gateway requests", ["endpoint", "alias", "status"])
request_latency = Histogram("lif_request_seconds", "End-to-end gateway latency", ["endpoint", "alias"],
                            buckets=LAT)
ttft = Histogram("lif_ttft_seconds", "Time to first token (streaming)", ["alias", "profile"], buckets=LAT)
tokens = Counter("lif_tokens_total", "Tokens processed", ["profile", "direction"])
decode_tps = Histogram("lif_decode_tokens_per_second", "Decode throughput per request", ["profile"],
                       buckets=(1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128))
fallbacks = Counter("lif_fallbacks_total", "Alias served by a non-primary profile",
                    ["alias", "served_by", "reason"])
inflight = Gauge("lif_inflight_requests", "Requests in flight", ["profile"])
throttled = Counter("lif_throttled_total", "Requests delayed or clamped by the primary-workload yield", ["reason"])

# ── availability probes (controller, every 15 s; 1 = usable, verified by real inference) ──
available = Gauge("lif_available", "Capability usable right now (inference-verified)", ["capability"])
probe_latency = Gauge("lif_probe_seconds", "Last synthetic probe latency", ["capability"])

# ── Primary workload / GPU ──
blerbz_state = Gauge("lif_blerbz_state", "Primary-workload capacity state (0 LOW,1 MODERATE,2 HIGH,3 IMMINENT)")
admissible = Gauge("lif_gpu_admissible_mib", "gpusched admissible GPU memory (MiB)")

# ── Decision Fabric / Jev ──
decisions = Counter("lif_decisions_total", "Decisions evaluated",
                    ["decision", "provider", "action"])
decision_latency = Histogram("lif_decision_seconds", "Decision latency", ["decision", "provider"], buckets=LAT)
decision_confidence = Histogram("lif_decision_confidence", "Decision confidence", ["decision", "provider"],
                                buckets=(0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.99, 1.0))
jev_requests = Counter("lif_jev_requests_total", "HTTP calls to Jev", ["status"])
jev_input_tokens = Counter("lif_jev_input_tokens_total", "Jev input tokens billed")
jev_cost = Counter("lif_jev_cost_usd_total", "Estimated Jev spend (USD)")
jev_cache = Counter("lif_decision_cache_total", "Decision cache lookups", ["result"])
jev_escalations = Counter("lif_decision_escalations_total", "Decisions escalated below auto threshold",
                          ["decision", "to"])
jev_breaker = Gauge("lif_jev_circuit_open", "1 while the Jev circuit breaker is open")
privacy_blocks = Counter("lif_privacy_blocks_total", "Outbound calls refused by privacy policy",
                         ["data_class", "destination"])

# ── models / registry ──
model_state = Gauge("lif_model_state", "1 for the registry state a model is in", ["model", "state"])
alias_target = Gauge("lif_alias_target", "1 for the profile an alias currently resolves to", ["alias", "profile"])

# ── batch ──
batch_items = Counter("lif_batch_items_total", "Batch items by outcome", ["outcome"])
batch_queue = Gauge("lif_batch_queue_depth", "Pending batch items", ["priority"])

# ── cost ──
cost_avoided = Counter("lif_external_cost_avoided_usd_total",
                       "ESTIMATE: API-equivalent cost of work served locally or by Jev", ["tier"])

# ── Decision Engineering (docs/DECISION_ENGINEERING.md) ──
# Funnel: every observed agent step lands in exactly one bucket.
agent_steps = Counter("lif_agent_steps_total", "Observed agent steps by computational bucket",
                      ["agent", "bucket"])          # code | jev_candidate | jev | generative | human | unknown
cascade = Counter("lif_cascade_resolutions_total", "Cascade decisions by the tier that resolved them",
                  ["decision", "tier"])              # code | jev | local_fast | local_reasoning | kimi | frontier | human | safe_default
escalations = Counter("lif_escalations_total", "Escalations out of the Jev tier", ["decision", "provider", "reason"])
escalation_latency = Histogram("lif_escalation_seconds", "Escalation provider latency", ["provider"], buckets=LAT)
escalation_tokens = Counter("lif_escalation_tokens_total", "Escalation tokens", ["provider", "direction"])
escalation_cost = Counter("lif_escalation_cost_usd_total", "Escalation spend (USD, from providers.yaml)",
                          ["provider"])
provider_breaker = Gauge("lif_provider_circuit_open", "1 while an escalation provider's breaker is open",
                         ["provider"])
provider_limited = Counter("lif_provider_rate_limited_total", "Calls delayed or refused by rate limits",
                           ["provider", "how"])      # waited | refused | upstream_429
fanout_size = Histogram("lif_jev_fanout_size", "Questions carried by one Jev request",
                        buckets=(1, 2, 3, 4, 6, 8, 12, 16, 32))
fanout_saved_tokens = Counter("lif_jev_fanout_saved_input_tokens_total",
                              "ESTIMATE: state tokens not re-sent thanks to shared-state fan-out")
shadow = Counter("lif_shadow_decisions_total", "Shadow evaluations", ["decision", "agreement"])
outcomes = Counter("lif_decision_outcomes_total", "Observed decision outcomes", ["decision", "route", "outcome"])
human_queue = Gauge("lif_human_review_queue", "Decisions waiting for a human")
heavy_avoided = Counter("lif_heavy_generation_avoided_total",
                        "Steps resolved by code/Jev/local-fast that the baseline sent to a heavy model",
                        ["decision"])
