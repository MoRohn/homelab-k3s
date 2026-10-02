"""Read-only state collection for operational explanations (spec §55, §100).

`gpu_state()` gathers what explains GPU utilization on this host: gpusched's metrics (utilization,
leases, primary-workload state, residents), host MemAvailable and the pods that request a GPU.
Every source is optional; a source that fails is listed under `errors` and the builder explains with
what it has. Nothing here changes state, and nothing leaves the host.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

GPU_RESOURCE = "nvidia.com/gpu"


async def _run(*cmd: str, timeout: float = 8.0) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"{cmd[0]} timed out") from None
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {err.decode()[:200]}")
    return out.decode()


def _mem_available_mib(path: str = "/proc/meminfo") -> float:
    with open(path) as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError("MemAvailable not in /proc/meminfo")


def pods_from_json(doc: dict) -> list[dict]:
    out = []
    for p in doc.get("items", []):
        spec, st = p.get("spec") or {}, p.get("status") or {}
        wants_gpu = any(GPU_RESOURCE in ((c.get("resources") or {}).get("limits") or {})
                        or GPU_RESOURCE in ((c.get("resources") or {}).get("requests") or {})
                        for c in spec.get("containers", []))
        wants_gpu = wants_gpu or bool(spec.get("runtimeClassName") == "nvidia")
        if not wants_gpu:
            continue
        cond = next((c for c in st.get("conditions", []) if c.get("type") == "PodScheduled"
                     and c.get("status") == "False"), {})
        out.append({"ns": p["metadata"].get("namespace"), "name": p["metadata"].get("name"),
                    "phase": st.get("phase"), "gpu": True, "unschedulable": bool(cond),
                    "reason": cond.get("reason", ""), "message": cond.get("message", "")})
    return out


_SNAP: tuple[float, Any] = (0.0, None)
SNAP_TTL = 5.0


async def gpusched_snapshot(max_age: float = SNAP_TTL):
    """One gpusched read shared by the router's resource probe and the GPU collector, cached briefly.
    The watcher's HTTP client is closed after each read."""
    global _SNAP
    if _SNAP[1] is not None and time.time() - _SNAP[0] < max_age:
        return _SNAP[1]
    from lif.gpu.state import GpuStateWatcher
    w = GpuStateWatcher()
    try:
        snap = await asyncio.wait_for(w.refresh(), 3.0)
    finally:
        await w.client.aclose()
    _SNAP = (time.time(), snap)
    return snap


async def gpu_state() -> dict[str, Any]:
    st: dict[str, Any] = {"collected_at": time.time(), "gpu": {}, "blerbz": {}, "pods": [], "residents": {},
                          "errors": {}}
    try:
        snap = await gpusched_snapshot()
        if snap.reachable:
            st["gpu"]["util_percent"] = snap.gpu_util_percent
            st["gpu"]["util_source"] = "gpusched_gpu_util_percent"
            st["gpu"]["mem_available_mib"] = snap.mem_available_mib or None
            st["blerbz"] = {"state": snap.state.name, "reason": snap.reason,
                            "production_leases": snap.production_leases, "background_leases": snap.background_leases}
            st["residents"] = dict(snap.residents_loaded)
        else:
            st["errors"]["gpusched"] = snap.reason
    except Exception as e:                          # noqa: BLE001 - a missing source must not stop the explanation
        st["errors"]["gpusched"] = f"{type(e).__name__}: {e}"[:200]
    if "util_percent" not in st["gpu"]:
        try:
            out = await _run("nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits")
            st["gpu"]["util_percent"] = float(out.split()[0])
            st["gpu"]["util_source"] = "nvidia-smi utilization.gpu"
        except Exception as e:                      # noqa: BLE001
            st["errors"]["nvidia-smi"] = str(e)[:200]
    if not st["gpu"].get("mem_available_mib"):
        try:
            st["gpu"]["mem_available_mib"] = _mem_available_mib()
        except Exception as e:                      # noqa: BLE001
            st["errors"]["meminfo"] = str(e)[:200]
    try:
        st["pods"] = pods_from_json(json.loads(await _run("kubectl", "get", "pods", "-A", "-o", "json")))
    except Exception as e:                          # noqa: BLE001
        st["errors"]["kubectl"] = str(e)[:200]
    return st
