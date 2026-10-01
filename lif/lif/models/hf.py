"""Hugging Face Hub metadata client (official REST API, no scraping, no downloads).

Unauthenticated by default: discovery needs only public metadata, and the primary workload's HF_TOKEN is
not borrowed. Gated models are recorded but never auto-downloaded.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from lif.common import log

LOG = log.get("lif.hf")
API = "https://huggingface.co/api"

# "35B-A3B": the A-prefixed figure is ACTIVE params of an MoE, never the model size
_PARAMS_IN_NAME = re.compile(r"(?i)(?<![\d.])(?<![-_.]a)(\d+(?:\.\d+)?)\s*([bm])(?![a-z])")
_QUANT_IN_FILE = re.compile(r"(?i)(IQ\d_[A-Z]+|Q\d_K(?:_[SML])?|Q\d_\d|Q\d_K|BF16|F16|F32|MXFP4|Q8_0)")


class HFClient:
    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None, concurrency: int = 8):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = client or httpx.AsyncClient(timeout=20, headers=self.headers, follow_redirects=True)
        self.sem = asyncio.Semaphore(concurrency)

    async def _get(self, url: str, params: Any = None) -> Any:
        async with self.sem:
            for attempt in range(3):
                r = await self.client.get(url, params=params)
                if r.status_code == 429:
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                r.raise_for_status()
                return r.json()
        raise httpx.HTTPError(f"rate limited: {url}")

    async def search(self, *, pipeline_tag: str | None = None, library: str | None = None, search: str | None = None,
                     author: str | None = None, sort: str = "downloads", limit: int = 100) -> list[dict]:
        params: list[tuple[str, str]] = [("sort", sort), ("direction", "-1"), ("limit", str(limit)),
                                         ("full", "true"), ("config", "true")]
        if pipeline_tag:
            params.append(("pipeline_tag", pipeline_tag))
        if library:
            params.append(("library", library))
        if search:
            params.append(("search", search))
        if author:
            params.append(("author", author))
        return await self._get(f"{API}/models", params=params)

    async def model_info(self, model_id: str, revision: str | None = None) -> dict:
        url = f"{API}/models/{model_id}" + (f"/revision/{revision}" if revision else "")
        return await self._get(url, params={"blobs": "true"})


# ── metadata normalisation (deterministic) ────────────────────────────────────

def params_b(info: dict) -> float | None:
    st = info.get("safetensors") or {}
    if st.get("total"):
        return round(st["total"] / 1e9, 2)
    gg = info.get("gguf") or {}
    # In a vision repo the Hub's GGUF summary can describe the image projector (architecture
    # "clip"), not the language model: its parameter count is not the model's.
    if gg.get("total") and str(gg.get("architecture") or "").lower() != "clip":
        return round(gg["total"] / 1e9, 2)
    m = _PARAMS_IN_NAME.findall(info.get("id", "").split("/")[-1])
    if m:
        n, unit = m[-1]
        return round(float(n) / (1000 if unit.lower() == "m" else 1), 3)
    return None


def license_of(info: dict) -> str | None:
    card = info.get("cardData") or {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if not lic:
        for t in info.get("tags") or []:
            if t.startswith("license:"):
                return t.split(":", 1)[1]
    return lic


def gguf_files(info: dict) -> list[dict]:
    out = []
    for s in info.get("siblings") or []:
        f = s.get("rfilename", "")
        if f.endswith(".gguf") and "mmproj" not in f.lower() and not re.search(r"-\d{5}-of-\d{5}", f):
            q = _QUANT_IN_FILE.search(f)
            out.append({"file": f, "size": s.get("size"), "sha256": (s.get("lfs") or {}).get("sha256"),
                        "quant": q.group(1).upper() if q else None})
    return out


_MMPROJ_QUANT = re.compile(r"(?i)(?:^|[-_.])(Q8_0|BF16|F16|F32)(?:[-_.]|$)")
MMPROJ_PREFER = ("Q8_0", "F16", "BF16")       # never F32: 2x the memory of F16 for no CPU benefit


def mmproj_files(info: dict) -> list[dict]:
    """Image projector GGUFs (`*mmproj*.gguf`) shipped next to the language-model weights.

    Names vary by converter: mmproj-<Model>-Q8_0.gguf, mmproj-F16.gguf, <name>.mmproj-Q8_0.gguf,
    <name>-mmproj.gguf (no precision in the name → quant None, which the picker never chooses)."""
    out = []
    for s in info.get("siblings") or []:
        f = s.get("rfilename", "")
        if f.endswith(".gguf") and "mmproj" in f.lower():
            q = _MMPROJ_QUANT.search(f.rsplit("/", 1)[-1][:-5])
            out.append({"file": f, "size": s.get("size"), "sha256": (s.get("lfs") or {}).get("sha256"),
                        "quant": q.group(1).upper() if q else None})
    return out


def pick_mmproj(files: list[dict], prefer=MMPROJ_PREFER) -> dict | None:
    """Q8_0 → F16 → BF16, each only with a size and an LFS sha256. F32 and unlabeled are never picked."""
    for q in prefer:
        for f in files:
            if f.get("quant") == q and f.get("size") and f.get("sha256"):
                return dict(f)
    return None


def pick_gguf(files: list[dict], prefer=("Q4_K_M", "Q5_K_M", "Q8_0", "Q4_K_S", "Q6_K")) -> dict | None:
    by = {f["quant"]: f for f in files if f.get("quant")}
    for q in prefer:
        if q in by and by[q].get("size") and by[q].get("sha256"):
            return by[q]
    return None


def context_length(info: dict) -> int | None:
    cfg = info.get("config") or {}
    for k in ("max_position_embeddings", "n_positions", "max_seq_len", "seq_length"):
        if isinstance(cfg.get(k), int):
            return cfg[k]
    gg = info.get("gguf") or {}
    return gg.get("context_length")


def _gguf_arch(info: dict) -> str | None:
    a = (info.get("gguf") or {}).get("architecture")
    return None if str(a or "").lower() == "clip" else a


def normalize(info: dict, category: str) -> dict:
    cfg = info.get("config") or {}
    files = gguf_files(info)
    pick = pick_gguf(files)
    if pick is not None and category == "vision":
        pick = dict(pick)
        mm = pick_mmproj(mmproj_files(info))
        if mm:
            pick["mmproj"] = mm
    return {
        "model_id": info["id"],
        "revision": info.get("sha"),
        "category": category,
        "architecture": (cfg.get("architectures") or [None])[0] or _gguf_arch(info),
        "family": cfg.get("model_type") or _gguf_arch(info),
        "params_b": params_b(info),
        "pipeline_tag": info.get("pipeline_tag"),
        "library": info.get("library_name"),
        "license": license_of(info),
        "gated": bool(info.get("gated")),
        "trust_remote_code": "custom_code" in (info.get("tags") or []),
        "safetensors": bool(info.get("safetensors")),
        "gguf": bool(files),
        "gguf_pick": pick,
        # listing-stage hint: a `*mmproj*.gguf` sibling exists (None = siblings not in the listing)
        "mmproj_listed": (any("mmproj" in (x.get("rfilename") or "").lower() and
                              (x.get("rfilename") or "").endswith(".gguf") for x in info["siblings"])
                          if info.get("siblings") else None),
        "context_length": context_length(info),
        "downloads": info.get("downloads") or 0,
        "likes": info.get("likes") or 0,
        "last_modified": info.get("lastModified"),
        "created_at": info.get("createdAt"),
        "tags": [t for t in (info.get("tags") or []) if not t.startswith(("arxiv:", "dataset:", "region:"))][:25],
        "base_model": (info.get("cardData") or {}).get("base_model"),
        "num_layers": cfg.get("num_hidden_layers"),
        "num_kv_heads": cfg.get("num_key_value_heads") or cfg.get("num_attention_heads"),
        "head_dim": cfg.get("head_dim") or ((cfg.get("hidden_size") // cfg["num_attention_heads"])
                                            if cfg.get("hidden_size") and cfg.get("num_attention_heads") else None),
        "vocab_size": cfg.get("vocab_size"),
        "source": "huggingface",
    }
