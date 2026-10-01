"""Model router: logical alias → a healthy physical profile, with explicit fallback.

Inputs: the routing table (controller registry, else config/models.yaml), per-profile
health (active probes + passive failures), primary-workload state, and — for `local/auto` — a
`request-route` decision from the Decision Fabric (rules for private prompts, Jev only
when the caller declared the prompt PUBLIC).
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

from lif.common import config, log

LOG = log.get("lif.router")

AUTO_ALIAS = "local/auto"
VISION_ALIAS = "local/vision"
ROUTE_TO_ALIAS = {"instant": "local/instant", "fast": "local/fast", "default": "local/default",
                  "reasoning": "local/reasoning", "code": "local/code"}


@dataclass
class Profile:
    name: str
    endpoint: str
    category: str
    params_b: float
    device: str = "cpu"
    context: int = 8192
    concurrency: int = 4
    hf_repo: str = ""
    revision: str = ""
    chat_template_kwargs: dict = field(default_factory=dict)
    embedding_dim: int | None = None
    auth_secret: str | None = None        # secret name sent as Bearer to this upstream
    yield_on_blerbz: bool = False         # shared primary-workload engine: skipped while it is IMMINENT
    vision: bool = False                  # accepts image_url parts (llama.cpp loaded with --mmproj)
    runtime: str = "llama.cpp"            # llama.cpp-only request fields (return_progress) go nowhere else

    @classmethod
    def from_cfg(cls, name: str, c: dict) -> "Profile":
        return cls(name=name, endpoint=c["endpoint"].rstrip("/"), category=c.get("category", "general"),
                   params_b=float(c.get("params_b", 0)), device=c.get("device", "cpu"),
                   context=int(c.get("context", 8192)), concurrency=int(c.get("concurrency", 4)),
                   hf_repo=c.get("hf_repo", ""), revision=c.get("revision", ""),
                   chat_template_kwargs=c.get("chat_template_kwargs") or {},
                   embedding_dim=c.get("embedding_dim"), auth_secret=c.get("auth_secret"),
                   yield_on_blerbz=bool(c.get("yield_on_blerbz", False)),
                   vision=bool(c.get("mmproj") or c.get("vision")), runtime=c.get("runtime") or "llama.cpp")


@dataclass
class Health:
    ok: bool = False
    checked: float = 0.0
    fails: int = 0
    last_error: str = ""
    down_until: float = 0.0          # passive breaker after request failures


@dataclass
class Route:
    alias: str
    requested: str
    profile: Profile
    fallback: bool
    reason: str
    degraded: bool
    candidates_tried: list[str]
    route_decision: dict | None = None
    canary: bool = False

    def meta(self) -> dict:
        m = {"requested": self.requested, "alias": self.alias, "served_by": self.profile.name,
             "model": f"{self.profile.hf_repo}@{self.profile.revision[:12]}" if self.profile.hf_repo else self.profile.name,
             "fallback": self.fallback, "degraded": self.degraded}
        if self.reason:
            m["reason"] = self.reason
        if self.route_decision:
            m["route_decision"] = self.route_decision
        if self.canary:
            m["canary"] = True
        return m


class NoRoute(Exception):
    def __init__(self, alias: str, reason: str):
        super().__init__(reason)
        self.alias, self.reason = alias, reason


class NotSupported(Exception):
    """The request needs a capability (images) that no model behind this alias has. A 422 for
    the caller, never an upstream error or a silent swap to another alias."""
    def __init__(self, alias: str, reason: str):
        super().__init__(reason)
        self.alias, self.reason = alias, reason


class Router:
    def __init__(self, controller_url: str | None = None, client: httpx.AsyncClient | None = None):
        self.controller_url = controller_url
        self.client = client or httpx.AsyncClient(timeout=3)
        self.profiles: dict[str, Profile] = {}
        self.aliases: dict[str, list[str]] = {}
        self.min_params: dict[str, float] = {}
        self.canaries: dict[str, dict] = {}       # alias → {"profile": name, "percent": 10}
        self.jev_enabled: bool | None = None      # operator kill switch, from the controller
        self.table_source = "none"
        self.health: dict[str, Health] = {}
        self.load_static()

    # ── routing table ────────────────────────────────────────────────────────

    def load_static(self) -> None:
        cat = config.catalogue()
        self._apply(cat.get("profiles") or {}, cat.get("aliases") or {}, cat.get("alias_min_params_b") or {},
                    "config")

    def _apply(self, profiles: dict, aliases: dict, min_params: dict, source: str,
               canaries: dict | None = None) -> None:
        self.profiles = {n: Profile.from_cfg(n, c) for n, c in profiles.items() if c.get("endpoint")}
        self.aliases = {a: [p for p in ps if p in self.profiles] for a, ps in aliases.items()}
        self.canaries = {a: c for a, c in (canaries or {}).items() if c and c.get("profile") in self.profiles}
        self.min_params = {a: float(v) for a, v in min_params.items()}
        self.table_source = source
        for n in self.profiles:
            self.health.setdefault(n, Health())

    async def refresh_table(self) -> None:
        """Pull the live alias table from the controller. On any failure keep the
        last-known-good table — a control-plane outage must not affect inference."""
        if not self.controller_url:
            return
        try:
            r = await self.client.get(f"{self.controller_url}/v1/routing")
            r.raise_for_status()
            t = r.json()
            if "jev_enabled" in t:
                self.jev_enabled = bool(t["jev_enabled"])
            if t.get("profiles") and t.get("aliases"):
                self._apply(t["profiles"], t["aliases"], t.get("alias_min_params_b") or {}, "controller",
                            t.get("canaries"))
        except Exception as exc:
            LOG.warning("routing table refresh failed; keeping last-known-good",
                        extra={"fields": {"err": str(exc)[:160], "source": self.table_source}})

    # ── health ───────────────────────────────────────────────────────────────

    async def probe(self, name: str) -> None:
        p, h = self.profiles[name], self.health[name]
        try:
            r = await self.client.get(f"{p.endpoint}/health", timeout=3)
            h.ok = r.status_code == 200
            h.last_error = "" if h.ok else f"health {r.status_code}"
        except Exception as exc:
            h.ok, h.last_error = False, str(exc)[:160]
        h.checked = time.time()

    async def probe_all(self) -> None:
        await asyncio.gather(*(self.probe(n) for n in self.profiles))

    def healthy(self, name: str) -> bool:
        h = self.health.get(name)
        return bool(h and h.ok and time.time() >= h.down_until)

    def mark_failure(self, name: str, err: str) -> None:
        h = self.health[name]
        h.fails += 1
        h.last_error = err[:160]
        if h.fails >= 2:
            h.down_until = time.time() + min(60, 5 * h.fails)

    def mark_success(self, name: str) -> None:
        h = self.health[name]
        h.fails, h.down_until = 0, 0.0

    # ── resolve ──────────────────────────────────────────────────────────────

    def resolve(self, alias: str, requested: str | None = None, exclude: set[str] | None = None,
                route_decision: dict | None = None, blerbz_imminent: bool = False,
                need_vision: bool = False) -> Route:
        """`need_vision`: the request carries images, so only profiles loaded with an image
        projector qualify. An unhealthy vision model never falls through to a text one."""
        requested = requested or alias
        yielded = {n for n, p in self.profiles.items() if p.yield_on_blerbz} if blerbz_imminent else set()
        if alias not in self.aliases:
            raise NoRoute(alias, f"unknown model alias '{alias}'. Use GET /v1/models")
        chain = self.aliases[alias]
        if need_vision:
            text_only = [n for n in chain if not self.profiles[n].vision]
            chain = [n for n in chain if self.profiles[n].vision]
            if not chain and text_only:
                raise NotSupported(alias, f"{alias} cannot read images: its models have no image projector. "
                                          f"Send images to {VISION_ALIAS} (or local/auto)")
        can = self.canaries.get(alias)
        if can and need_vision and not self.profiles[can["profile"]].vision:
            can = None
        if can and not exclude and can["profile"] not in yielded and self.healthy(can["profile"]) and random.random() * 100 < float(can.get("percent", 0)):
            p = self.profiles[can["profile"]]
            return Route(alias=alias, requested=requested, profile=p, fallback=False,
                         reason=f"canary ({can.get('percent')}% of {alias})",
                         degraded=p.params_b < self.min_params.get(alias, 0), candidates_tried=[],
                         route_decision=route_decision, canary=True)
        if not chain:
            if alias == VISION_ALIAS:
                raise NoRoute(alias, f"no local model is deployed for {alias}: no vision model is installed yet. "
                                     "Use Models → Check for better models (vision) to find one")
            raise NoRoute(alias, f"no local model is deployed for {alias} (capacity; see /v1/capabilities)")
        tried = []
        for i, name in enumerate(chain):
            if (exclude and name in exclude) or name in yielded:
                tried.append(name)
                continue
            if not self.healthy(name):
                tried.append(name)
                continue
            p = self.profiles[name]
            reason = ""
            if i > 0:
                why = ["GPU engine reserved for primary-workload production" if t in yielded
                       else (self.health[t].last_error or "unhealthy") for t in chain[:i]]
                reason = f"primary unavailable: {'; '.join(why)[:200]}"
            degraded = p.params_b < self.min_params.get(alias, 0)
            if degraded and not reason:
                reason = (f"{alias} expects >= {self.min_params[alias]:g}B; largest resident model is "
                          f"{p.params_b:g}B (GPU reserved for the primary workload)")
            return Route(alias=alias, requested=requested, profile=p, fallback=i > 0 or requested != alias,
                         reason=reason, degraded=degraded, candidates_tried=tried, route_decision=route_decision)
        raise NoRoute(alias, f"all models for {alias} are unavailable: " +
                      "; ".join(f"{n}: {self.health[n].last_error or 'unhealthy'}" for n in chain))

    def alias_status(self, blerbz_imminent: bool = False) -> dict[str, dict]:
        out = {}
        for a, chain in self.aliases.items():
            try:
                r = self.resolve(a, blerbz_imminent=blerbz_imminent)
                out[a] = {"available": True, "served_by": r.profile.name, "fallback": r.fallback,
                          "degraded": r.degraded, "reason": r.reason,
                          "vision": any(self.profiles[n].vision for n in chain)}
            except NoRoute as e:
                out[a] = {"available": False, "reason": e.reason}
        out[AUTO_ALIAS] = {"available": any(v["available"] for v in out.values()),
                           "served_by": "decision: request-route", "fallback": False, "degraded": False,
                           "reason": ""}
        return out
