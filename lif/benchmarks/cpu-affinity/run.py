#!/usr/bin/env python3
"""CPU-affinity A/B for the CPU tiers: which cores and thread count serve chat fastest?

tier0 runs on the A725 efficiency cores (CPUs 0-4,10-14, mask 7C1F) by decision
(cpu-tiers-for-local-inference) so the X925 cores stay free for the primary workload. This measures
what that costs. Each arm is one short llama-bench Job (same image, model and cache settings as tier0:
--no-repack, flash attention, q8_0 KV) with its own memory limit, so it can't disturb tier0's cgroup.

Gate (re-checked before every arm): host MemAvailable >= 8192 MiB and primary workload not HIGH/IMMINENT
(an unreadable state counts as busy). Driving CPU inference below that headroom thrashes the host.

    python3 benchmarks/cpu-affinity/run.py              # wait for the gate, run every arm
    python3 benchmarks/cpu-affinity/run.py --check      # print the gate and exit
    ARMS=a725x10,x925x10 python3 benchmarks/cpu-affinity/run.py

Results go to private/lif/benchmarks/ (local only): they are measured on the production host.
Run from lif/ with kubectl pointed at the cluster.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = REPO / "private/lif/benchmarks"
NS = "ai-serving"
MIN_HEADROOM_MIB = 8192
sys.path.insert(0, str(HERE.parents[1]))
from lif.controller.templates import LLAMA_IMAGE  # noqa: E402

MODEL = ("/models/unsloth/Qwen3-4B-Instruct-2507-GGUF/a06e946bb6b655725eafa393f4a9745d460374c9/"
         "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
# name → (cpu mask, threads). A725 = CPUs 0-4,10-14 (two 5-core clusters); X925 = CPUs 5-9,15-19.
ARMS = {
    "a725x10": ("0x7C1F", 10),      # today
    "a725x5": ("0x1F", 5),          # one A725 cluster
    "x925x10": ("0xF83E0", 10),
    "x925x5": ("0x3E0", 5),         # one X925 cluster
    "x925x8": ("0xF83E0", 8),       # leave two X925 cores free
    # Repacked weights unlock llama.cpp's ARM GEMM kernels (prompt processing is compute-bound: a 1,700-token
    # prompt read at ~26 tok/s on tier0, 2026-10-01) at the cost of ~2.4 GiB anonymous (unreclaimable) memory,
    # the same as an mlock'd mmap. These arms need that much more headroom.
    "a725x10-repack": ("0x7C1F", 10),
    "x925x10-repack": ("0xF83E0", 10),
}
REPACK_EXTRA_MIB = 2560


def mem_available_mib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return 0.0


def workload_state() -> str:
    """Primary-workload state as the gateway sees it (/v1/health needs no key)."""
    try:
        out = subprocess.run(
            ["kubectl", "-n", "ai-system", "exec", "deploy/gateway", "--", "python", "-c",
             "import httpx;print(httpx.get('http://localhost:8080/v1/health',timeout=5).text)"],
            capture_output=True, text=True, timeout=30, check=True).stdout
        return str((json.loads(out).get("blerbz") or {}).get("state") or "UNKNOWN").upper()
    except Exception:
        return "UNKNOWN"


def gate(extra_mib: int = 0) -> tuple[bool, str]:
    st, mem = workload_state(), mem_available_mib()
    need = MIN_HEADROOM_MIB + extra_mib
    ok = st in ("LOW", "MODERATE") and mem >= need
    return ok, f"primary workload {st}, MemAvailable {mem:.0f} MiB (need ≥ {need}, LOW/MODERATE)"


def job(name: str, mask: str, threads: int, repack: bool = False) -> dict:
    mem = "6144Mi" if repack else "3584Mi"
    args = ["bench", "-m", MODEL, "-C", mask, "--cpu-strict", "1", "-t", str(threads), "--repack", str(int(repack)),
            "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "-p", "512", "-n", "128", "-r", "3", "-o", "json"]
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": NS, "labels": {"app": "cpu-affinity-bench"}},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 600, "activeDeadlineSeconds": 900,
                 "template": {"metadata": {"labels": {"app": "cpu-affinity-bench"}}, "spec": {
                     "restartPolicy": "Never", "priorityClassName": "ai-experimental",
                     "automountServiceAccountToken": False, "enableServiceLinks": False,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{
                         "name": "bench", "image": LLAMA_IMAGE, "command": ["/app/llama"], "args": args,
                         "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}},
                         # weights 2,381 MiB + a 640-token context and buffers + headroom
                         "resources": {"requests": {"cpu": "500m", "memory": mem},
                                       "limits": {"cpu": "10", "memory": mem}},
                         "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True}]}],
                     "volumes": [{"name": "models",
                                  "persistentVolumeClaim": {"claimName": "model-store", "readOnly": True}}]}}}}


def kubectl(*a: str, stdin: str | None = None, timeout: int = 60) -> str:
    return subprocess.run(["kubectl", *a], input=stdin, capture_output=True, text=True, timeout=timeout,
                          check=True).stdout


def run_arm(arm: str) -> list[dict]:
    mask, threads = ARMS[arm]
    name = f"cpu-affinity-{arm}-{int(time.time())}"
    kubectl("apply", "-f", "-", stdin=json.dumps(job(name, mask, threads, repack=arm.endswith("-repack"))))
    try:
        kubectl("-n", NS, "wait", "--for=condition=complete", f"job/{name}", "--timeout=900s", timeout=960)
        logs = kubectl("-n", NS, "logs", f"job/{name}", timeout=60)
    finally:
        subprocess.run(["kubectl", "-n", NS, "delete", "job", name, "--wait=false"], capture_output=True)
    start = logs.index("[")             # llama-bench prints load logs before the JSON array
    return json.loads(logs[start:logs.rindex("]") + 1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print the gate and exit")
    ap.add_argument("--max-wait-min", type=float, default=120)
    a = ap.parse_args()
    ok, why = gate()
    if a.check:
        print(("OPEN: " if ok else "CLOSED: ") + why)
        sys.exit(0 if ok else 1)
    arms = [x for x in (os.environ.get("ARMS") or ",".join(ARMS)).split(",") if x]
    results, deadline = {}, time.time() + a.max_wait_min * 60
    for arm in arms:
        while not (g := gate(REPACK_EXTRA_MIB if arm.endswith("-repack") else 0))[0]:
            if time.time() > deadline:
                print(f"gave up waiting: {g[1]}", flush=True)
                break
            print(f"  waiting: {g[1]}", flush=True)
            time.sleep(60)
        else:
            print(f"{arm}: {g[1]}", flush=True)
            rows = run_arm(arm)
            results[arm] = {"mask": ARMS[arm][0], "threads": ARMS[arm][1], "repack": arm.endswith("-repack"),
                            "gate": g[1], "rows": [
                {k: r.get(k) for k in ("n_prompt", "n_gen", "avg_ts", "stddev_ts", "n_threads", "cpu_mask")}
                for r in rows]}
            for r in results[arm]["rows"]:
                kind = f"pp{r['n_prompt']}" if r["n_prompt"] else f"tg{r['n_gen']}"
                print(f"  {kind}: {r['avg_ts']:.1f} ± {r['stddev_ts']:.1f} tok/s (n=3)", flush=True)
            continue
        break
    if results:
        OUT.mkdir(parents=True, exist_ok=True)
        f = OUT / f"cpu-affinity-{time.strftime('%Y%m%d-%H%M')}.json"
        f.write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "model": MODEL, "results": results}, indent=2))
        print(f"wrote {f}")


if __name__ == "__main__":
    main()
