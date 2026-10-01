"""Models area: roles, deployments, discovery, candidates, previews and model operations (spec §10, §26–§29, §41, §98).

Roles are logical capabilities (Fast, Balanced, Deep…) joined from the controller routing table, the
gateway's live alias status and the memory guard; deployments are controller registry rows. Every
operation is pre-validated here because the controller is lenient (pin/delete of an unknown id
returns ok, canary percent and promote alias are unchecked), and dangerous ones are previewed first:
the preview and the POST share one builder, so the typed confirmation the UI asks for is exactly the
one the server checks. Reads degrade to the poller cache; nothing here answers 500.

Owner: SYS.
"""
from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Depends, Query

from lif.console import auth, poller, upstream
from lif.console import humanize as hz
from lif.console.contracts import (ActionPreview, ActivityEvent, CandidateComparison, CandidateItem, ComparisonRow,
                                   DeploymentDetail, DiscoveryRequest, DiscoveryRun, DiscoveryStage, DiscoveryState,
                                   ModelAction, ModelActionRequest, ModelDeployment, ModelEvent, ModelRole,
                                   ModelsOverview, OkResponse, PreviewAction, RollbackRequest, User)
from lif.console.errors import human
from lif.console.events import hub
from lif.console.upstream import UpstreamError

router = APIRouter(prefix="/api/models", tags=["models"])

_read = Depends(auth.require("read"))

PORTFOLIO_STATES = ("PRODUCTION", "CANARY", "STANDBY")
CANDIDATE_STATES = ("CANDIDATE", "DOWNLOADING", "STAGED", "VALIDATING", "BENCHMARKING", "APPROVED", "CANARY")
OPERATE: set[str] = {"load", "unload", "benchmark", "download", "test"}
ROUTING_SWITCH = "New requests switch within about 15 seconds; requests already running finish where they started."


# ── shared reads ─────────────────────────────────────────────────────────────────────────────

def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) and "error" not in v else {}


async def _routing() -> dict[str, Any]:
    """Live routing table (open endpoint, cheap); the poller copy when the controller is down."""
    try:
        return _d(await upstream.get("controller", "/v1/routing", timeout=4.0))
    except UpstreamError:
        return _d(poller.snapshot().raw.get("routing"))


