"""Minimal Kubernetes API client (in-cluster ServiceAccount; RBAC limited to ai-serving).

The controller only ever touches LIF objects in its own namespaces: scaling model
Deployments, creating download/GC Jobs and short-lived candidate servers. It has no
rights over BNN, gpusched or anything outside ai-serving/ai-batch.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class K8s:
    def __init__(self, base: str | None = None, token: str | None = None, verify: str | bool | None = None):
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        self.base = base or (f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}" if host else "")
        self.token = token or ((SA / "token").read_text().strip() if (SA / "token").exists() else "")
        ca = SA / "ca.crt"
        self.client = httpx.AsyncClient(base_url=self.base, timeout=15,
                                        verify=verify if verify is not None else (str(ca) if ca.exists() else True),
                                        headers={"Authorization": f"Bearer {self.token}"} if self.token else {})

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.token)

    async def _req(self, method: str, path: str, **kw) -> Any:
        r = await self.client.request(method, path, **kw)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"k8s {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    # deployments
    async def deployment(self, ns: str, name: str) -> dict | None:
        return await self._req("GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")

    async def deployments(self, ns: str, selector: str = "") -> list[dict]:
        out = await self._req("GET", f"/apis/apps/v1/namespaces/{ns}/deployments",
                              params={"labelSelector": selector} if selector else None)
        return (out or {}).get("items", [])

    async def scale(self, ns: str, name: str, replicas: int) -> dict:
        return await self._req("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale",
                               json={"spec": {"replicas": replicas}},
                               headers={"Content-Type": "application/merge-patch+json"})

    async def apply_deployment(self, ns: str, body: dict) -> dict:
        name = body["metadata"]["name"]
        if await self.deployment(ns, name):
            return await self._req("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", json=body)
        return await self._req("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", json=body)

    async def delete_deployment(self, ns: str, name: str) -> None:
        await self._req("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                        params={"propagationPolicy": "Background"})

    # services
    async def apply_service(self, ns: str, body: dict) -> dict:
        name = body["metadata"]["name"]
        if await self._req("GET", f"/api/v1/namespaces/{ns}/services/{name}"):
            return {}
        return await self._req("POST", f"/api/v1/namespaces/{ns}/services", json=body)

    async def delete_service(self, ns: str, name: str) -> None:
        await self._req("DELETE", f"/api/v1/namespaces/{ns}/services/{name}")

    # jobs
    async def job(self, ns: str, name: str) -> dict | None:
        return await self._req("GET", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}")

    async def create_job(self, ns: str, body: dict) -> dict:
        return await self._req("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", json=body)

    async def delete_job(self, ns: str, name: str) -> None:
        await self._req("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}",
                        params={"propagationPolicy": "Background"})

    async def pod_logs(self, ns: str, selector: str, tail: int = 50) -> str:
        pods = await self._req("GET", f"/api/v1/namespaces/{ns}/pods", params={"labelSelector": selector})
        items = (pods or {}).get("items", [])
        if not items:
            return ""
        name = items[-1]["metadata"]["name"]
        r = await self.client.get(f"/api/v1/namespaces/{ns}/pods/{name}/log", params={"tailLines": tail})
        return r.text if r.status_code == 200 else ""
