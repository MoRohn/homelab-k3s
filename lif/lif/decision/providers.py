"""Decision providers.

JevProvider      hosted TypeSafe Jev (api.typesafe.ai/v1/systemone). Several decisions
                 over the SAME state go out as one request (Jev's `questions` map).
RulesProvider    deterministic functions registered per decision name; may abstain.
LocalLLMProvider the local fast model picks an option letter under a grammar; option
                 probabilities come from token logprobs. They are NOT calibrated, so the
                 confidence is capped below the auto-action threshold.
"""
from __future__ import annotations

import asyncio
import math
import string
import time
from typing import Any, Awaitable, Callable

import httpx

from lif.common import config, log, metrics
from lif.decision.types import DecisionDef, DecisionResult, normalize_state

LOG = log.get("lif.decision")


class ProviderUnavailable(Exception):
    """Provider cannot answer right now (outage, breaker open, privacy, abstain)."""


class CircuitBreaker:
    def __init__(self, failures: int = 5, reset_sec: float = 60, gauge=None):
        self.limit, self.reset_sec, self.gauge = failures, reset_sec, gauge
        self.fails, self.opened_at = 0, 0.0

    @property
    def open(self) -> bool:
        if self.opened_at and time.monotonic() - self.opened_at >= self.reset_sec:
            return False           # half-open: allow a trial call
        return bool(self.opened_at)

    def ok(self) -> None:
        self.fails, self.opened_at = 0, 0.0
        if self.gauge is not None:
            self.gauge.set(0)

    def fail(self) -> None:
        self.fails += 1
        if self.fails >= self.limit:
            self.opened_at = time.monotonic()
            if self.gauge is not None:
                self.gauge.set(1)


# ── Jev ──────────────────────────────────────────────────────────────────────

