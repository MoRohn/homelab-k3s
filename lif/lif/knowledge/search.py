"""Hybrid retrieval: full text (FTS5/BM25) + metadata filters + graph importance + recency + pinning,
plus optional semantic embeddings from the LIF embedding alias.

Embeddings are an enhancement, never a dependency: when the gateway is unreachable (offline, model
shed by the memory guard) search silently runs lexical + graph only and says so in `retrieval`.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import time
from typing import Any

import httpx

from lif.knowledge.store import Store

WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "why", "what", "which",
        "how", "did", "we", "do", "does", "this", "that", "with", "by", "be", "it", "as", "at", "from", "our",
        "should", "can", "would", "will", "i", "you", "me", "my", "now", "any", "has", "have", "been"}


def terms(text: str) -> list[str]:
    out = []
    for w in WORD.findall(text.lower()):
        for part in re.split(r"[._-]+", w):
            if len(part) > 1 and part not in STOP and part not in out:
                out.append(part)
    return out


def fts_query(text: str) -> str:
    ts = terms(text)
    return " OR ".join(f'"{t}"*' for t in ts) if ts else '""'


class Embedder:
    """Calls the gateway's OpenAI-compatible /v1/embeddings. Disabled for 5 min after a failure."""

    def __init__(self, url: str | None = None, key: str | None = None, model: str = "local/embedding"):
        self.url = (url or os.environ.get("LIF_KNOWLEDGE_EMBED_URL") or "").rstrip("/")
        self.key = key or os.environ.get("LIF_API_KEY", "")
        self.model = model
        self.down_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.url) and time.time() >= self.down_until

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.enabled or not texts:
            return None
        try:
            r = httpx.post(f"{self.url}/v1/embeddings", timeout=10,
                           headers={"Authorization": f"Bearer {self.key}", "X-LIF-Workload": "knowledge-search",
                                    "X-LIF-Data-Class": "CONFIDENTIAL"},
                           json={"model": self.model, "input": [t[:2000] for t in texts]})
            r.raise_for_status()
            return [d["embedding"] for d in r.json()["data"]]
        except Exception:
            self.down_until = time.time() + 300
            return None


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


class Search:
    def __init__(self, store: Store, embedder: Embedder | None = None):
        self.s = store
        self.emb = embedder if embedder is not None else Embedder()

    def _vectors(self, keys: list[str]) -> dict[str, list[float]]:
        if not self.emb.enabled or not keys:
            return {}
        have = {r["key"]: r for r in self.s.db.q(
            "SELECT * FROM embeddings WHERE key IN (%s)" % ",".join("?" * len(keys)), tuple(keys))}
        objs = {k: self.s.get(k) for k in keys}
        todo = [k for k in keys if objs[k] and (k not in have or have[k]["sha"] != objs[k]["sha"])]
        if todo:
            vecs = self.emb.embed([f"{objs[k]['title']}\n{objs[k]['body'][:1500]}" for k in todo])
            if vecs is None:
                return {}
            for k, v in zip(todo, vecs):
                self.s.db.x("INSERT OR REPLACE INTO embeddings(key,sha,vec) VALUES(?,?,?)",
                            (k, objs[k]["sha"], json.dumps(v)))
                have[k] = {"vec": json.dumps(v)}
        return {k: json.loads(have[k]["vec"]) for k in keys if k in have}

    def search(self, text: str, type: str | None = None, repo: str | None = None, status: str | None = None,
               project: str | None = None, limit: int = 20, include_packages: bool = True, log: bool = True) -> dict:
        t0 = time.perf_counter()
        hits = self.s.fts(fts_query(text), 300) if terms(text) else []
        # embedded passages/records count toward their parent object, keeping the best snippet
        agg: dict[str, dict] = {}
        for h in hits:
            top = h["key"].split("^", 1)[0]
            lex = -h["rank"]
            a = agg.setdefault(top, {"lex": 0.0, "snippet": h["snippet"], "matched": []})
            if lex > a["lex"]:
                a["lex"], a["snippet"] = lex, h["snippet"]
            if "^" in h["key"]:
                a["matched"].append(h["key"])
        # exact id / title match
        for o in self.s.objects("sub IS NULL AND (id=? OR lower(title)=?)", (text.strip(), text.strip().lower())):
            agg.setdefault(o["key"], {"lex": 0.0, "snippet": "", "matched": []})["lex"] += 50
        retrieval = ["fulltext", "graph", "recency"]
        keys = list(agg)
        objs = {k: self.s.get(k) for k in keys}
        objs = {k: o for k, o in objs.items() if o is not None}
        if type:
            from lif.knowledge.graph import Graph
            allowed = Graph(self.s)._subtypes([t.strip() for t in type.split(",")])
            objs = {k: o for k, o in objs.items() if o["type_name"] in allowed}
        if repo:
            objs = {k: o for k, o in objs.items() if o["repo"] == repo}
        if status:
            objs = {k: o for k, o in objs.items() if o["status"] in status.split(",")}
        if project:
            objs = {k: o for k, o in objs.items() if o["project"] == project}
        if not include_packages:
            objs = {k: o for k, o in objs.items() if o["source"] == "workspace"}
        sem: dict[str, float] = {}
        if self.emb.enabled and objs:
            qv = self.emb.embed([text])
            vecs = self._vectors(list(objs)) if qv else {}
            if qv and vecs:
                sem = {k: _cos(qv[0], v) for k, v in vecs.items()}
                retrieval.insert(1, "semantic")
        maxlex = max([agg[k]["lex"] for k in objs] or [1]) or 1
        today = dt.date.today()
        indeg = {r["dst"]: r["n"] for r in self.s.db.q(
            "SELECT dst, count(*) n FROM edges WHERE dst IS NOT NULL AND kind IN ('field','inverse') GROUP BY dst")}
        results = []
        for k, o in objs.items():
            lex = agg[k]["lex"] / maxlex
            age = (today - dt.date.fromisoformat(o["updated"])).days if o["updated"] else 365
            recency = 1 / (1 + max(age, 0) / 90)
            importance = min(1.0, math.log1p(indeg.get(k, 0)) / 3)
            pinned = 1.0 if o["fields"].get("pinned") else 0.0
            penalty = 0.5 if o["status"] in ("superseded", "deprecated", "retracted", "invalidated", "rejected") else 1
            score = (0.55 * lex + 0.2 * sem.get(k, 0) + 0.12 * importance + 0.08 * recency + 0.05 * pinned) * penalty
            results.append({"key": k, "type": o["type_name"], "title": o["title"], "status": o["status"],
                            "repo": o["repo"], "path": o["path"], "updated": o["updated"],
                            "score": round(score, 4), "snippet": agg[k]["snippet"], "matched": agg[k]["matched"][:3]})
        results.sort(key=lambda r: -r["score"])
        if log:                          # interactive searches only: they drive tool/view suggestions
            self.s.log_query("search:" + " ".join(sorted(terms(text))) + (f" type={type}" if type else ""))
        _observe(time.perf_counter() - t0)
        return {"query": text, "retrieval": retrieval, "results": results[:limit], "total": len(results)}


try:
    from prometheus_client import Histogram
    _LAT = Histogram("lif_knowledge_query_seconds", "Knowledge search latency",
                     buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2))
except Exception:                       # pragma: no cover
    _LAT = None


def _observe(sec: float) -> None:
    if _LAT is not None:
        _LAT.observe(sec)
