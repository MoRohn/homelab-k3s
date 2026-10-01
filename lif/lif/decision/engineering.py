"""Decision Engineering runtime: one builder for the decision-fabric service and the CLI's
offline mode, so both run the same registry → fabric → cascade → SDK wiring.

Also the read models behind the Control Center: overview (executive dashboard), funnel,
cascade edges, evidence for promotions. Every number is computed from stored records;
when there is no data the field is None, never an illustrative value (spec §98).
"""
from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lif.common import config, log
from lif.decision import calibration, experiments, provenance, shadow
from lif.decision.cascade import Cascade
from lif.decision.escalation import (FrontierProvider, HumanReviewProvider, KimiK3Provider, LocalReasoningProvider)
from lif.decision.fabric import DecisionFabric, DecisionLog
from lif.decision.lint import lint, summarize
from lif.decision.providers import JevProvider, LocalLLMProvider, RulesProvider
from lif.decision.registry import Registry
from lif.decision.sdk import Intelligence
from lif.decision.store import Store
from lif.decision.types import DecisionDef, load_definitions, package_files, packages_root

LOG = log.get("lif.decision.engineering")


def escalation_providers(gateway_url: str, gateway_key: str | None, store: Store | None) -> dict:
    out: dict[str, Any] = {
        "local_reasoning": LocalReasoningProvider(gateway_url, "local-reasoning", api_key=gateway_key),
        "local_fast": LocalReasoningProvider(gateway_url, "local-fast", api_key=gateway_key),
        "kimi": KimiK3Provider(),
        "frontier": FrontierProvider(),
    }
    if store is not None:
        out["human"] = HumanReviewProvider(store)
    return out


