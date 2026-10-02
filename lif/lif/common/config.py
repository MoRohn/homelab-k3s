"""Load platform config (config/lif.yaml) and the model catalogue (config/models.yaml).

Paths come from LIF_CONFIG / LIF_MODELS so the same code runs from the repo, from
tests, and from the lif-config ConfigMap mounted at /etc/lif.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def _path(env: str, default: str) -> Path:
    if os.environ.get(env):
        return Path(os.environ[env])
    mounted = Path("/etc/lif") / default
    return mounted if mounted.exists() else REPO_CONFIG / default


@lru_cache(maxsize=None)
def platform() -> dict[str, Any]:
    return yaml.safe_load(_path("LIF_CONFIG", "lif.yaml").read_text())


@lru_cache(maxsize=None)
def catalogue() -> dict[str, Any]:
    return yaml.safe_load(_path("LIF_MODELS", "models.yaml").read_text())


def get(dotted: str, default: Any = None) -> Any:
    node: Any = platform()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def secret(name: str) -> str | None:
    """Read a secret from the environment or from /var/run/lif/secrets/<name>."""
    if os.environ.get(name):
        return os.environ[name]
    p = Path(os.environ.get("LIF_SECRETS_DIR", "/var/run/lif/secrets")) / name
    return p.read_text().strip() if p.exists() else None


def keys(name: str) -> dict[str, str]:
    """key → client name, from the `name:key` lines of secret `name`. Lines with a blank name or key
    are skipped: an empty key would otherwise authenticate a bare `Authorization: Bearer `."""
    out = {}
    for line in (secret(name) or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            n, k = (s.strip() for s in line.split(":", 1))
            if n and k:
                out[k] = n
    return out


def internal_ok(request) -> bool:
    """Control endpoints between LIF services carry X-LIF-Internal (Secret LIF_INTERNAL_KEY)."""
    import hmac
    want = secret("LIF_INTERNAL_KEY") or ""
    got = request.headers.get("x-lif-internal", "")
    return bool(want) and hmac.compare_digest(want, got)
