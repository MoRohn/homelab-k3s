"""Deterministic simulation primitives for SimulationSpec (spec §65–§67).

A simulation in the IR names a primitive here and binds its inputs to spec Variables. Outcomes are
always computed by this code. The interactive renderer evaluates the primitive over the control grid
ahead of time and the page only looks results up, so the rules exist once (in Python) and the
browser runs no model of its own.

Add a primitive by registering a pure function: same inputs → same outputs, no I/O, no randomness.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lif.understanding.spec import ExplanationSpec, Variable

MAX_GRID = 20_000


@dataclass(frozen=True)
class Primitive:
    name: str
    inputs: dict[str, str]                       # input → "int" | "float" | "enum"
    outputs: dict[str, str]                      # output → short description
    fn: Callable[[dict[str, Any]], dict[str, Any]]
    defaults: dict[str, Any] = field(default_factory=dict)
    description: str = ""


PRIMITIVES: dict[str, Primitive] = {}


def primitive(name: str, inputs: dict[str, str], outputs: dict[str, str], defaults: dict | None = None,
              description: str = ""):
    def deco(fn):
        PRIMITIVES[name] = Primitive(name, inputs, outputs, fn, defaults or {}, description)
        return fn
    return deco


@primitive("gpu-placement/v1",
           inputs={"gpus": "int", "gpu_mem_gb": "float", "pods": "int", "pod_mem_gb": "float",
                   "affinity": "enum", "eligible_gpus": "int", "reserved_gb": "float"},
           outputs={"placed": "pods bound to a GPU", "pending": "pods left in the queue",
                    "utilization_pct": "share of GPU memory allocated to pods",
                    "idle_gpus": "GPUs with no pod", "pending_reason": "why the first pending pod waits"},
           defaults={"affinity": "none", "eligible_gpus": 0, "reserved_gb": 0.0},
           description="First-fit placement of identical pods on GPUs, with a node-affinity rule and memory "
                       "reserved for the primary workload on GPU 0.")
def gpu_placement(p: dict[str, Any]) -> dict[str, Any]:
    gpus, cap, n, need = int(p["gpus"]), float(p["gpu_mem_gb"]), int(p["pods"]), float(p["pod_mem_gb"])
    affinity = str(p.get("affinity", "none"))
    eligible_n = gpus if affinity == "none" else max(0, min(int(p.get("eligible_gpus") or 0), gpus))
    free = [cap - (float(p.get("reserved_gb") or 0.0) if i == 0 else 0.0) for i in range(gpus)]
    total = sum(max(f, 0.0) for f in free)
    eligible = list(range(eligible_n))
    order = eligible + ([i for i in range(gpus) if i not in eligible] if affinity == "preferred" else [])
    used = [0] * gpus
    placed = 0
    for _ in range(n):
        slot = next((i for i in order if free[i] >= need), None)
        if slot is None:
            break
        free[slot] -= need
        used[slot] += 1
        placed += 1
    pending = n - placed
    reason = "none"
    if pending:
        if need > max(cap - (float(p.get("reserved_gb") or 0.0) if gpus == 1 else 0.0), 0.0):
            reason = "pod_larger_than_gpu"
        elif affinity == "strict" and any(free[i] >= need for i in range(gpus) if i not in eligible):
            reason = "affinity"
        else:
            reason = "memory"
    alloc = sum(used) * need
    return {"placed": placed, "pending": pending,
            "utilization_pct": round(100.0 * alloc / total, 1) if total > 0 else 0.0,
            "idle_gpus": sum(1 for u in used if u == 0), "pending_reason": reason}


@primitive("gpu-reservation-throughput/v1",
           inputs={"total_mem_gb": "float", "reserved_gb": "float", "job_mem_gb": "float", "job_minutes": "float"},
           outputs={"slots": "jobs that fit at once", "jobs_per_hour": "completed jobs per hour",
                    "idle_gb": "memory neither reserved nor used by a job"},
           description="How memory held back for the primary workload limits concurrent batch jobs.")
def reservation_throughput(p: dict[str, Any]) -> dict[str, Any]:
    free = max(0.0, float(p["total_mem_gb"]) - float(p["reserved_gb"]))
    job = float(p["job_mem_gb"])
    slots = math.floor(free / job) if job > 0 else 0
    minutes = max(float(p["job_minutes"]), 1e-9)
    return {"slots": slots, "jobs_per_hour": round(slots * 60.0 / minutes, 2),
            "idle_gb": round(free - slots * job, 1)}


# ── evaluation ───────────────────────────────────────────────────────────────────────────────

def _values(v: Variable) -> list[Any]:
    if v.kind == "enum":
        return list(v.options)
    if v.min is None or v.max is None:
        return [v.default]
    step = v.step or (1 if v.kind == "int" else (v.max - v.min) / 10 or 1)
    out, x = [], float(v.min)
    while x <= float(v.max) + 1e-9:
        out.append(round(x) if v.kind == "int" else round(x, 6))
        x += step
    return out


def _cast(kind: str, val: Any) -> Any:
    if kind == "int":
        return round(float(val))
    if kind == "float":
        return float(val)
    return str(val)


def inputs_for(spec: ExplanationSpec, values: dict[str, Any]) -> dict[str, Any]:
    """Primitive inputs from Variable values (id → value), falling back to each Variable's default."""
    sim = spec.simulation
    assert sim is not None
    prim = PRIMITIVES[sim.primitive]
    out = dict(prim.defaults)
    for vid, name in sim.bindings.items():
        v = spec.get(vid)
        val = values.get(vid, v.default if v is not None else None)
        if val is not None:
            out[name] = _cast(prim.inputs[name], val)
    return out


def run(spec: ExplanationSpec, values: dict[str, Any] | None = None) -> dict[str, Any]:
    sim = spec.simulation
    if sim is None:
        raise ValueError("spec has no simulation")
    return PRIMITIVES[sim.primitive].fn(inputs_for(spec, values or {}))


def grid_key(indices: list[int]) -> str:
    """Grid cells are keyed by control-value indices, so the page never formats numbers itself."""
    return "|".join(str(i) for i in indices)


def grid(spec: ExplanationSpec) -> dict[str, Any]:
    """Evaluate the primitive at every combination of control values. Returns the control order, each
    control's values and {grid_key: outputs}. Refuses grids above MAX_GRID points."""
    sim = spec.simulation
    if sim is None:
        raise ValueError("spec has no simulation")
    controls = [spec.get(c.variable) for c in sim.controls]
    axes = [_values(v) for v in controls]
    size = math.prod(len(a) for a in axes)
    if size > MAX_GRID:
        raise ValueError(f"control grid has {size} points (max {MAX_GRID}); widen the steps")
    table = {}
    for idx in itertools.product(*(range(len(a)) for a in axes)):
        vals = {v.id: axes[k][i] for k, (v, i) in enumerate(zip(controls, idx, strict=True))}
        table[grid_key(list(idx))] = run(spec, vals)
    return {"controls": [v.id for v in controls], "axes": axes, "results": table}