@dataclass
class Runtime:
    store: Store
    registry: Registry
    fabric: DecisionFabric
    cascade: Cascade
    intel: Intelligence

    def refresh(self, name: str | None = None) -> None:
        """Re-resolve serving versions after a promotion/rollback; drop stale cache entries."""
        self.fabric.defs.update(self.registry.effective())
        if self.fabric.cache is not None:
            self.fabric.cache.invalidate(name)

    @classmethod
    def build(cls, *, store: Store | None = None, rules: RulesProvider | None = None, jev_key: str | None = None,
              gateway_url: str | None = None, gateway_key: str | None = None,
              decision_log: DecisionLog | None = None, definitions: dict | None = None) -> "Runtime":
        from lif.decision.rules import rules as core_rules
        store = store or Store()
        defs = definitions if definitions is not None else load_definitions()
        reg = Registry(defs, store)
        gw = gateway_url or os.environ.get("LIF_GATEWAY_URL", "http://gateway.ai-system.svc:8080")
        fab = DecisionFabric(reg.effective(), rules or core_rules,
                             jev=JevProvider(jev_key if jev_key is not None else config.secret("TYPE_SAFE_JEV_API_KEY")),
                             local_llm=LocalLLMProvider(gw, "local/instant", api_key=gateway_key),
                             decision_log=decision_log)
        esc = escalation_providers(gw, gateway_key, store)
        cas = Cascade(fab, reg, esc, store)
        intel = Intelligence(cas, gw, gateway_key, external={"kimi-k3": esc["kimi"]}, store=store)
        provenance.set_sink(provenance.default_sink())
        return cls(store=store, registry=reg, fabric=fab, cascade=cas, intel=intel)

    # ── evidence for promotion gates ─────────────────────────────────────────

    def package_dir(self, d: DecisionDef) -> Path | None:
        for f in package_files():
            if f.parent.name == d.name and f.stem == d.version:
                return f.parent
        priv = Path(os.environ.get("LIF_PRIVATE_DECISIONS", "/data/decisions")) / d.name
        return priv if priv.exists() else None

    def latest_test(self, ref: str) -> dict | None:
        rows = self.store.q("SELECT report FROM experiments WHERE name='decision-test' AND candidate=? "
                            "ORDER BY id DESC LIMIT 1", (ref,))
        return json.loads(rows[0]["report"]) if rows else None

    def calibration_file(self, ref: str) -> Path:
        base = Path(os.environ.get("LIF_CALIBRATION_DIR", "/data/calibration"))
        return base / f"{ref.replace('/', '@')}.json"

    def latest_calibration(self, ref: str) -> dict | None:
        f = self.calibration_file(ref)
        return json.loads(f.read_text()) if f.exists() else None

    def evidence(self, ref: str, policy: dict | None = None) -> dict:
        d = self.registry.get(ref)
        pd = self.package_dir(d)
        fs = lint(d, pd)
        test = self.latest_test(ref)
        cal = self.latest_calibration(ref)
        prev = [v for v in self.registry.versions(d.name) if v.version != d.version]
        reg_ok = None
        if test and prev:
            old = self.latest_test(prev[-1].ref)
            if old:
                reg_ok = experiments.compare(old, test)["regression_ok"]
        rec_high = (cal or {}).get("recommended", {}).get("high")
        want_high = (policy or {}).get("high")
        return {"lint_errors": summarize(fs)["error"], "lint": summarize(fs),
                "tests_total": (test or {}).get("n", 0), "tests_accuracy": (test or {}).get("accuracy"),
                "regression_ok": reg_ok,
                "calibration_adequate": bool((cal or {}).get("adequacy", {}).get("adequate")),
                "calibration_recommended_high": rec_high,
                "thresholds_from_calibration": rec_high is not None and want_high is not None
                and float(want_high) >= float(rec_high) - 1e-9}

    def calibrate(self, ref: str, use_agreement: bool = False, save: bool = True) -> dict:
        d = self.registry.get(ref)
        samples = shadow.samples(self.store, ref)
        rep = calibration.report(ref, samples, risk=d.risk, use_agreement=use_agreement)
        rep["generated_at"] = time.time()
        rep["pins"] = {"jev_model": self.fabric.jev.model if self.fabric.jev else None,
                       "state_compiler": str(config.get("decision_engineering.state_compiler_version", "sc-1"))}
        if save:
            f = self.calibration_file(ref)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(rep, indent=1, default=str))
        return rep

    # ── read models ──────────────────────────────────────────────────────────

    def overview(self, window_s: float = 86400) -> dict:
        since = time.time() - window_s
        rows = self.store.q("SELECT route, executor, confidence, record FROM provenance WHERE ts>=?", (since,))
        n = len(rows)
        by_exec: dict[str, int] = {}
        by_route: dict[str, int] = {}
        lat: list[float] = []
        cost = 0.0
        unknown_cost = False
        for r in rows:
            by_route[r["route"]] = by_route.get(r["route"], 0) + 1
            tier = _tier_of(r["executor"], r["route"])
            by_exec[tier] = by_exec.get(tier, 0) + 1
            rec = json.loads(r["record"])
            for c in rec.get("chain") or []:
                if c.get("latency_ms"):
                    lat.append(float(c["latency_ms"]))
        ops = self.store.q("SELECT classification, COUNT(*) AS n FROM operations GROUP BY classification")
        steps = {o["classification"]: o["n"] for o in ops}
        bounded = sum(by_exec.get(t, 0) for t in ("code", "jev", "local_fast", "local_reasoning", "kimi",
                                                  "frontier", "human", "safe_default"))
        jev_auto = by_exec.get("jev", 0)
        heavy = sum(by_exec.get(t, 0) for t in ("kimi", "frontier"))
        lat_rows = self.fabric.log.items[-2000:]
        dec_lat = [x["latency_ms"] for x in lat_rows if x.get("latency_ms") and not x.get("cached")]
        return {
            "window_hours": window_s / 3600, "decisions": n or None,
            "share": {k: round(v / n, 4) for k, v in by_exec.items()} if n else None,
            "by_route": by_route or None,
            "jev_auto_rate": round(by_route.get("auto", 0) / n, 4) if n else None,
            "decision_offload_rate": round((by_exec.get("jev", 0) + by_exec.get("code", 0)) / bounded, 4)
            if bounded else None,
            "frontier_generation_avoidance_rate": round(1 - heavy / bounded, 4) if bounded else None,
            "heavy_escalations": heavy if n else None,
            "jev_resolved": jev_auto if n else None,
            "median_decision_latency_ms": round(statistics.median(dec_lat), 1) if dec_lat else None,
            "median_escalation_latency_ms": round(statistics.median(lat), 1) if lat else None,
            "agent_steps": steps or None,
            "human_pending": HumanReviewProvider(self.store).pending_count(),
            "note": "computed from provenance and mined operations; null = no data yet",
            "cost_unknown_tiers": unknown_cost or None, "cost_usd": round(cost, 6) if n else None,
        }

    def cascade_edges(self, window_s: float = 86400 * 7) -> dict:
        """Volume, latency and cost per cascade edge from provenance chains (Cascade Explorer)."""
        since = time.time() - window_s
        edges: dict[str, dict] = {}
        total = 0
        for r in self.store.q("SELECT executor, route, record FROM provenance WHERE ts>=?", (since,)):
            total += 1
            rec = json.loads(r["record"])
            prev = "jev" if r["executor"] != "code" else "code"
            for c in rec.get("chain") or []:
                tier = c.get("tier")
                if not tier or c.get("skipped"):
                    continue
                key = f"{prev}->{tier}"
                e = edges.setdefault(key, {"volume": 0, "latency_ms": [], "errors": 0})
                e["volume"] += 1
                if c.get("latency_ms"):
                    e["latency_ms"].append(c["latency_ms"])
                if c.get("error"):
                    e["errors"] += 1
                prev = tier
        for e in edges.values():
            ls = e.pop("latency_ms")
            e["p50_ms"] = round(statistics.median(ls), 1) if ls else None
            e["share"] = round(e["volume"] / total, 4) if total else None
        return {"decisions": total, "edges": edges}

    def inventory_from_store(self) -> list[dict]:
        rows = self.store.q("SELECT signature, agent, classification, COUNT(*) AS n, SUM(input_tokens) AS tin, "
                            "SUM(output_tokens) AS tout, AVG(latency_ms) AS lat, AVG(bounded) AS bounded, "
                            "MIN(ts) AS t0, MAX(ts) AS t1 FROM operations GROUP BY signature ORDER BY n DESC")
        return rows

    def save_operations(self, ops: list) -> int:
        n = 0
        with self.store.db.tx() as c:
            for o in ops:
                d = o.to_dict()
                c.execute("INSERT OR REPLACE INTO operations(id,run_id,seq,ts,agent,workflow,source,kind,name,"
                          "signature,classification,class_confidence,reasons,executor,input_tokens,output_tokens,"
                          "latency_ms,cost_usd,output_label,reversible,bounded,record) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (d["id"], d["run_id"], d["seq"], d["ts"], d["agent"], d["workflow"], d["source"], d["kind"],
                           d["name"], d["signature"], d["classification"], d["confidence"], json.dumps(d["reasons"]),
                           d["executor"], d["input_tokens"], d["output_tokens"], d["latency_ms"], None,
                           d["output_label"], int(d["reversible"]), None if d["bounded"] is None else int(d["bounded"]),
                           json.dumps({k: d[k] for k in ("weaknesses", "detections", "separable", "features")},
                                      default=str)))
                n += 1
        return n


def _tier_of(executor: str, route: str) -> str:
    if route in ("safe_default", "advisory"):
        return "safe_default"
    if route == "human":
        return "human"
    if route == "baseline":
        return "baseline"
    return {"rules": "code", "local_llm": "local_fast", "kimi-k3": "kimi", "local-reasoning": "local_reasoning",
            "local-fast": "local_fast"}.get(executor, executor)


__all__ = ["Runtime", "packages_root"]
