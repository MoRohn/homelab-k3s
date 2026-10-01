"""Decision registry: versioned definitions, lifecycle stages, releases (pins) and rollback.

    DISCOVERED → DESIGNED → TESTED → SHADOW → CALIBRATED → LOW_RISK_AUTOMATION
               → EXPANDED_AUTOMATION → PRODUCTION            (+ RETIRED; rollback from any stage)

Definitions live in Git (config/decisions/*.yaml, decision-packages/<pkg>/<name>/v*.yaml)
and are never edited in place: a change is a new version. What runs is decided by the
*release*: a row that pins {version, stage, rollout %, thresholds, zone policy, Jev model,
state-compiler version}. Releases are append-only; rollback re-activates the previous one.
With no release row, a name resolves to its newest `stage: production` YAML version.

Gates (promotion_blockers) are deterministic. The autotuner may *recommend* a release;
only a named human actor may create one that automates (spec §67).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

from lif.common import config, log
from lif.decision.store import Store, now
from lif.decision.types import DecisionDef, _vkey

LOG = log.get("lif.decision.registry")

STAGES = ["discovered", "designed", "tested", "shadow", "calibrated",
          "low_risk_automation", "expanded_automation", "production"]
RETIRED = "retired"
LIVE = {"low_risk_automation", "expanded_automation", "production"}    # Jev answer may drive actions
OBSERVED = {"shadow", "calibrated"}                                     # Jev runs, result only logged
DEFAULT_ROLLOUT = {"low_risk_automation": 10.0, "expanded_automation": 50.0, "production": 100.0}
MACHINE_ACTORS = {"autotuner", "decision-miner", "decision-compiler", "system", ""}


class RegistryError(Exception):
    pass


@dataclass
class Release:
    name: str
    version: str
    stage: str
    rollout_pct: float = 100.0
    pins: dict[str, str] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    actor: str = ""
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0
    id: int = 0

    @property
    def ref(self) -> str:
        return f"{self.name}/{self.version}"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def promotion_blockers(d: DecisionDef, target: str, evidence: dict[str, Any], actor: str) -> list[str]:
    """Deterministic gates for moving `d` to `target`. Empty list = allowed.

    evidence keys (computed by lint/experiments/calibration, never self-reported by a model):
      lint_errors: int            tests_total, tests_accuracy, tests_min_accuracy: float
      regression_ok: bool         calibration_adequate: bool   shadow_n: int
      thresholds_from_calibration: bool
    """
    if target == RETIRED:
        return []
    if target not in STAGES:
        return [f"unknown stage {target!r}"]
    out: list[str] = []
    ti = STAGES.index(target)
    cur = STAGES.index(d.stage) if d.stage in STAGES else 0
    if ti > cur + 1 and not (cur >= STAGES.index("calibrated") and target in LIVE):
        out.append(f"cannot skip from {d.stage} to {target}; advance one stage at a time")
    if ti >= STAGES.index("tested") and int(evidence.get("lint_errors", 1)):
        out.append("decision lint reports errors")
    if ti >= STAGES.index("shadow"):
        n, acc = int(evidence.get("tests_total", 0)), evidence.get("tests_accuracy")
        floor = float(evidence.get("tests_min_accuracy", config.get(
            "decision_engineering.promotion.min_test_accuracy", 0.9)))
        min_tests = int(config.get("decision_engineering.promotion.min_tests", 20))
        if n < min_tests:
            out.append(f"only {n} labelled test cases (need ≥ {min_tests})")
        elif acc is None or float(acc) < floor:
            out.append(f"test accuracy {acc} below {floor}")
        if evidence.get("regression_ok") is False:
            out.append("regression versus the previous version exceeds policy")
    if ti >= STAGES.index("calibrated") and not evidence.get("calibration_adequate"):
        out.append("calibration sample is not adequate across confidence bands and slices")
    if target in LIVE:
        if not evidence.get("thresholds_from_calibration"):
            out.append("thresholds must come from a calibration report, not defaults")
        if actor in MACHINE_ACTORS:
            out.append("automation stages need a named human actor (autotuner may only recommend)")
        if d.risk == "critical" or not d.reversible:
            out.append(f"risk={d.risk} reversible={d.reversible}: may never be automated (human/policy only)")
    return out


class Registry:
    def __init__(self, definitions: dict[str, DecisionDef], store: Store | None = None):
        self.defs = definitions
        self.store = store

    # ── lookup ───────────────────────────────────────────────────────────────

    def versions(self, name: str) -> list[DecisionDef]:
        vs = [d for k, d in self.defs.items() if "/" in k and d.name == name]
        return sorted(vs, key=lambda d: _vkey(d.version))

    def names(self) -> list[str]:
        return sorted({d.name for d in self.defs.values()})

    def get(self, ref: str) -> DecisionDef:
        if ref not in self.defs:
            raise RegistryError(f"unknown decision {ref!r}")
        return self.defs[ref]

    def active_release(self, name: str) -> Release | None:
        if self.store is None:
            return None
        rows = self.store.q("SELECT * FROM releases WHERE name=? AND active=1 ORDER BY id DESC LIMIT 1", (name,))
        return _release(rows[0]) if rows else None

    def history(self, name: str) -> list[Release]:
        if self.store is None:
            return []
        return [_release(r) for r in self.store.q("SELECT * FROM releases WHERE name=? ORDER BY id DESC", (name,))]

    def resolve(self, name: str) -> DecisionDef | None:
        """The definition that serves bare `name` right now, with the release's pins applied.
        A release in an OBSERVED stage does not serve; the previous production version does."""
        rel = self.active_release(name)
        if rel is not None and rel.stage in LIVE and f"{name}/{rel.version}" in self.defs:
            base = self.defs[f"{name}/{rel.version}"]
            return dataclasses.replace(base, stage=rel.stage,
                                       thresholds={**base.thresholds, **rel.thresholds},
                                       policy={**base.policy, **rel.policy}, pins={**base.pins, **rel.pins})
        return self.defs.get(name)       # newest YAML version with stage: production (types.load_definitions)

    def effective(self) -> dict[str, DecisionDef]:
        """Definitions for a DecisionFabric: every pinned ref plus each bare name resolved."""
        out = {k: v for k, v in self.defs.items() if "/" in k}
        for n in self.names():
            r = self.resolve(n)
            if r is not None:
                out[n] = r
        return out

    def stage_of(self, ref: str) -> str:
        """A version's stage = its most recent release row, else its YAML `stage`."""
        d = self.get(ref)
        if self.store is not None:
            rows = self.store.q("SELECT stage FROM releases WHERE name=? AND version=? ORDER BY id DESC LIMIT 1",
                                (d.name, d.version))
            if rows:
                return rows[0]["stage"]
        return d.stage

    def shadow_candidates(self, name: str) -> list[DecisionDef]:
        """Versions that should run in shadow next to whatever serves `name`."""
        out = []
        for d in self.versions(name):
            st = self.stage_of(d.ref)
            if st in OBSERVED or (st in LIVE and st != "production"):
                out.append(dataclasses.replace(d, stage=st))
        return out

    # ── lifecycle ────────────────────────────────────────────────────────────

    def transition(self, ref: str, target: str, *, actor: str, reason: str, evidence: dict | None = None,
                   thresholds: dict | None = None, policy: dict | None = None, pins: dict | None = None,
                   rollout_pct: float | None = None, force_rollback: bool = False) -> Release:
        if self.store is None:
            raise RegistryError("registry has no store; transitions are recorded in decision-eng.db")
        d = self.get(ref)
        current = dataclasses.replace(d, stage=self.stage_of(ref))
        evidence = evidence or {}
        backwards = target == RETIRED or (target in STAGES and current.stage in STAGES
                                          and STAGES.index(target) < STAGES.index(current.stage))
        if not (backwards or force_rollback):
            blockers = promotion_blockers(current, target, evidence, actor)
            if blockers:
                raise RegistryError(f"{ref} → {target} blocked: " + "; ".join(blockers))
        pins = {**d.pins, **(pins or {})}
        if target in LIVE or target in OBSERVED:
            pins.setdefault("jev_model", str(config.get("decision_fabric.jev.model", "")))
            pins.setdefault("state_compiler", str(config.get("decision_engineering.state_compiler_version", "sc-1")))
        rel = Release(name=d.name, version=d.version, stage=target,
                      rollout_pct=float(rollout_pct if rollout_pct is not None else DEFAULT_ROLLOUT.get(target, 0.0)),
                      pins=pins, thresholds=thresholds or {}, policy=policy or {}, actor=actor, reason=reason,
                      evidence=evidence, ts=now())
        serving = self.active_release(d.name)
        with self.store.db.tx() as c:
            # `active` = the release that serves the bare name. Only LIVE stages serve;
            # demoting the serving version stops it serving (the YAML production version,
            # if any, takes over). Observing a NEW version never displaces what serves.
            if target in LIVE or (serving is not None and serving.version == d.version):
                c.execute("UPDATE releases SET active=0 WHERE name=? AND active=1", (d.name,))
            active = 1 if target in LIVE else 0
            cur = c.execute(
                "INSERT INTO releases(name,version,stage,rollout_pct,pins,thresholds,policy,actor,reason,evidence,ts,"
                "active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rel.name, rel.version, rel.stage, rel.rollout_pct, json.dumps(rel.pins), json.dumps(rel.thresholds),
                 json.dumps(rel.policy), actor, reason, json.dumps(evidence, default=str), rel.ts, active))
            rel.id = int(cur.lastrowid)
        LOG.info("decision stage change", extra={"fields": {"ref": ref, "to": target, "actor": actor}})
        return rel

    def rollback(self, name: str, *, actor: str, reason: str) -> Release | None:
        """Undo the serving release: its version drops back to `shadow` (still observed),
        and the previous LIVE release (another version, or the same version with other
        thresholds) serves again. With no earlier release, the YAML production version serves.
        Returns the release that now serves, or None when YAML serves."""
        if self.store is None:
            raise RegistryError("registry has no store")
        active = self.active_release(name)
        if active is None:
            raise RegistryError(f"{name}: nothing to roll back (no serving release)")
        prev = next((r for r in self.history(name)
                     if r.id < active.id and r.stage in LIVE
                     and (r.version != active.version or r.thresholds != active.thresholds
                          or r.policy != active.policy)), None)
        ev = json.dumps({"rolled_back": active.id, "restored": prev.id if prev else "yaml"})
        cols = "name,version,stage,rollout_pct,pins,thresholds,policy,actor,reason,evidence,ts,active"
        with self.store.db.tx() as c:
            c.execute("UPDATE releases SET active=0 WHERE name=?", (name,))
            c.execute(f"INSERT INTO releases({cols}) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                      (name, active.version, "shadow", 0, json.dumps(active.pins), json.dumps(active.thresholds),
                       json.dumps(active.policy), actor, f"rolled back: {reason}", ev, now()))
            if prev is not None:
                cur = c.execute(f"INSERT INTO releases({cols}) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                                (name, prev.version, prev.stage, prev.rollout_pct, json.dumps(prev.pins),
                                 json.dumps(prev.thresholds), json.dumps(prev.policy), actor,
                                 f"restored by rollback: {reason}", ev, now()))
                prev.id, prev.actor, prev.reason = int(cur.lastrowid), actor, f"restored by rollback: {reason}"
        LOG.warning("decision rolled back", extra={"fields": {"name": name, "to": prev.ref if prev else "yaml"}})
        return prev

    def summary(self) -> list[dict]:
        out = []
        for n in self.names():
            serving = self.resolve(n)
            rel = self.active_release(n)
            out.append({"name": n, "serving": serving.ref if serving else None,
                        "stage": serving.stage if serving else None,
                        "rollout_pct": rel.rollout_pct if rel else (100.0 if serving else 0.0),
                        "versions": [{"ref": d.ref, "stage": self.stage_of(d.ref), "risk": d.risk,
                                      "primitive": d.type, "package": d.package} for d in self.versions(n)]})
        return out


def _release(r: dict) -> Release:
    return Release(name=r["name"], version=r["version"], stage=r["stage"], rollout_pct=r["rollout_pct"],
                   pins=json.loads(r["pins"]), thresholds=json.loads(r["thresholds"]), policy=json.loads(r["policy"]),
                   actor=r["actor"], reason=r["reason"], evidence=json.loads(r["evidence"]), ts=r["ts"], id=r["id"])
