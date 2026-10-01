#!/usr/bin/env python3
"""Start a serving manifest's exact llama-server arguments in a throwaway pod before applying it.

The model servers use `strategy: Recreate`, so a flag the pinned llama.cpp rejects means no chat until
someone rolls back (2026-10-01: `--mlock` was removed in this build, and tier0 crash-looped). This runs the
same image and args with a small context and one slot (a little anon memory; the weights are shared page
cache), waits for "listening" or an error, prints the server's warnings, and deletes the pod.

    python3 scripts/smoke-server-args.py deploy/k8s/serving/tier0.yaml      # exit 0 = safe to apply
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

NS = "ai-serving"


def pod_for(manifest: Path) -> dict:
    dep = next(d for d in yaml.safe_load_all(manifest.read_text()) if d and d.get("kind") == "Deployment")
    spec = dep["spec"]["template"]["spec"]
    c = dict(spec["containers"][0])
    args = list(c["args"])
    for flag, value in (("--ctx-size", "1024"), ("--parallel", "1")):
        if flag in args:
            args[args.index(flag) + 1] = value
    name = f"{dep['metadata']['name']}-args-smoke"
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": NS, "labels": {"app": "args-smoke"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "securityContext": spec.get("securityContext", {}), "volumes": spec.get("volumes", []),
                     "containers": [{**{k: c[k] for k in ("name", "image", "command", "securityContext",
                                                          "volumeMounts") if k in c},
                                     "args": args,
                                     "resources": {"requests": {"cpu": "500m", "memory": "3584Mi"},
                                                   "limits": {"cpu": "8", "memory": "3584Mi"}}}]}}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    pod = pod_for(Path(sys.argv[1]))
    name = pod["metadata"]["name"]
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(pod), text=True, check=True,
                   capture_output=True)
    logs, verdict = "", None
    try:
        for _ in range(60):
            time.sleep(3)
            logs = subprocess.run(["kubectl", "-n", NS, "logs", name], capture_output=True, text=True).stdout
            if "listening on" in logs:
                verdict = 0
                break
            if "error:" in logs or "invalid argument" in logs:
                verdict = 1
                break
            phase = subprocess.run(["kubectl", "-n", NS, "get", "pod", name, "-o", "jsonpath={.status.phase}"],
                                   capture_output=True, text=True).stdout
            if phase in ("Failed", "Succeeded"):
                verdict = 1
                break
    finally:
        subprocess.run(["kubectl", "-n", NS, "delete", "pod", name, "--wait=false"], capture_output=True)
    for line in logs.splitlines():
        if any(w in line.lower() for w in ("error", "invalid", "warn", "listening")):
            print(line)
    if verdict is None:
        print("TIMEOUT: no 'listening' within 180 s")
        return 1
    print("OK: the server starts with these arguments" if verdict == 0 else "FAIL: do not apply this manifest")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
