"""Agent SDK (spec §79–§80): two primitives, chosen consciously.

    decision = await intelligence.decide("source-relevance", state)       # bounded judgment
    text     = await intelligence.generate(messages, alias="local/default")  # creation

decide() → state compiler → cascade (code → Jev → local → Kimi → human/safe default).
The caller sees answer, confidence, executor, decision_version, escalated, latency; it
does not need to know which tier answered. Act only when `decision.actionable`.

generate() is local-first through the LIF gateway. External generation (Kimi or another
approved model) happens only when the caller allows it AND privacy policy permits the
data class; otherwise it stays local or fails loudly.

RemoteIntelligence is the same API over HTTP to the decision-fabric service, for agents
that run outside it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from lif.common import config, log
from lif.decision import fanout, provenance
from lif.decision.cascade import Baseline, Cascade, CascadeResult, Validator
from lif.decision.escalation import OpenAIChatProvider
from lif.decision.state_compiler import MissingState, compile_state, from_knowledge
from lif.decision.store import Store
from lif.policy import engine as policy

LOG = log.get("lif.decision.sdk")


@dataclass
class Decision:
    answer: str
    confidence: float
    executor: str
    decision_version: str
    escalated: bool
    latency_ms: float
    actionable: bool
    route: str
    provenance_id: str = ""
    probabilities: dict[str, float] = field(default_factory=dict)
    pending_ticket: int | None = None
    state_tokens: int = 0
    cost_usd: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, r: CascadeResult, state_tokens: int = 0, explain: bool = False) -> "Decision":
        return cls(answer=r.answer, confidence=r.confidence, executor=r.executor, decision_version=r.decision_version,
                   escalated=r.escalated, latency_ms=round(r.latency_ms, 2), actionable=r.actionable, route=r.route,
                   provenance_id=r.provenance_id, probabilities=r.probabilities, pending_ticket=r.pending_ticket,
                   state_tokens=state_tokens, cost_usd=r.cost_usd,
                   detail={"chain": r.chain, "zone": r.zone, "degraded": r.degraded} if explain else {})


@dataclass
class Generation:
    text: str
    executor: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    degraded: bool = False
    fallback: bool = False


class Intelligence:
    def __init__(self, cascade: Cascade, gateway_url: str | None = None, gateway_key: str | None = None,
                 external: dict[str, OpenAIChatProvider] | None = None, store: Store | None = None,
                 client: httpx.AsyncClient | None = None):
        self.cascade = cascade
        self.gateway = (gateway_url or "").rstrip("/")
        self.gateway_key = gateway_key
        self.external = external or {}
        self.store = store
        self.client = client or httpx.AsyncClient(timeout=300)

    # ── decide ───────────────────────────────────────────────────────────────

    async def decide(self, name: str, state: dict[str, Any], *, agent: str = "", workflow: str = "",
                     data_class: str | None = None, baseline: Baseline | None = None, code: Baseline | None = None,
                     validator: Validator | None = None, choices: list[str] | dict[str, str] | None = None,
                     compile: bool = True, untrusted_paths: list[str] | None = None,
                     explain: bool = False) -> Decision:
        d = self.cascade._serving(name) or self.cascade.fabric.defs.get(name)
        st, n_tok = state, 0
        if compile and d is not None:
            try:
                c = compile_state(d, state, untrusted_paths=untrusted_paths)
                st, n_tok = c.state, c.tokens_after
            except MissingState as e:
                # incomplete evidence: never guess. Safe default, not actionable (§74).
                LOG.warning("state compiler: missing evidence", extra={"fields": {"decision": name, "err": str(e)}})
                return Decision(answer=d.default or d.labels[0], confidence=0.0, executor="state-compiler",
                                decision_version=d.ref, escalated=False, latency_ms=0.0, actionable=False,
                                route="safe_default", detail={"error": str(e)})
        r = await self.cascade.decide(name, st, data_class=data_class, agent=agent, workflow=workflow,
                                      baseline=baseline, code=code, validator=validator, choices=choices)
        return Decision.of(r, n_tok, explain)

    async def decide_many(self, requests: list[fanout.DecisionRequest], *, agent: str = "", workflow: str = "",
                          explain: bool = False) -> tuple[dict[str, Decision], dict]:
        """Several decisions; independent ones sharing state go out as one Jev request."""
        async def ev(names, state, data_class):
            got = await self.cascade.decide_group(names, state, data_class=data_class, agent=agent,
                                                  workflow=workflow)
            return {n: Decision.of(r, explain=explain) for n, r in got.items()}
        return await fanout.run(ev, requests)

    def context(self, task: str, budget_tokens: int = 800) -> list[dict]:
        """Relevant knowledge objects for a task (Knowledge Work layer), to put in state."""
        return from_knowledge(task, budget_tokens)

    def outcome(self, decision: Decision, correct_answer: str, source: str = "observed") -> dict:
        """Report what the right answer was (§52). Feeds calibration."""
        from lif.decision.shadow import record_outcome
        if self.store is None or not decision.provenance_id:
            return {"provenance": 0, "shadow": 0}
        return record_outcome(self.store, outcome=correct_answer, provenance_id=decision.provenance_id, source=source)

    # ── generate ─────────────────────────────────────────────────────────────

    async def generate(self, messages: list[dict] | str, *, alias: str = "local/default", max_tokens: int = 512,
                       data_class: str = "CONFIDENTIAL", workload: str = "agent", allow_external: bool = False,
                       external: str = "kimi-k3", temperature: float = 0.2) -> Generation:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        t0 = time.perf_counter()
        local_err = ""
        if self.gateway:
            headers = {"X-LIF-Data-Class": data_class, "X-LIF-Workload": workload}
            if self.gateway_key:
                headers["Authorization"] = f"Bearer {self.gateway_key}"
            try:
                r = await self.client.post(f"{self.gateway}/v1/chat/completions", headers=headers,
                                           json={"model": alias, "messages": messages, "max_tokens": max_tokens,
                                                 "temperature": temperature})
                if r.status_code == 200:
                    data = r.json()
                    lif = data.get("lif") or {}
                    u = data.get("usage") or {}
                    return Generation(text=data["choices"][0]["message"].get("content") or "", executor="local",
                                      model=lif.get("served_by", alias), input_tokens=int(u.get("prompt_tokens", 0)),
                                      output_tokens=int(u.get("completion_tokens", 0)),
                                      latency_ms=(time.perf_counter() - t0) * 1000,
                                      degraded=bool(lif.get("degraded")), fallback=bool(lif.get("fallback")))
                local_err = f"gateway {r.status_code}"
            except httpx.HTTPError as e:
                local_err = type(e).__name__
        prov = self.external.get(external)
        cls = policy.classify(str(messages), data_class).data_class
        if allow_external and prov is not None and prov.enabled and policy.may_send(cls, "external_llm"):
            body = {"model": prov.cfg["model"], "messages": messages, "max_tokens": max_tokens}
            if (prov.cfg.get("request") or {}).get("send_sampling_params", True):
                body["temperature"] = temperature
            await prov.limiter.acquire()
            r = await prov.client.post(f"{prov.cfg['base_url'].rstrip('/')}/chat/completions", json=body,
                                       headers={"Authorization": f"Bearer {prov.api_key}"})
            if r.status_code == 200:
                data = r.json()
                u = data.get("usage") or {}
                return Generation(text=data["choices"][0]["message"].get("content") or "", executor=prov.name,
                                  model=data.get("model", ""), input_tokens=int(u.get("prompt_tokens", 0)),
                                  output_tokens=int(u.get("completion_tokens", 0)),
                                  latency_ms=(time.perf_counter() - t0) * 1000, fallback=True)
            local_err += f"; {prov.name} {r.status_code}"
        raise RuntimeError(f"generation unavailable: {local_err or 'no gateway configured'}"
                           + ("" if allow_external else " (external generation not allowed by caller)"))


class RemoteIntelligence:
    """decide() against the decision-fabric service (`POST /de/decide`)."""

    def __init__(self, url: str | None = None, client: httpx.AsyncClient | None = None):
        self.url = (url or config.get("decision_engineering.service_url", "")
                    or "http://decision-fabric.ai-system.svc:8080").rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=180)

    async def decide(self, name: str, state: dict, **kw) -> Decision:
        r = await self.client.post(f"{self.url}/de/decide", json={"decision": name, "state": state, **kw})
        r.raise_for_status()
        d = r.json()
        return Decision(**{k: d[k] for k in Decision.__dataclass_fields__ if k in d})


__all__ = ["Intelligence", "RemoteIntelligence", "Decision", "Generation", "provenance"]
