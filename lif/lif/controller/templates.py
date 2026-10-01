"""Manifests the controller creates. Kept identical in spirit to deploy/k8s/serving/*.yaml."""
from __future__ import annotations

import re

LLAMA_IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:6d607629e3dd5e85f45c43d1494648126cb3f93f2122c9cd53f43242c94cde14"
CURL_IMAGE = "curlimages/curl:8.10.1"
A725_MASK = "7C1F"          # CPUs 0-4,10-14 (Cortex-A725). X925 cores stay free for BNN/ffmpeg.


# HF repo ids and filenames are UNTRUSTED input. Anything that becomes a filesystem path
# or a container argument must match these, and may never contain "..".
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,250}\.gguf$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_profile(p: dict) -> None:
    if not _REPO.match(p.get("hf_repo", "")) or ".." in p["hf_repo"]:
        raise ValueError(f"unsafe hf_repo {p.get('hf_repo')!r}")
    if not _FILE.match(p.get("file", "")) or ".." in p["file"]:
        raise ValueError(f"unsafe file name {p.get('file')!r}")
    if not _SHA40.match(p.get("revision", "")):
        raise ValueError("revision must be a 40-char commit sha")
    if p.get("sha256") is not None and not _SHA256.match(p["sha256"]):
        raise ValueError("sha256 must be 64 hex chars")


def model_path(p: dict) -> str:
    return f"/models/{p['hf_repo']}/{p['revision']}/{p['file']}"


def llama_args(p: dict) -> list[str]:
    args = ["--model", model_path(p), "--host", "0.0.0.0", "--port", "8080",
            "--threads", "10", "--threads-batch", "10", "--cpu-mask", A725_MASK, "--cpu-strict", "1",
            "--metrics", "--no-repack", "--no-webui",
            "--ctx-size", str(p.get("context", 8192)), "--parallel", str(p.get("concurrency", 2)),
            "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--flash-attn", "on"]
    if p.get("category") in ("embedding", "reranking"):
        args += ["--embedding", "--pooling", "rank" if p["category"] == "reranking" else "last",
                 "--ubatch-size", "512", "--batch-size", "512"]
    else:
        args += ["--jinja"]
    return args


def model_server(name: str, profile_name: str, p: dict, *, priority: str = "ai-critical",
                 role: str = "model-server") -> tuple[dict, dict]:
    validate_profile(p)
    mem = f"{int(p.get('memory_budget_mb', 4096))}Mi"
    labels = {"app": name, "lif.dev/profile": profile_name, "lif.dev/role": role}
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": "ai-serving", "labels": {**labels, "lif.dev/tier": "cpu",
                                                                         "app.kubernetes.io/managed-by": "lif-controller"}},
        "spec": {
            "replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": labels}, "spec": {
                "priorityClassName": priority, "automountServiceAccountToken": False, "enableServiceLinks": False,
                "terminationGracePeriodSeconds": 30,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "llama", "image": LLAMA_IMAGE, "command": ["/app/llama-server"], "args": llama_args(p),
                    "ports": [{"name": "http", "containerPort": 8080}],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "500m", "memory": mem}, "limits": {"cpu": "8", "memory": mem}},
                    "startupProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 5,
                                     "failureThreshold": 60},
                    "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 10},
                    "livenessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 30,
                                      "timeoutSeconds": 10, "failureThreshold": 4},
                    "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True},
                                     {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": "model-store", "readOnly": True}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": name, "namespace": "ai-serving", "labels": labels},
           "spec": {"selector": {"app": name}, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}}
    return dep, svc


def download_job(name: str, p: dict) -> dict:
    """Resumable, sha256-verified, atomic download of one pinned file into the model store."""
    validate_profile(p)
    if not p.get("sha256"):
        raise ValueError("refusing to download without a sha256")
    script = r'''
set -eu
d="/models/$REPO/$REV"; f="$d/$FILE"; mkdir -p "$(dirname "$f")"
if [ -f "$f" ] && echo "$SHA  $f" | sha256sum -c -s; then echo "ok (cached)"; exit 0; fi
curl -fL --retry 5 --retry-delay 5 -C - -o "$f.part" "https://huggingface.co/$REPO/resolve/$REV/$FILE"
if echo "$SHA  $f.part" | sha256sum -c -s; then mv "$f.part" "$f"; echo ok
else mv "$f.part" "$f.bad"; echo "SHA256 MISMATCH (quarantined)"; exit 3; fi
'''
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": "ai-serving",
                     "labels": {"lif.dev/role": "download", "app.kubernetes.io/managed-by": "lif-controller"}},
        "spec": {"backoffLimit": 3, "ttlSecondsAfterFinished": 86400, "activeDeadlineSeconds": 14400,
                 "template": {"metadata": {"labels": {"lif.dev/role": "download", "job": name}}, "spec": {
                     "priorityClassName": "ai-maintenance", "restartPolicy": "Never",
                     "automountServiceAccountToken": False,
                     "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                     "containers": [{"name": "fetch", "image": CURL_IMAGE, "command": ["/bin/sh", "-c", script],
                                     "env": [{"name": "REPO", "value": p["hf_repo"]},
                                             {"name": "REV", "value": p["revision"]},
                                             {"name": "FILE", "value": p["file"]},
                                             {"name": "SHA", "value": p["sha256"]}],
                                     "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                                   "limits": {"memory": "256Mi"}},
                                     "volumeMounts": [{"name": "models", "mountPath": "/models"}]}],
                     "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": "model-store"}}]}}}}


def gc_job(name: str, paths: list[str]) -> dict:
    """Delete model artifacts (paths relative to /models). Only ever called for artifacts
    the registry says are not production, rollback-critical, pinned or referenced."""
    for path in paths:
        parts = path.split("/")
        if len(parts) < 3 or ".." in parts or not _REPO.match("/".join(parts[:2])):
            raise ValueError(f"unsafe gc path {path!r}")
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": "ai-serving", "labels": {"lif.dev/role": "gc"}},
        "spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 3600, "template": {"spec": {
            "priorityClassName": "ai-maintenance", "restartPolicy": "Never", "automountServiceAccountToken": False,
            "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
            "containers": [{"name": "gc", "image": CURL_IMAGE, "command": ["/bin/sh", "-c", 'rm -fv -- "$@"', "rm"] + [f"/models/{p}" for p in paths],
                            "resources": {"limits": {"memory": "32Mi"}},
                            "volumeMounts": [{"name": "models", "mountPath": "/models"}]}],
            "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": "model-store"}}]}}}}