class JevProvider:
    name = "jev"

    def __init__(self, api_key: str | None, client: httpx.AsyncClient | None = None):
        c = config.get("decision_fabric.jev") or {}
        self.api_key = api_key
        self.base_url = c.get("base_url", "https://api.typesafe.ai").rstrip("/")
        self.model = c.get("model", "jev-1.13.0")
        self.timeout = float(c.get("timeout_sec", 4))
        self.price = float(c.get("price_usd_per_mtok_input", 0.42))
        self.sem = asyncio.Semaphore(int(c.get("max_concurrency", 16)))
        cb = c.get("circuit_breaker") or {}
        self.breaker = CircuitBreaker(cb.get("failures", 5), cb.get("reset_sec", 60), metrics.jev_breaker)
        self.client = client or httpx.AsyncClient(timeout=self.timeout)
        self.enabled = bool(api_key) and config.get("decision_fabric.provider") == "typesafe_jev"

    @property
    def version(self) -> str:
        return self.model

    def available(self) -> bool:
        return self.enabled and not self.breaker.open

    async def evaluate_group(self, defs: list[DecisionDef], state: Any) -> dict[str, DecisionResult]:
        if not self.available():
            raise ProviderUnavailable("jev disabled" if not self.enabled else "jev circuit open")
        keys = {_qkey(d.name): d for d in defs}
        body = {"model": self.model, "state": state if isinstance(state, (str, dict, list)) else normalize_state(state),
                "questions": {k: d.jev_question() for k, d in keys.items()}}
        t0 = time.perf_counter()
        async with self.sem:
            data = await self._post(body)
        ms = (time.perf_counter() - t0) * 1000
        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens", 0))
        cost = float(usage.get("cost_usd", in_tok * self.price / 1e6))
        metrics.jev_input_tokens.inc(in_tok)
        metrics.jev_cost.inc(cost)
        out: dict[str, DecisionResult] = {}
        answers = data.get("answers") or {}
        for k, d in keys.items():
            if k not in answers:
                raise ProviderUnavailable(f"jev response missing answer '{k}'")
            r = _parse_answer(d, answers[k])
            r.provider, r.latency_ms, r.provider_version = "jev", ms, data.get("model", self.model)
            r.cost_usd, r.input_tokens = cost / len(keys), in_tok // len(keys)
            out[d.name] = r
        return out

    async def _post(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(2):                       # one retry on 502/timeout
            try:
                resp = await self.client.post(f"{self.base_url}/v1/systemone", json=body, headers=headers,
                                              timeout=self.timeout)
            except httpx.HTTPError as exc:
                metrics.jev_requests.labels("transport_error").inc()
                last = exc
            else:
                metrics.jev_requests.labels(str(resp.status_code)).inc()
                if resp.status_code == 200:
                    self.breaker.ok()
                    return resp.json()
                if resp.status_code in (401, 402, 403):
                    # credentials/credits: not transient — stop calling until reset
                    self.breaker.fail(); self.breaker.opened_at = time.monotonic()
                    raise ProviderUnavailable(f"jev {resp.status_code}: {resp.text[:200]}")
                if resp.status_code == 400:
                    raise ValueError(f"jev rejected request: {resp.text[:300]}")
                last = ProviderUnavailable(f"jev {resp.status_code}")
            if attempt == 0:
                await asyncio.sleep(0.25)
        self.breaker.fail()
        raise ProviderUnavailable(f"jev unavailable: {last}")


def _qkey(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _parse_answer(d: DecisionDef, a: dict) -> DecisionResult:
    if d.type == "choice":
        probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items()}
        return DecisionResult(decision=a["choice"], confidence=float(a.get("confidence", probs.get(a["choice"], 0))),
                              probabilities=probs, provider="jev", decision_ref=d.ref)
    if d.type == "score":
        score = float(a["score"])
        raw = a.get("probabilities") or {}
        probs = {d.levels[int(k)]: float(v) for k, v in raw.items() if str(k).isdigit() and int(k) < len(d.levels)}
        label = d.levels[min(len(d.levels) - 1, max(0, round(score)))]
        return DecisionResult(decision=label, confidence=float(a.get("confidence", probs.get(label, 0))),
                              probabilities=probs, provider="jev", decision_ref=d.ref, score=score)
    p = float(a["noul"])
    return DecisionResult(decision="yes" if p >= 0.5 else "no", confidence=max(p, 1 - p),
                          probabilities={"yes": p, "no": 1 - p}, provider="jev", decision_ref=d.ref, score=p)


# ── Rules ────────────────────────────────────────────────────────────────────

RuleFn = Callable[[Any], "tuple[str, float] | None"]


class RulesProvider:
    """Deterministic rules. A rule returns (label, confidence) or None to abstain."""
    name = "rules"
    version = "rules-1"

    def __init__(self):
        self._rules: dict[str, RuleFn] = {}

    def register(self, decision_name: str):
        def wrap(fn: RuleFn) -> RuleFn:
            self._rules[decision_name] = fn
            return fn
        return wrap

    def has(self, decision_name: str) -> bool:
        return decision_name in self._rules

    async def evaluate(self, d: DecisionDef, state: Any) -> DecisionResult:
        fn = self._rules.get(d.name)
        if fn is None:
            raise ProviderUnavailable(f"no rule for {d.name}")
        t0 = time.perf_counter()
        out = fn(state)
        if out is None:
            raise ProviderUnavailable(f"rule for {d.name} abstained")
        label, conf = out
        if label not in d.labels:
            raise ValueError(f"rule for {d.name} returned unknown label {label!r}")
        return DecisionResult(decision=label, confidence=conf, probabilities={label: conf}, provider="rules",
                              decision_ref=d.ref, latency_ms=(time.perf_counter() - t0) * 1000,
                              provider_version=self.version)


# ── Local LLM ────────────────────────────────────────────────────────────────

class LocalLLMProvider:
    name = "local_llm"

    def __init__(self, base_url: str, alias: str = "local/instant", api_key: str | None = None,
                 client: httpx.AsyncClient | None = None, confidence_cap: float = 0.95, timeout: float = 30):
        self.base_url, self.alias, self.api_key = base_url.rstrip("/"), alias, api_key
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.cap = confidence_cap
        self.version = f"local:{alias}"

    async def evaluate(self, d: DecisionDef, state: Any) -> DecisionResult:
        labels = d.labels
        if len(labels) > 26:
            raise ProviderUnavailable("too many options for letter grammar")
        letters = string.ascii_uppercase[:len(labels)]
        if d.type == "noul":
            opts = {"A": ("yes", "yes, the proposition holds"), "B": ("no", "no, it does not hold")}
        elif d.type == "choice":
            opts = {L: (lab, d.choices[lab]) for L, lab in zip(letters, labels)}
        else:
            opts = {L: (lab, f"level {i} of {len(labels) - 1}") for i, (L, lab) in enumerate(zip(letters, labels))}
        prompt = (f"{d.instructions}\n\nInput:\n{normalize_state(state)[:12000]}\n\nOptions:\n"
                  + "\n".join(f"{L}) {lab}: {desc}" for L, (lab, desc) in opts.items())
                  + "\n\nAnswer with the single letter of the best option.")
        body = {"model": self.alias, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 20,
                "grammar": "root ::= " + " | ".join(f'"{L}"' for L in opts)}
        headers = {"X-LIF-Data-Class": "CONFIDENTIAL", "X-LIF-Workload": "decision-fabric"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        t0 = time.perf_counter()
        try:
            r = await self.client.post(f"{self.base_url}/v1/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"local llm: {exc}") from exc
        if r.status_code != 200:
            raise ProviderUnavailable(f"local llm {r.status_code}: {r.text[:200]}")
        data = r.json()
        choice = data["choices"][0]
        picked = (choice["message"].get("content") or "").strip()[:1].upper()
        probs_l: dict[str, float] = {}
        for item in ((choice.get("logprobs") or {}).get("content") or [{}])[0].get("top_logprobs", []) or []:
            tok = item.get("token", "").strip().upper()
            if tok in opts:
                probs_l[tok] = probs_l.get(tok, 0.0) + math.exp(item["logprob"])
        if picked not in opts:
            raise ProviderUnavailable(f"local llm returned {picked!r}")
        if not probs_l:
            probs_l = {picked: 0.75}          # no logprobs → conservative fixed confidence
        z = sum(probs_l.values())
        probs = {opts[L][0]: v / z for L, v in probs_l.items()}
        label = opts[picked][0]
        conf = min(self.cap, probs.get(label, 0.0))
        score = None
        if d.type == "score":
            score = sum(labels.index(lab) * p for lab, p in probs.items())
        elif d.type == "noul":
            score = probs.get("yes", 0.0)
        return DecisionResult(decision=label, confidence=conf, probabilities=probs, provider="local_llm",
                              decision_ref=d.ref, score=score, latency_ms=(time.perf_counter() - t0) * 1000,
                              provider_version=data.get("model", self.version))


Evaluator = Callable[[DecisionDef, Any], Awaitable[DecisionResult]]