def _ctx(routing: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    raw = poller.snapshot().raw
    shed = _d(raw.get("memory_guard")).get("shed") or []
    return routing if routing is not None else _d(raw.get("routing")), _d(raw.get("capabilities")), \
        [str(s) for s in shed] if isinstance(shed, list) else []


async def _rows(states: tuple[str, ...]) -> list[dict[str, Any]]:
    body = await upstream.get("controller", "/v1/models", params={"state": ",".join(states)}, timeout=8.0)
    rows = body.get("models") if isinstance(body, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _cached_rows(states: tuple[str, ...]) -> list[dict[str, Any]]:
    return [r for r in poller.snapshot().raw.get("models") or [] if isinstance(r, dict) and r.get("state") in states]


# Registry profile ids ('qwen3-1.7b-q8-cpu'): the id goes into an upstream path, so nothing that could
# change the path (slashes, dot segments, '?', '#', '%') gets through — it simply doesn't exist.
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


def _model_not_found() -> Exception:
    return human(404, "Model not found", "It may have been removed. Nothing was changed.", "Refresh the list.")


def _check_id(mid: str) -> None:
    if not MODEL_ID.match(mid) or ".." in mid:
        raise _model_not_found()


async def _row(mid: str) -> dict[str, Any]:
    """One registry row with benchmarks and activity, or a human 404 (the controller is lenient about ids)."""
    _check_id(mid)
    try:
        row = await upstream.get("controller", f"/v1/models/{mid}", timeout=6.0)
    except UpstreamError as e:
        raise upstream.to_human(e, doing="read this model", not_found="Model not found") from None
    if not isinstance(row, dict) or not row.get("id"):
        raise _model_not_found()
    return row


def _role_order(dep: ModelDeployment) -> int:
    keys = [r[1] for r in hz.ROLES]
    return min((keys.index(a) for a in dep.roles if a in keys), default=len(keys))


# ── portfolio ────────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=ModelsOverview)
async def overview(_: User = _read) -> ModelsOverview:
    """Roles first, then the models in use or on standby (§26) — not every downloaded file."""
    poller.ensure_fresh()
    snap = poller.snapshot()
    routing, caps, shed = _ctx()
    try:
        rows = await _rows(PORTFOLIO_STATES)
    except UpstreamError:
        rows = _cached_rows(PORTFOLIO_STATES)
    deployments = sorted((poller.build_deployment(r, routing, caps, shed) for r in rows),
                         key=lambda d: (PORTFOLIO_STATES.index(d.state) if d.state in PORTFOLIO_STATES else 9,
                                        _role_order(d)))
    counts = _d(snap.raw.get("overview")).get("models")
    cached = snap.raw.get("models") or []
    if cached:
        n = sum(1 for r in cached if isinstance(r, dict) and r.get("state") in CANDIDATE_STATES and r.get("state") != "CANARY")
    else:
        n = sum(int(hz.num(v) or 0) for k, v in _d(counts).items() if k in ("CANDIDATE", "STAGED", "APPROVED"))
    roles = snap.roles or poller.build_roles(routing, caps, shed)
    return ModelsOverview(roles=roles, deployments=deployments, candidates_count=n)


@router.get("/roles/{role}", response_model=ModelRole)
async def role(role: str, _: User = _read) -> ModelRole:
    if role not in hz.ROLE_BY_KEY:
        raise human(404, "Unknown model role", "There's no role with that name.", "Go back to Models.",
                    [("Open Models", "/models")])
    found = next((r for r in poller.snapshot().roles if r.role == role), None)
    if found is None:
        routing, caps, shed = _ctx(await _routing())
        found = next(r for r in poller.build_roles(routing, caps, shed) if r.role == role)
    return found


@router.get("/deployments/{mid}", response_model=DeploymentDetail)
async def deployment(mid: str, _: User = _read) -> DeploymentDetail:
    _check_id(mid)
    routing, caps, shed = _ctx()
    try:
        row = await upstream.get("controller", f"/v1/models/{mid}", timeout=6.0)
    except UpstreamError as e:
        if e.status == 404:
            raise upstream.to_human(e, not_found="Model not found") from None
        cached = next((d for d in poller.snapshot().deployments if d.id == mid), None)
        if cached is None:
            raise upstream.to_human(e, doing="read this model", not_found="Model not found") from None
        note = ActivityEvent(id="note", ts=time.time(), severity="warning", title=upstream.reason(e),
                             detail="Showing the last known details; benchmarks and history are unavailable.")
        return DeploymentDetail(deployment=cached, activity=[note])
    if not isinstance(row, dict) or not row.get("id"):
        raise human(404, "Model not found", "It may have been removed.", "Refresh the list.")
    bench = [poller.benchmark_summary(b) for b in row.get("benchmarks") or [] if isinstance(b, dict)]
    if bench and not row.get("last_benchmark"):
        b0 = next(b for b in row["benchmarks"] if isinstance(b, dict))
        row = {**row, "last_benchmark": {"ts": b0.get("ts"), "summary": b0.get("summary")}}
    acts = [e for e in (hz.activity(a) for a in row.get("activity") or [] if isinstance(a, dict)) if e is not None]
    return DeploymentDetail(deployment=poller.build_deployment(row, routing, caps, shed), benchmarks=bench,
                            activity=acts[:50])


# ── discovery (§28) ──────────────────────────────────────────────────────────────────────────

# POST /v1/models/refresh returns no run id and swallows a 'disabled' outcome, so the console remembers
# the newest run id it saw before asking and watches for a newer one.
_disc: dict[str, Any] = {"requested_at": 0.0, "known_max": 0, "categories": []}
DISCOVERY_START_GRACE = 20.0


def _int(v: Any) -> int:
    n = hz.num(v)
    return int(n) if n is not None else 0


def _upgrades(run_ts: float, shortlisted: set[str]) -> int:
    """Shortlisted models from this run that have since passed evaluation with a CANARY recommendation."""
    n = 0
    for r in poller.snapshot().raw.get("models") or []:
        if not isinstance(r, dict) or r.get("state") not in ("APPROVED", "CANARY", "PRODUCTION"):
            continue
        rid = str(_d(r.get("meta")).get("model_id") or r.get("model_id") or "")
        rec = str(_d(_d(r.get("screening")).get("comparison")).get("recommendation") or "").upper()
        if rid in shortlisted and rec == "CANARY" and (hz.num(r.get("updated")) or 0) >= run_ts:
            n += 1
    return n


FUNNEL_KEYS = ("listed", "after_listing_filter", "detail_fetched", "after_deterministic", "after_screening")


def discovery_run(run: dict[str, Any], running_id: Any) -> DiscoveryRun:
    """Controller discovery row → DiscoveryRun. The funnel is written only when a run finishes, so a running
    run has indeterminate stages (count None, done False) rather than invented progress."""
    rid = run.get("id")
    status = str(run.get("status") or "running")
    if status == "running" and running_id != rid:
        status = "interrupted"          # the controller restarted mid-run and never reconciled the row
    funnel = _d(run.get("funnel"))
    cats = _d(funnel.get("categories"))
    good = {k: v for k, v in cats.items() if isinstance(v, dict) and "error" not in v}
    listed = sum(_int(c.get("listed")) for c in good.values())
    filtered = sum(_int(c.get("after_deterministic", c.get("after_listing_filter"))) for c in good.values())
    screened = sum(_int(c.get("after_screening")) for c in good.values())
    short: set[str] = {str(x) for c in good.values() for x in (c.get("shortlisted") or []) if x}
    finished = status in ("succeeded", "failed") and bool(good)
    defs = (("discovering", "Discovering", listed, "models found"), ("filtering", "Filtering", filtered, "relevant"),
            ("evaluating", "Evaluating", screened, "candidates"),
            ("benchmarking", "Benchmarking", len(short), "shortlisted for testing"))
    stages = [DiscoveryStage(key=k, label=f"{lbl} ({n:,} {unit})" if finished else lbl,  # type: ignore[arg-type]
                             count=n if finished else None, done=finished) for k, lbl, n, unit in defs]
    upgrades = _upgrades(hz.num(run.get("ts")) or 0.0, short) if finished else 0
    if status == "running":
        summary = "Checking for better models…"
    elif status == "interrupted":
        summary = "Interrupted: the control service restarted during this check"
    elif status == "failed" and not good:
        err = hz.run_error(run.get("error"), "", 120)
        summary = "Check failed" + (f": {err}" if err else "")
    elif upgrades:
        summary = f"{upgrades} meaningful upgrade{'s' if upgrades != 1 else ''} found"
    elif short:
        summary = f"{len(short)} candidate{'s' if len(short) != 1 else ''} worth testing (not benchmarked yet)"
    else:
        summary = "No better models found"
    errs = {k: str(v.get("error"))[:80] for k, v in cats.items() if isinstance(v, dict) and "error" in v}
    return DiscoveryRun(
        id=str(rid or ""), started_at=hz.num(run.get("ts")) or 0.0, finished_at=hz.num(run.get("finished")),
        status=status, stages=stages, summary=summary, upgrades=upgrades, categories=list(cats),  # type: ignore[arg-type]
        tech=hz.tech(run=rid, raw_status=run.get("status"), total_ms=funnel.get("total_ms"),
                     # the controller's raw funnel, summed over categories ("Internal steps" in the UI)
                     **{k: sum(_int(c.get(k)) for c in good.values()) if finished else None for k in FUNNEL_KEYS},
                     screen_ms=sum(_int(c.get("screen_ms")) for c in good.values()) or None,
                     jev_calls=sum(_int(c.get("jev_calls")) for c in good.values()) or None,
                     shortlisted=", ".join(sorted(short)), category_errors=errs or None, error=run.get("error")))


def _placeholder(categories: list[str]) -> DiscoveryRun:
    return DiscoveryRun(status="running", summary="Starting…", categories=categories, started_at=_disc["requested_at"],
                        stages=[DiscoveryStage(key=k, label=lbl) for k, lbl in (  # type: ignore[arg-type]
                            ("discovering", "Discovering"), ("filtering", "Filtering"),
                            ("evaluating", "Evaluating"), ("benchmarking", "Benchmarking"))])


def _discovery_enabled() -> tuple[bool, str | None]:
    s = _d(poller.snapshot().raw.get("settings"))
    if s.get("discovery_disabled"):
        return False, "Model checks are turned off in System → Settings."
    return True, None


async def _discovery_state() -> DiscoveryState:
    enabled, why = _discovery_enabled()
    try:
        body = await upstream.get("controller", "/v1/discovery/runs")
    except UpstreamError as e:
        return DiscoveryState(enabled=False, disabled_reason=f"{upstream.reason(e)}, so model checks can't be "
                                                             "started or followed right now.")
    raw_runs = body.get("runs") if isinstance(body, dict) else None
    runs = [r for r in raw_runs if isinstance(r, dict)] if isinstance(raw_runs, list) else []
    running_id = body.get("running") if isinstance(body, dict) else None
    out = [discovery_run(r, running_id) for r in runs]
    current = next((r for r in out if r.status == "running"), None)
    note: str | None = None
    newest = max((_int(r.get("id")) for r in runs), default=0)
    pending = _disc["requested_at"] and newest <= _disc["known_max"]
    if current is None and pending:
        age = time.time() - _disc["requested_at"]
        task = str(_d(_d(poller.snapshot().raw.get("tasks"))).get("discovery") or "")
        if age < DISCOVERY_START_GRACE or task == "running":
            current = _placeholder(_disc["categories"])
        else:       # the controller swallowed it (disabled) or the task failed before creating a run row
            _disc["requested_at"] = 0.0
            note = ("The last check ended without running; model checks may have been turned off."
                    if not task.startswith("failed") else f"The last check failed to start: {task[7:].strip()[:120]}")
    elif not pending:
        _disc["requested_at"] = 0.0
    # disabled_reason only explains a disabled button; a remark about the last request is a note.
    return DiscoveryState(current=current, recent=[r for r in out if r is not current][:10], enabled=enabled,
                          disabled_reason=why, note=note)


@router.get("/discovery", response_model=DiscoveryState)
async def discovery(_: User = _read) -> DiscoveryState:
    return await _discovery_state()


@router.post("/discovery", response_model=DiscoveryState)
async def start_discovery(req: DiscoveryRequest, user: User = Depends(auth.require("models.discover"))) -> DiscoveryState:
    """'Check for better models'. Screening each survivor costs a little paid Jev usage (stated in the UI)."""
    cats = [c for c in (req.categories or []) if c]
    bad = [c for c in cats if c not in hz.DISCOVERY_CATEGORIES]
    if bad:
        raise human(422, "Unknown model category", "Nothing was started.",
                    f"Choose from: {', '.join(hz.DISCOVERY_CATEGORIES)}.", tech={"unknown": ", ".join(bad)})
    enabled, why = _discovery_enabled()
    if not enabled:
        raise human(409, "Model checks are turned off", why or "", "Turn model checks back on, then try again.",
                    [("Open settings", "/system/settings")])
    try:
        body = await upstream.get("controller", "/v1/discovery/runs")
        runs = body.get("runs") if isinstance(body, dict) else None
        _disc["known_max"] = max((_int(r.get("id")) for r in (runs if isinstance(runs, list) else [])
                                  if isinstance(r, dict)), default=0)
        res = await upstream.post("controller", "/v1/models/refresh", {"categories": cats} if cats else {},
                                  actor=user.name)
    except UpstreamError as e:
        raise upstream.to_human(e, doing="start the model check") from None
    if not (isinstance(res, dict) and res.get("status") == "already_running"):
        _disc["requested_at"], _disc["categories"] = time.time(), cats
    auth.audit(user, "models.discovery", ",".join(cats) or "all")
    hub.publish("model", ModelEvent(summary="Checking for better models"))
    poller.refresh_soon()
    return await _discovery_state()


# ── candidates (§29) ─────────────────────────────────────────────────────────────────────────

SCREENING: dict[str, str] = {"shortlist": "Worth testing", "review": "Needs a human look",
                             "hold": "Unlikely to beat the current model", "reject": "Unsuitable"}


def _role_for(row: dict[str, Any]) -> tuple[str | None, Any]:
    alias = hz.CATEGORY_ALIAS.get(str(row.get("category") or ""))
    r = hz.ROLE_BY_ALIAS.get(alias or "")
    return alias, (r[0] if r else None)


def comparison_hint(row: dict[str, Any]) -> str:
    state = str(row.get("state") or "")
    scr = _d(row.get("screening"))
    comp = _d(scr.get("comparison"))
    if comp.get("recommendation"):
        return hz.RECOMMENDATION.get(str(comp["recommendation"]).upper(), ("", "Compared with the current model"))[1]
    if state in ("DOWNLOADING", "VALIDATING", "BENCHMARKING"):
        return hz.model_state(state)[0]
    if str(_d(row.get("profile")).get("device") or "").lower() == "gpu":
        return "GPU model: testing needs a scheduled GPU window"
    act = str(_d(scr.get("recommendation")).get("action") or "").lower()
    base = SCREENING.get(act, hz.model_state(state)[0])
    return f"{base}; not benchmarked yet" if not row.get("last_benchmark") else base


@router.get("/candidates", response_model=list[CandidateItem])
async def candidates(_: User = _read) -> list[CandidateItem]:
    routing, caps, shed = _ctx()
    try:
        rows = await _rows(CANDIDATE_STATES)
    except UpstreamError:
        rows = _cached_rows(CANDIDATE_STATES)
    order = {"CANARY": 0, "APPROVED": 1, "BENCHMARKING": 2, "VALIDATING": 2, "STAGED": 3, "DOWNLOADING": 3}
    rows.sort(key=lambda r: (order.get(str(r.get("state")), 4), -(hz.num(r.get("updated")) or 0)))
    return [CandidateItem(deployment=poller.build_deployment(r, routing, caps, shed), comparison_hint=comparison_hint(r),
                          role=_role_for(r)[1]) for r in rows]


def _bench(row: dict[str, Any]) -> dict[str, Any]:
    for b in row.get("benchmarks") or []:
        if isinstance(b, dict) and _d(b.get("summary")):
            return {**_d(b.get("summary")), "_suite": b.get("suite"), "_ts": b.get("ts")}
    lb = _d(row.get("last_benchmark"))
    return {**_d(lb.get("summary")), "_ts": lb.get("ts")} if _d(lb.get("summary")) else {}


def verdict(cur: float | None, cand: float | None, better: str, tol: float = 0.02) -> str:
    if cur is None or cand is None:
        return "unknown"
    if abs(cand - cur) <= tol * max(abs(cur), 1e-9):
        return "same"
    return "better" if (cand > cur) == (better == "higher") else "worse"


def _row_metric(metric: str, unit: str, better: str, cur: float | None, cand: float | None,
                note: str | None = None) -> ComparisonRow:
    return ComparisonRow(metric=metric, unit=unit, better=better, current=cur, candidate=cand,  # type: ignore[arg-type]
                         verdict=verdict(cur, cand, better), note=note)  # type: ignore[arg-type]


def _err_rate(b: dict[str, Any]) -> float | None:
    items = hz.num(b.get("items"))
    return round((hz.num(b.get("errors")) or 0) / items * 100, 1) if items else None


def build_comparison(cand_row: dict[str, Any], inc_row: dict[str, Any] | None, alias: str | None,
                     routing: dict[str, Any], caps: dict[str, Any], shed: list[str]) -> CandidateComparison:
    """Current vs candidate on Quality, TTFT, Throughput, Memory and Stability; every number says where it came from."""
    cand = poller.build_deployment(cand_row, routing, caps, shed)
    inc = poller.build_deployment(inc_row, routing, caps, shed) if inc_row else None
    cb, ib = _bench(cand_row), _bench(inc_row or {})
    pending = "not benchmarked yet"
    q = lambda b: round(hz.num(b["quality"]) * 100, 1) if hz.num(b.get("quality")) is not None else None  # noqa: E731
    cand_tps, tps_note = hz.num(cb.get("decode_tps_p50")), None
    if cand_tps is None and cand.speed_basis == "estimated":
        cand_tps, tps_note = cand.speed_tps, "candidate estimated from model size"
    mem_note = None if cand.memory_basis == "configured" and (inc is None or inc.memory_basis == "configured") \
        else "estimated from model size where not configured"
    rows = [
        _row_metric("Quality", "%", "higher", q(ib), q(cb), None if cb else pending),
        _row_metric("Time to first token", "ms", "lower", hz.num(ib.get("ttft_ms_p50")), hz.num(cb.get("ttft_ms_p50")),
                    None if cb else pending),
        _row_metric("Throughput", "tokens/s", "higher", hz.num(ib.get("decode_tps_p50")), cand_tps,
                    tps_note or (None if cb else pending)),
        _row_metric("Memory", "GB", "lower", inc.memory_gb if inc else None, cand.memory_gb, mem_note),
        _row_metric("Stability (errors)", "%", "lower", _err_rate(ib), _err_rate(cb), None if cb else pending),
    ]
    comp = _d(_d(cand_row.get("screening")).get("comparison"))
    rec = str(comp.get("recommendation") or "").upper()
    if rec in hz.RECOMMENDATION:
        code, sentence = hz.RECOMMENDATION[rec]
    elif not cb:
        code, sentence = "benchmark_first", "Not benchmarked yet: run a benchmark to compare it fairly."
    else:
        code, sentence = "unknown", "Benchmarked, but there's no comparison with the current model yet."
    if inc is None and code != "benchmark_first":
        sentence += " There's no current model for this role to compare against."
    jev = _d(comp.get("jev"))
    advisory = None
    verdict = hz.jev_improves(jev.get("improves"))
    if verdict is not None:
        conf = hz.num(jev.get("confidence"))
        advisory = (f"Jev advice (not a measurement): {'likely an improvement' if verdict else 'probably not an improvement'}"
                    + (f", confidence {conf * 100:.0f}%" if conf is not None else ""))
    elif _d(_d(cand_row.get("screening")).get("recommendation")).get("why"):
        advisory = f"Screening note: {str(_d(_d(cand_row.get('screening')).get('recommendation')).get('why'))[:300]}"
    return CandidateComparison(
        candidate=cand, incumbent=inc, role=alias or "", rows=rows, recommendation=sentence,
        recommendation_code=code, advisory=advisory,  # type: ignore[arg-type]
        tech=hz.tech(raw_recommendation=rec or None, failed_checks=comp.get("failed_checks"), delta=comp.get("delta"),
                     thresholds=comp.get("thresholds"), candidate_benchmark_ts=cb.get("_ts"),
                     incumbent_benchmark_ts=ib.get("_ts"), suite=cb.get("_suite")))


@router.get("/candidates/{mid}/compare", response_model=CandidateComparison)
async def compare(mid: str, _: User = _read) -> CandidateComparison:
    row = await _row(mid)
    routing = await _routing()
    _, caps, shed = _ctx(routing)
    alias, _role = _role_for(row)
    chain = _d(routing.get("aliases")).get(alias or "") or []
    inc_id = chain[0] if isinstance(chain, list) and chain else None
    inc_row = None
    if inc_id and inc_id != mid:
        try:
            got = await upstream.get("controller", f"/v1/models/{inc_id}", timeout=6.0)
            inc_row = got if isinstance(got, dict) and got.get("id") else None
        except UpstreamError:
            inc_row = None
    return build_comparison(row, inc_row, alias, routing, caps, shed)


# ── previews (§41, §98) and operations ───────────────────────────────────────────────────────

class _Plan:
    """What an action will do. `refusal` set = the controller will refuse it; the POST refuses first.

    Client convention (contracts are frozen, so no `refusal` field): a preview with confirm "none" and
    no `rollback` line is a refusal: the UI shows it (disabled button reason or a note) and never POSTs.
    Every runnable confirm-"none" preview therefore carries a rollback line."""

    def __init__(self, preview: ActionPreview, refusal: str | None = None, body: dict[str, Any] | None = None):
        self.preview, self.refusal, self.body = preview, refusal, body or {}


def _name(pid: str | None) -> str:
    return hz.model_name(pid) if pid else "no model"


def _target_alias(row: dict[str, Any], alias: str | None) -> str:
    target = alias or hz.CATEGORY_ALIAS.get(str(row.get("category") or "")) or ""
    if target not in hz.ROLE_BY_ALIAS or target == "local/auto":
        raise human(422, "That isn't a model role", "Nothing was changed.",
                    "Pick one of the roles listed on the Models page.", tech={"alias": target or "(none)"})
    return target


async def _alias_versions() -> dict[str, Any]:
    try:
        return _d(await upstream.get("controller", "/v1/aliases"))
    except UpstreamError as e:
        raise upstream.to_human(e, doing="read the role history") from None


def _previous_chain(alias: str, version: int) -> list[str] | None:
    """The chain a rollback returns to, reconstructed from alias_changed activity (there is no history endpoint)."""
    rows = poller.snapshot().raw.get("activity_rows") or []
    current = None
    for r in rows:     # newest first
        d = _d(r.get("detail")) if isinstance(r, dict) else {}
        if r.get("subject") != alias or r.get("kind") != "alias_changed":
            continue
        v = _int(d.get("version"))
        chain = d.get("chain") if isinstance(d.get("chain"), list) else None
        if v == version:
            current = chain
        elif 0 < v < version and chain and chain != current and not d.get("canary"):
            return [str(x) for x in chain]
    return None


async def rollback_plan(alias: str) -> _Plan:
    rows = await _alias_versions()
    row = _d(rows.get(alias))
    label = hz.role_label(alias)
    if not row:
        return _Plan(ActionPreview(title="Nothing to roll back", changes=[f"{label} has no change history to roll back."],
                                   confirm="none"), refusal=f"{label} has no change history to roll back.")
    version = _int(row.get("version"))
    chain = [str(x) for x in row.get("chain") or []] if isinstance(row.get("chain"), list) else []
    if version <= 1:
        return _Plan(ActionPreview(title="Nothing to roll back", changes=[f"{label} is on its first version; "
                                                                        "there's nothing earlier to go back to."],
                                   confirm="none"), refusal="There's no earlier version of this role.")
    prev = _previous_chain(alias, version)
    to = f"{_name(prev[0])}" if prev else "the previous model list"
    return _Plan(ActionPreview(
        title=f"Roll back {label}?",
        changes=[f"{label} ({alias}) goes back from {_name(chain[0] if chain else None)} to {to}."]
                + ([] if prev else ["The exact previous model isn't recorded here; the control service picks the most "
                                    "recent earlier version with a different model."]),
        interrupts=[ROUTING_SWITCH, "If the previous model's server isn't running it is started, even when memory "
                                    "is tight."],
        rollback=f"You can promote {_name(chain[0] if chain else None)} again afterwards.",
        confirm="typed", confirm_text=alias), body={"alias": alias})


async def action_plan(mid: str, action: str, alias: str | None, percent: int | None) -> _Plan:
    """Single source for GET …/preview and the POST check (typed confirmation text included)."""
    row = await _row(mid)
    routing = await _routing()
    aliases = _d(routing.get("aliases"))
    state = str(row.get("state") or "").upper()
    me = _name(mid)
    used_by = [a for a, ch in aliases.items() if isinstance(ch, list) and mid in ch]
    primary_of = [a for a, ch in aliases.items() if isinstance(ch, list) and ch and ch[0] == mid]
    _, caps, shed = _ctx(routing)
    dep = poller.build_deployment(row, routing, caps, shed)
    mem = f"about {dep.memory_gb:g} GB" if dep.memory_gb else "an unknown amount"
    gpu_model = dep.device.lower() == "gpu"

    if action in ("promote", "canary"):
        target = _target_alias(row, alias)
        label = hz.role_label(target)
        chain = aliases.get(target) if isinstance(aliases.get(target), list) else []
        cur = chain[0] if chain else None
        if action == "promote":
            if cur == mid:
                return _Plan(ActionPreview(title=f"{me} already serves {label}", confirm="none",
                                           changes=["Nothing changes."]), refusal=f"{me} already serves {label}.")
            refusal = None if state in ("CANARY", "APPROVED", "STANDBY") else \
                f"Only a model that passed evaluation can be promoted; this one is '{hz.model_state(state)[0].lower()}'."
            return _Plan(ActionPreview(
                title=f"Promote {me}?",
                changes=([refusal] if refusal else []) + [f"{label} ({target}) changes from {_name(cur)} to {me}."
                                                          if cur else f"{label} ({target}) starts using {me}."],
                interrupts=[ROUTING_SWITCH] + ([] if dep.health == "healthy" else
                                               [f"The model server starts first and uses {mem} of memory."]),
                rollback=(f"{_name(cur)} stays next in line as the fallback, and you can roll {label} back to it."
                          if cur else f"You can roll {label} back afterwards."),
                confirm="simple"), refusal=refusal, body={"alias": target})
        pct = 10 if percent is None else percent
        if not 0 <= pct <= 100:
            raise human(422, "The trial share must be between 0 and 100%", "Nothing was changed.", "Pick a percentage.")
        refusal = None if state in ("APPROVED", "STANDBY") else \
            f"Only a model that passed evaluation can be trialled; this one is '{hz.model_state(state)[0].lower()}'."
        return _Plan(ActionPreview(
            title=f"Trial {me} on {pct}% of {label}?",
            changes=([refusal] if refusal else []) + [f"{pct}% of {label} requests go to {me}; {_name(cur)} keeps the rest."],
            interrupts=["Nothing running is interrupted."],
            rollback=f"Roll {label} back to end the trial, or promote {me} to finish it.", confirm="simple"),
            refusal=refusal, body={"alias": target, "percent": pct})

    if action == "rollback":
        target = alias or (primary_of[0] if primary_of else None)
        if not target:
            return _Plan(ActionPreview(title="Nothing to roll back", confirm="none",
                                       changes=[f"{me} isn't the main model of any role."]),
                         refusal=f"{me} isn't the main model of any role.")
        return await rollback_plan(_target_alias(row, target))

    if action == "unload":
        if used_by:
            roles = ", ".join(hz.role_label(a) for a in used_by)
            msg = f"{roles} use{'s' if len(used_by) == 1 else ''} {me}; the control service won't stop a model a role depends on."
            return _Plan(ActionPreview(title=f"Can't stop {me} while it's in use", changes=[msg], confirm="none"),
                         refusal=msg)
        return _Plan(ActionPreview(title=f"Stop {me}?", changes=[f"Stops the model server and frees {mem} of memory."],
                                   interrupts=["Any request running on it is cut off."],
                                   rollback="Start it again at any time.", confirm="simple"))

    if action == "load":
        if gpu_model:
            msg = "GPU models can only run in a scheduled GPU window; the console can't start them."
            return _Plan(ActionPreview(title=f"Can't start {me}", changes=[msg], confirm="none"), refusal=msg)
        snap_mem = poller.snapshot().status.resource.mem_available_gb
        tight = snap_mem is not None and dep.memory_gb is not None and (snap_mem - dep.memory_gb) * 1024 < hz.MEM_HEADROOM_MIB
        return _Plan(ActionPreview(
            title=f"Start {me}?", changes=[f"Starts the model server on the CPU tier using {mem} of memory."],
            interrupts=["Memory is tight: the control service may refuse, or optional services may be paused to make room."]
            if tight else [], rollback="Stop it again at any time.", confirm="simple" if tight else "none"))

    if action == "delete":
        why = None
        if state in ("PRODUCTION", "CANARY"):
            why = f"{me} is in use; promote another model first."
        elif row.get("pinned"):
            why = f"{me} is kept (pinned); unpin it first."
        elif used_by:
            why = f"{', '.join(hz.role_label(a) for a in used_by)} still list {me} as a fallback."
        if why:
            return _Plan(ActionPreview(title=f"Can't remove {me}", changes=[why], confirm="none"), refusal=why)
        return _Plan(ActionPreview(
            title=f"Remove {me}?", changes=[f"Removes {me} and its benchmark history from the registry."],
            interrupts=[], rollback="This can't be undone. A future model check may find it again, but its history is lost.",
            confirm="typed", confirm_text=mid))

    raise human(422, "There's no preview for that action", "Nothing was changed.",
                "Preview is available for promote, trial, rollback, stop, start and remove.")


@router.get("/deployments/{mid}/preview", response_model=ActionPreview)
async def preview(mid: str, action: PreviewAction, alias: str | None = None,
                  percent: int | None = Query(None), _: User = _read) -> ActionPreview:
    return (await action_plan(mid, action, alias, percent)).preview


@router.get("/roles/{role}/rollback/preview", response_model=ActionPreview)
async def rollback_preview(role: str, _: User = _read) -> ActionPreview:
    r = hz.ROLE_BY_KEY.get(role)
    if r is None or role == "auto":
        raise human(404, "Unknown model role", "There's no role with that name.", "Go back to Models.")
    return (await rollback_plan(r[1])).preview


_DONE: dict[str, str] = {
    "load": "Starting the model server. It's ready when its status shows Ready.",
    "unload": "Model server stopped.",
    "benchmark": "Benchmark started. It runs in the background; results appear here when it finishes.",
    "download": "Download started. It runs in the background and is verified when it finishes.",
    "promote": "Promoted. New requests switch within about 15 seconds.",
    "canary": "Trial started.",
    "delete": "Model removed.",
    "block": "Model blocked: it won't be suggested or used.", "unblock": "Model unblocked.",
    "pin": "Model kept: cleanup won't remove it.", "unpin": "Model no longer kept.",
}


def _publish(summary: str, deployment_id: str | None = None, role_key: Any = None) -> None:
    hub.publish("model", ModelEvent(role=role_key, deployment_id=deployment_id, summary=summary))
    poller.refresh_soon()


@router.post("/deployments/{mid}/{action}", response_model=OkResponse)
async def model_action(mid: str, action: ModelAction, req: ModelActionRequest | None = None,
                       user: User = Depends(auth.current_user)) -> OkResponse:
    req = req or ModelActionRequest()
    await auth.require("models.operate" if action in OPERATE else "models.release")(user)
    if action == "test":
        raise human(409, "Testing a single model isn't available yet",
                    "The gateway routes by role, so one specific model can't be targeted from here. Nothing was run.",
                    "Run a benchmark instead: it tests the model on a fixed set of tasks.")
    if action in ("promote", "canary", "delete", "unload", "load"):
        plan = await action_plan(mid, action, req.alias, req.percent)
        if plan.refusal:
            raise human(409, plan.preview.title if plan.preview.title.startswith("Can't") else "That can't be done right now",
                        plan.refusal.rstrip(".") + ". Nothing was changed.", "Check the model's state and try again.")
        if plan.preview.confirm == "typed" and (req.confirm or "").strip() != plan.preview.confirm_text:
            raise human(422, "Confirmation didn't match", "Nothing was changed.",
                        f"Type {plan.preview.confirm_text} exactly to confirm.")
        body = plan.body
    else:
        await _row(mid)             # the controller answers ok for unknown ids on flags/delete
        body = {}
    try:
        if action == "delete":
            await upstream.delete("controller", f"/v1/models/{mid}", actor=user.name)
            res: Any = {}
        else:
            res = await upstream.post("controller", f"/v1/models/{mid}/{action}", body, actor=user.name,
                                      timeout=30.0 if action in ("promote", "canary", "load") else 10.0)
    except UpstreamError as e:
        raise upstream.to_human(e, doing=f"{action} {_name(mid)}", not_found="Model not found") from None
    auth.audit(user, f"models.{action}", mid, body or None)
    already = isinstance(res, dict) and res.get("status") == "already_running"
    msg = "That's already running." if already else _DONE.get(action, "Done.")
    _publish(f"{_name(mid)}: {action}", deployment_id=mid)
    return OkResponse(message=msg)


@router.post("/roles/{role}/rollback", response_model=OkResponse)
async def rollback(role: str, req: RollbackRequest, user: User = Depends(auth.require("models.release"))) -> OkResponse:
    r = hz.ROLE_BY_KEY.get(role)
    if r is None or role == "auto":
        raise human(404, "Unknown model role", "There's no role with that name.", "Go back to Models.")
    plan = await rollback_plan(r[1])
    if plan.refusal:
        raise human(409, "Nothing to roll back", plan.refusal.rstrip(".") + ". Nothing was changed.",
                    "Check the role's history.")
    if (req.confirm or "").strip() != plan.preview.confirm_text:
        raise human(422, "Confirmation didn't match", "Nothing was changed.",
                    f"Type {plan.preview.confirm_text} exactly to confirm.")
    try:
        res = await upstream.post("controller", "/v1/aliases/rollback", plan.body, actor=user.name, timeout=30.0)
    except UpstreamError as e:
        raise upstream.to_human(e, doing=f"roll back {r[2]}", not_found="This role has no history") from None
    auth.audit(user, "models.rollback", r[1], {"to": _d(res).get("note")})
    _publish(f"{r[2]} rolled back", role_key=role)
    return OkResponse(message=f"{r[2]} rolled back. New requests switch within about 15 seconds.")

