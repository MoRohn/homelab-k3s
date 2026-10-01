"""Hardware-fit engine: will this model run here, safely, and on which tier?

Estimates are built from the budgets MEASURED on this DGX Spark (private/lif/docs/audit/GPU_BASELINE.md, local only),
not from "128 GB". Fitting in the pool is not the same as being safe:

  total = weights + KV cache (context × concurrency) + runtime overhead
  CPU tier    anonymous memory is what counts (weights stay mmap'd with --no-repack), and
              the weights must be small enough to decode at a useful speed on 10 A725 cores
  GPU now     total + safety reserve ≤ gpusched admissible (≈1 GiB today)
  GPU window  total + safety reserve ≤ admissible + GRAMZ unload (~34.6 GB measured), and
              only as a preemptible gpusched command job
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from lif.common import config

GRAMZ_UNLOAD_MIB = 34_600          # measured 2026-09-30 (gpusched live unload)
CPU_ANON_BUDGET_MIB = 1_536        # per CPU model: KV + compute buffers (anon) — measured 0.3–0.7 GiB
CPU_MAX_WEIGHTS_MIB = 6_144        # beyond this, CPU decode on A725 cores drops below ~8 tok/s
CPU_DECODE_GBPS = 50.0             # effective weight-streaming rate measured on the A725 set (2.4 GB × 21 tok/s)

BYTES_PER_PARAM = {"F32": 4.0, "F16": 2.0, "BF16": 2.0, "Q8_0": 1.07, "Q6_K": 0.82, "Q5_K_M": 0.71,
                   "Q4_K_M": 0.60, "Q4_K_S": 0.57, "MXFP4": 0.53, "FP8": 1.0, "INT4": 0.55}


@dataclass
class FitReport:
    verdict: str                  # fits_cpu | fits_gpu_now | fits_when_gramz_unloaded | no_fit
    weights_mib: int
    kv_mib: int
    overhead_mib: int
    total_mib: int
    anon_mib: int
    est_cpu_decode_tps: float | None
    reasons: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def kv_cache_mib(meta: dict, context: int, concurrency: int, kv_bytes: float = 1.0) -> int:
    """KV = 2 × layers × kv_heads × head_dim × tokens × bytes. kv_bytes 1.0 = q8_0 cache."""
    L, H, D = meta.get("num_layers"), meta.get("num_kv_heads"), meta.get("head_dim")
    tokens = context  # llama.cpp splits --ctx-size across slots; total tokens = context
    if L and H and D:
        return int(2 * L * H * D * tokens * kv_bytes / 2**20)
    # unknown architecture: conservative ~0.1 MiB/token/B-params heuristic
    return int((meta.get("params_b") or 8) * 0.1 * tokens * kv_bytes / 8)


def estimate(meta: dict, *, device: str = "auto", context: int = 8192, concurrency: int = 4,
             admissible_mib: float | None = None) -> FitReport:
    reasons: list[str] = []
    safety = int(float(config.get("gpu.safety_reserve_gb", 16)) * 1024)
    pb = meta.get("params_b")
    pick = meta.get("gguf_pick") or {}
    if pick.get("size"):
        weights = int(pick["size"] / 2**20)
    elif pb:
        weights = int(pb * 1e9 * BYTES_PER_PARAM.get((meta.get("precision") or "BF16").upper(), 2.0) / 2**20)
    else:
        return FitReport("no_fit", 0, 0, 0, 0, 0, None, ["parameter count unknown — cannot size safely"])
    kv = kv_cache_mib(meta, context, concurrency)
    overhead = 256 + int(weights * 0.05)
    total = weights + kv + overhead
    anon = kv + overhead
    tps = round(CPU_DECODE_GBPS * 1024 / max(weights, 1), 1)

    cpu_ok = bool(pick) and weights <= CPU_MAX_WEIGHTS_MIB and anon <= CPU_ANON_BUDGET_MIB
    if device in ("auto", "cpu"):
        if not pick:
            reasons.append("no GGUF artifact → not servable on the CPU tier (llama.cpp)")
        elif weights > CPU_MAX_WEIGHTS_MIB:
            reasons.append(f"weights {weights} MiB > CPU tier max {CPU_MAX_WEIGHTS_MIB} MiB (≈{tps} tok/s)")
        elif anon > CPU_ANON_BUDGET_MIB:
            reasons.append(f"KV+buffers {anon} MiB > CPU anon budget {CPU_ANON_BUDGET_MIB} MiB; reduce context")
        if cpu_ok:
            return FitReport("fits_cpu", weights, kv, overhead, total, anon, tps,
                             reasons + [f"CPU tier: ~{tps} tok/s est., {anon} MiB anonymous"])
        if device == "cpu":
            return FitReport("no_fit", weights, kv, overhead, total, anon, tps, reasons)

    adm = float(admissible_mib or 0)
    if total + safety <= adm:
        return FitReport("fits_gpu_now", weights, kv, overhead, total, anon, None,
                         reasons + [f"GPU: {total} MiB + {safety} MiB reserve ≤ admissible {adm:.0f} MiB"])
    if total + safety <= adm + GRAMZ_UNLOAD_MIB:
        return FitReport("fits_when_gramz_unloaded", weights, kv, overhead, total, anon, None,
                         reasons + [f"GPU only while GRAMZ is unloaded: {total} MiB + {safety} MiB reserve ≤ "
                                    f"{adm:.0f} + {GRAMZ_UNLOAD_MIB} MiB (preemptible gpusched job)"])
    reasons.append(f"GPU: {total} MiB + {safety} MiB reserve > {adm:.0f} + {GRAMZ_UNLOAD_MIB} MiB even with "
                   "GRAMZ unloaded")
    return FitReport("no_fit", weights, kv, overhead, total, anon, None, reasons)
