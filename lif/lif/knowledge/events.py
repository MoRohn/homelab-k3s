"""LIF platform events → persistent knowledge.

The controller's activity stream (registry `activity` table, `/v1/activity?since=seq`) is the
platform's event bus. This sink turns the *significant* events into typed objects and leaves the
rest as telemetry (memory-guard sheds, state flaps and probes stay in Prometheus/SQLite, not Git).

    benchmark_completed            → benchmark (evidence) linked to the model
    state_changed → PRODUCTION     → decision (promotion, evidence = latest benchmark) + deployment + event MODEL_PROMOTED
    state_changed → other states   → model.state updated (+ event for REJECTED / FAILED / QUARANTINED)
    alias_rollback                 → event MODEL_ROLLBACK + change; the rolled-back deployment is superseded
    setting_changed                → event CONFIG_CHANGED (only for keys that alter platform behavior)

Objects get deterministic ids from the activity sequence number, so re-consuming the stream (for
example after `knowledge rebuild` drops the cursor) never duplicates knowledge.

Where they are written is `events_repo` in workspace.yaml: operational history is private by
default (CLAUDE.md), so with no events_repo configured the sink refuses to write.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import TYPE_CHECKING

import httpx

from lif.common import log
from lif.knowledge.writer import WriteError, slug

if TYPE_CHECKING:
    from lif.knowledge.ops import Knowledge

LOG = log.get("lif.knowledge.events")
BEHAVIOR_SETTINGS = {"automatic_discovery", "automatic_promotion", "automatic_download", "maintenance",
                     "batch_paused", "jev_enabled"}


class EventSink:
    def __init__(self, kn: "Knowledge", repo: str | None = None, actor: str = "lif-controller"):
        self.kn = kn
        self.repo = repo or kn.ws.config.get("events_repo")
        if not self.repo or kn.ws.repo(self.repo) is None:
            raise WriteError(f"events_repo '{self.repo}' is not configured/present (operational history is "
                             f"private; set events_repo in workspace.yaml)")
        self.actor = actor
        self.w = kn.writer(actor)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _exists(self, oid: str) -> bool:
        return self.kn.store.get(f"{self.repo}::{oid}") is not None

    def _create(self, type: str, oid: str, fields: dict, body: str = "", folder: str | None = None) -> str | None:
        """→ oid if written now, None if it already existed (re-consumed event)."""
        if self._exists(oid):
            return None
        self.w.create(type, fields, body, id=oid, repo=self.repo, folder=folder, force=True)
        return oid

    def model(self, mid: str, detail: dict | None = None) -> str:
        oid = f"model-{slug(mid, 80)}"
        if not self._exists(oid):
            d = detail or {}
            self._create("model", oid, {"title": mid, "profile": mid, "hf_id": d.get("model_id", ""),
                                        "category": d.get("category", ""), "state": d.get("state", "")},
                         f"Model profile `{mid}` as known to the LIF registry.")
        return oid

    @staticmethod
    def _date(ts: float) -> str:
        return dt.datetime.fromtimestamp(ts).date().isoformat()

    def _active_deployment(self, profile: str, alias: str) -> str | None:
        for dep in self.kn.graph.find(type="deployment", status="active", repo=self.repo):
            if dep["fields"].get("profile") == profile and dep["fields"].get("alias") == alias:
                return dep["key"]
        return None

    def latest_benchmark(self, model_oid: str) -> str | None:
        rows = self.kn.graph.find(type="benchmark", links_to=f"{self.repo}::{model_oid}")
        rows.sort(key=lambda r: (r["fields"].get("seq") or 0), reverse=True)
        return rows[0]["key"].split("::")[-1] if rows else None

    # ── consumption ──────────────────────────────────────────────────────────
    def consume(self, activity: list[dict]) -> list[str]:
        """activity rows as returned by the registry (any order). Returns created/updated keys."""
        out: list[str] = []
        for ev in sorted(activity, key=lambda e: e["seq"]):
            try:
                out += [k for k in self._one(ev) if k]
            except WriteError as e:
                LOG.warning("event not recorded", extra={"fields": {"seq": ev["seq"], "err": str(e)[:200]}})
        self.kn.store.set_meta("events_cursor", max([e["seq"] for e in activity] + [self.cursor]))
        return out

    @property
    def cursor(self) -> int:
        return int(self.kn.store.meta("events_cursor", 0) or 0)

    def _one(self, ev: dict) -> list[str]:
        kind, subj, d = ev["kind"], ev.get("subject", ""), ev.get("detail") or {}
        if isinstance(d, str):
            d = json.loads(d)
        seq, date, actor = ev["seq"], self._date(ev["ts"]), ev.get("actor", "")
        if kind == "benchmark_completed":
            m = self.model(subj)
            oid = f"benchmark-{slug(subj, 60)}-{seq}"
            summary = {k: v for k, v in d.items() if k != "suite" and v is not None}
            return [self._create("benchmark", oid, {"title": f"{subj} · {d.get('suite', 'suite')} benchmark", "model": f"[[{m}]]",
                                            "suite": d.get("suite", ""), "summary": summary, "date": date, "seq": seq,
                                            "basis": "measured", "produced_by": None,
                                            "location": f"lif-registry:benchmarks (activity seq {seq})"},
                         f"Local benchmark of `{subj}` on this DGX (suite `{d.get('suite')}`).")]
        if kind == "state_changed":
            m = self.model(subj)
            to = d.get("to", "")
            out: list[str | None] = []
            if (self.kn.store.get(f"{self.repo}::{m}") or {}).get("fields", {}).get("state") != to.lower():
                self.w.update(f"{self.repo}::{m}", set={"state": to.lower()}, force=True)
                out.append(m)
            reason = d.get("reason", "")
            if to == "PRODUCTION" and reason.startswith("rollback of "):
                # a rollback restoring known-good capacity: part of MODEL_ROLLBACK, not a new decision
                alias = reason.removeprefix("rollback of ").strip()
                if not self._active_deployment(subj, alias):
                    dep = f"deployment-{slug(subj, 60)}-{seq}"
                    out.append(self._create("deployment", dep, {
                        "title": f"{subj} restored to {alias}", "model": f"[[{m}]]", "status": "active",
                        "since": date, "profile": subj, "alias": alias, "restored_by_rollback": True},
                        f"Restored by a rollback of `{alias}` (activity seq {seq}, actor `{actor}`).",
                        folder="deployments"))
                out.append(self._event("MODEL_RESTORED", seq, date, actor, m, d))
            elif to == "PRODUCTION" and not reason.startswith("promoted to "):
                out.append(self._event("MODEL_PRODUCTION", seq, date, actor, m, d))     # forced/unknown path
            elif to == "PRODUCTION":
                bench = self.latest_benchmark(m)
                dec = f"promote-{slug(subj, 60)}-{seq}"
                dep = f"deployment-{slug(subj, 60)}-{seq}"
                out.append(self._create("decision", dec, {
                    "title": f"Promote {subj} ({reason})", "question": f"Should {subj} serve production traffic?",
                    "status": "accepted", "selected": subj, "date": date, "deciders": [actor],
                    "evidence": [f"[[{bench}]]"] if bench else [], "kind": "model-promotion",
                    "affects": [f"[[{dep}]]"], "method": "[[model-evaluation::model-promotion]]"
                    if self.kn.ws.repo("model-evaluation") else None},
                    f"Recorded from the LIF registry (activity seq {seq}, actor `{actor}`): {reason}.\n\n"
                    f"Evidence is the most recent local benchmark of the model at promotion time.",
                    folder="decisions"))
                out.append(self._create("deployment", dep, {"title": f"{subj} in production", "model": f"[[{m}]]",
                                                 "status": "active", "since": date, "profile": subj,
                                                 "alias": reason.removeprefix("promoted to ").strip() or None},
                             folder="deployments"))
                out.append(self._event("MODEL_PROMOTED", seq, date, actor, m, d))
            else:
                if d.get("frm") == "PRODUCTION":       # left production: its deployments end
                    for dep in self.kn.graph.find(type="deployment", status="active", repo=self.repo):
                        if dep["fields"].get("profile") == subj:
                            self.w.update(dep["key"], set={"status": "superseded", "superseded_on": date}, force=True)
                            out.append(dep["key"].split("::")[-1])
                if to in ("REJECTED", "FAILED", "QUARANTINED"):
                    out.append(self._event(f"MODEL_{to}", seq, date, actor, m, d))
            return out
        if kind == "alias_rollback":
            evid = self._event("MODEL_ROLLBACK", seq, date, actor, None, {"alias": subj, **d})
            for dep in self.kn.graph.find(type="deployment", status="active"):
                if dep["fields"].get("alias") == subj and dep["fields"].get("profile") not in d.get("chain", []):
                    self.w.update(dep["key"], set={"status": "superseded", "superseded_on": date}, force=True)
            return [evid]
        if kind == "setting_changed" and subj in BEHAVIOR_SETTINGS:
            return [self._event("CONFIG_CHANGED", seq, date, actor, None, {"setting": subj, **d})]
        return []

    def _event(self, kind: str, seq: int, date: str, actor: str, subject: str | None, detail: dict) -> str | None:
        oid = f"event-{seq}-{slug(kind, 30)}"
        return self._create("event", oid, {"title": f"{kind} {detail.get('alias') or subject or ''}".strip(),
                                           "kind": kind, "at": date, "date": date, "actor": actor,
                                           "subject": f"[[{subject}]]" if subject else None,
                                           "detail": {k: v for k, v in detail.items() if v is not None}},
                            folder="events")

    # ── live tail of the controller ──────────────────────────────────────────
    async def tail(self, controller_url: str, admin_key: str, every: float = 15.0) -> None:
        async with httpx.AsyncClient(timeout=15) as http:
            while True:
                try:
                    r = await http.get(f"{controller_url}/v1/activity", params={"since": self.cursor, "limit": 500},
                                       headers={"Authorization": f"Bearer {admin_key}"})
                    r.raise_for_status()
                    rows = r.json().get("activity") or []
                    if rows:
                        await asyncio.to_thread(self.consume, rows)
                except Exception:          # noqa: BLE001 — the controller may be down; try again later
                    LOG.exception("event tail failed")
                await asyncio.sleep(every)
