"""confidence-calibrator: per-decision reliability, coverage/error curves, consequence-aware
thresholds, sample adequacy and the what-if simulator (spec §22–§30, §61).

Pure Python (no numpy). All inputs are observed samples; nothing here assumes a
provider's confidence is calibrated. Error bounds use the Wilson score interval so a
threshold is chosen on the *upper* bound of the error rate, not on the point estimate.

A Sample's `correct` is ground truth (an observed outcome or a human label). Agreement
with the old executor is NOT ground truth; callers pass it as `agree` and it is reported
separately (agreement_rate) and never used to choose a threshold unless the caller
explicitly sets `use_agreement_as_label` for a decision whose baseline is the reference.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Iterable

from lif.common import config

DEFAULT_BANDS = [0.0, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.99, 1.0000001]


@dataclass
class Sample:
    confidence: float
    correct: bool | None = None          # ground truth: outcome or human label
    agree: bool | None = None            # matches the baseline executor
    slice: str = "common"                # common | edge | failure | <custom>
    answer: str = ""
    expected: str = ""
    exit: bool = False                   # the answer was an exit option (other/none/…)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _labelled(samples: Iterable[Sample], use_agreement: bool = False) -> list[tuple[float, bool, Sample]]:
    out = []
    for s in samples:
        y = s.correct if s.correct is not None else (s.agree if use_agreement else None)
        if y is not None:
            out.append((float(s.confidence), bool(y), s))
    return out


def reliability(samples: list[Sample], bands: list[float] | None = None, use_agreement: bool = False) -> list[dict]:
    bands = bands or DEFAULT_BANDS
    lab = _labelled(samples, use_agreement)
    rows = []
    for lo, hi in zip(bands, bands[1:]):
        inb = [(c, y) for c, y, _ in lab if lo <= c < hi]
        n, k = len(inb), sum(1 for _, y in inb if y)
        wl, wh = wilson(k, n)
        rows.append({"lo": lo, "hi": min(hi, 1.0), "n": n, "accuracy": round(k / n, 4) if n else None,
                     "mean_confidence": round(sum(c for c, _ in inb) / n, 4) if n else None,
                     "accuracy_ci95": [round(wl, 4), round(wh, 4)] if n else None})
    return rows


def ece(samples: list[Sample], bands: list[float] | None = None, use_agreement: bool = False) -> float | None:
    rows = reliability(samples, bands, use_agreement)
    n = sum(r["n"] for r in rows)
    if not n:
        return None
    return round(sum(r["n"] / n * abs(r["accuracy"] - r["mean_confidence"]) for r in rows if r["n"]), 4)


def at_threshold(samples: list[Sample], t: float, use_agreement: bool = False) -> dict:
    """coverage(T) = automatic / total; error_rate(T) = wrong automatic / automatic (§26).
    Exit answers are never automatic (they mean 'escalate')."""
    lab = _labelled(samples, use_agreement)
    total = len(lab)
    auto = [(c, y) for c, y, s in lab if c >= t and not s.exit]
    n, wrong = len(auto), sum(1 for _, y in auto if not y)
    lo, hi = wilson(wrong, n)
    return {"threshold": round(t, 4), "n": total, "auto": n, "coverage": round(n / total, 4) if total else None,
            "error_rate": round(wrong / n, 4) if n else None, "error_ci95_upper": round(hi, 4) if n else None,
            "escalation_rate": round(1 - n / total, 4) if total else None}


def curve(samples: list[Sample], thresholds: list[float] | None = None, use_agreement: bool = False) -> list[dict]:
    ts = thresholds or [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.925, 0.95, 0.97, 0.98, 0.99, 0.995]
    return [at_threshold(samples, t, use_agreement) for t in ts]


def max_error_for(risk: str) -> float | None:
    """Tolerated automatic-error rate by consequence (config decision_engineering.max_error_by_risk).
    None = never automate (critical)."""
    table = config.get("decision_engineering.max_error_by_risk") or {}
    v = table.get(risk)
    return None if v is None else float(v)


def choose_threshold(samples: list[Sample], max_error: float | None, *, min_auto: int = 30,
                     use_agreement: bool = False) -> dict:
    """Lowest T (max coverage) whose Wilson upper bound on automatic error ≤ max_error and
    with at least `min_auto` automatic samples. Returns {threshold: None} when no T qualifies
    — the decision then stays in shadow; there is no universal fallback threshold (§2)."""
    if max_error is None:
        return {"threshold": None, "reason": "risk class may never be automated"}
    lab = _labelled(samples, use_agreement)
    cands = sorted({round(c, 4) for c, _, _ in lab})
    best = None
    for t in cands:                       # ascending: first qualifying T has the largest coverage
        r = at_threshold(samples, t, use_agreement)
        if r["auto"] >= min_auto and r["error_ci95_upper"] is not None and r["error_ci95_upper"] <= max_error:
            best = r
            break
    if best is None:
        return {"threshold": None, "reason": f"no threshold keeps the 95% upper error bound ≤ {max_error} with "
                f"≥ {min_auto} automatic samples (n={len(lab)})"}
    return {**best, "max_error": max_error, "reason": "lowest threshold meeting the consequence bound"}


def choose_low(samples: list[Sample], high: float | None, floor_accuracy: float = 0.5,
               use_agreement: bool = False) -> float | None:
    """Three-zone LOW boundary: below it the provider is no better than `floor_accuracy`
    (Wilson upper bound), so escalating to a reasoning model is wasted spend and the case
    goes to the safe default / human instead. None = no evidence for a low zone."""
    if high is None:
        return None
    lab = sorted(_labelled(samples, use_agreement), key=lambda x: x[0])
    low = None
    for t in sorted({round(c, 4) for c, _, _ in lab if c < high}):
        below = [y for c, y, _ in lab if c < t]
        if len(below) < 10:
            continue
        _, hi = wilson(sum(below), len(below))
        if hi <= floor_accuracy:
            low = t
    return low


def required_samples(max_error: float, z: float = 1.96) -> int:
    """Automatic samples needed so that ZERO observed errors gives a Wilson upper bound
    ≤ max_error (the best case; any error raises it)."""
    n = 1
    while wilson(0, n, z)[1] > max_error and n < 10_000_000:
        n = math.ceil(n * 1.25) if n > 20 else n + 1
    return n


@dataclass
class Adequacy:
    adequate: bool
    n: int
    reasons: list[str] = field(default_factory=list)
    by_slice: dict[str, int] = field(default_factory=dict)
    low_confidence_n: int = 0
    high_confidence_n: int = 0
    failures_n: int = 0
    needed_auto_for_bound: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def adequacy(samples: list[Sample], risk: str = "low", use_agreement: bool = False) -> Adequacy:
    """Is the shadow sample representative enough to calibrate? Driven by evidence, not a
    calendar (§30): common + edge cases, low and high confidence, and observed failures."""
    c = config.get("decision_engineering.calibration") or {}
    lab = _labelled(samples, use_agreement)
    n = len(lab)
    by_slice: dict[str, int] = {}
    for _, _, s in lab:
        by_slice[s.slice] = by_slice.get(s.slice, 0) + 1
    low_n = sum(1 for conf, _, _ in lab if conf < float(c.get("low_band", 0.8)))
    high_n = sum(1 for conf, _, _ in lab if conf >= float(c.get("high_band", 0.9)))
    fails = sum(1 for _, y, _ in lab if not y)
    me = max_error_for(risk)
    need = required_samples(me) if me else None
    reasons = []
    if n < int(c.get("min_samples", 200)):
        reasons.append(f"{n} labelled samples < {c.get('min_samples', 200)}")
    for sl in c.get("required_slices", ["common", "edge"]):
        if by_slice.get(sl, 0) < int(c.get("min_per_slice", 20)):
            reasons.append(f"slice {sl!r} has {by_slice.get(sl, 0)} < {c.get('min_per_slice', 20)}")
    if low_n < int(c.get("min_low_confidence", 20)):
        reasons.append(f"{low_n} low-confidence samples < {c.get('min_low_confidence', 20)}")
    if high_n < int(c.get("min_high_confidence", 50)):
        reasons.append(f"{high_n} high-confidence samples < {c.get('min_high_confidence', 50)}")
    if fails < int(c.get("min_failures", 5)):
        reasons.append(f"only {fails} observed failures; need ≥ {c.get('min_failures', 5)} to see failure modes")
    if need and high_n < need:
        reasons.append(f"risk={risk}: proving error ≤ {me} needs ≥ {need} high-confidence samples (have {high_n})")
    if me is None:
        reasons.append("risk=critical: calibration is advisory only; never automated")
    return Adequacy(adequate=not reasons, n=n, reasons=reasons, by_slice=by_slice, low_confidence_n=low_n,
                    high_confidence_n=high_n, failures_n=fails, needed_auto_for_bound=need)


def report(name: str, samples: list[Sample], risk: str = "low", use_agreement: bool = False) -> dict:
    """The calibration.json document for one decision version."""
    me = max_error_for(risk)
    pick = choose_threshold(samples, me, use_agreement=use_agreement,
                            min_auto=int((config.get("decision_engineering.calibration") or {}).get("min_auto", 30)))
    low = choose_low(samples, pick.get("threshold"), use_agreement=use_agreement)
    lab = _labelled(samples, use_agreement)
    agree = [s.agree for s in samples if s.agree is not None]
    return {"decision": name, "risk": risk, "n_samples": len(samples), "n_labelled": len(lab),
            "label_source": "agreement-with-baseline" if use_agreement else "ground-truth",
            "accuracy": round(sum(1 for _, y, _ in lab if y) / len(lab), 4) if lab else None,
            "agreement_rate": round(sum(agree) / len(agree), 4) if agree else None,
            "ece": ece(samples, use_agreement=use_agreement),
            "reliability": reliability(samples, use_agreement=use_agreement),
            "curve": curve(samples, use_agreement=use_agreement),
            "recommended": {"high": pick.get("threshold"), "low": low, "max_error": me, "detail": pick},
            "adequacy": adequacy(samples, risk, use_agreement).to_dict()}


# ── expected risk / cost / latency (§26–§28, §61) ─────────────────────────────

def expected_failures(volume: float, coverage: float, error_rate: float) -> float:
    return volume * coverage * error_rate


def simulate(samples: list[Sample], volume_per_day: float, costs: dict[str, float], latencies_ms: dict[str, float],
             thresholds: list[float] | None = None, escalate_to: str = "kimi", use_agreement: bool = False,
             llm_first: dict | None = None) -> list[dict]:
    """What-if table for the operator: per threshold → coverage, escalations, error, expected
    failures/day, cost/day and mean latency. `costs` / `latencies_ms` are per call for
    'jev' and the escalation tier, taken from pricing.py and measured telemetry."""
    rows = []
    for r in curve(samples, thresholds, use_agreement):
        if r["coverage"] is None:
            continue
        esc = 1 - r["coverage"]
        cost = volume_per_day * (costs.get("jev", 0.0) + esc * costs.get(escalate_to, 0.0))
        lat = latencies_ms.get("jev", 0.0) + esc * latencies_ms.get(escalate_to, 0.0)
        row = {**r, "escalated_per_day": round(volume_per_day * esc, 1),
               "expected_failures_per_day": round(expected_failures(volume_per_day, r["coverage"],
                                                                    r["error_rate"] or 0.0), 3),
               "cost_per_day_usd": round(cost, 4), "mean_latency_ms": round(lat, 1)}
        if llm_first:
            row["savings_vs_llm_first_usd"] = round(volume_per_day * llm_first.get("cost", 0.0) - cost, 4)
            row["latency_vs_llm_first_ms"] = round(lat - llm_first.get("latency_ms", 0.0), 1)
        rows.append(row)
    return rows
