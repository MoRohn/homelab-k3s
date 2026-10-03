"""Web search (SearXNG, self-hosted) and evidence ranking.

`SearxngProvider.search(q)` calls the in-cluster SearXNG JSON API. SearXNG fans the query out to public
engines without an account or key; the query is the only thing it forwards. `rank(q, hits)` orders hits
by term overlap (BM25-style), engine agreement, recency and source quality, with at most two per domain.

The provider is an interface so another backend (a keyed API, a second SearXNG) can be added as a
fallback without touching the callers.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlparse

import httpx

from lif.common import log

LOG = log.get("lif.web.search")


@dataclass
class Hit:
    title: str
    url: str
    snippet: str
    published: datetime | None = None
    engines: tuple[str, ...] = ()
    score: float = 0.0
    passages: list[str] = field(default_factory=list)      # filled when the page is read

    @property
    def domain(self) -> str:
        host = urlparse(self.url).hostname or ""
        return host[4:] if host.startswith("www.") else host


@dataclass
class SearchResult:
    hits: list[Hit]
    answers: list[str] = field(default_factory=list)       # engine instant answers (SearXNG `answers`)
    provider: str = ""
    ms: float = 0.0
    error: str | None = None


class SearchProvider(Protocol):
    name: str

    async def search(self, q: str, *, time_range: str | None = None, news: bool = False) -> SearchResult: ...


class SearxngProvider:
    name = "searxng"

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, timeout: float = 3.0,
                 language: str = "en-US"):
        self.base = base_url.rstrip("/")
        self.timeout, self.language = timeout, language
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def search(self, q: str, *, time_range: str | None = None, news: bool = False) -> SearchResult:
        params = {"q": q, "format": "json", "language": self.language, "safesearch": "1",
                  "categories": "news,general" if news else "general"}
        if time_range:
            params["time_range"] = time_range
        t0 = time.perf_counter()
        try:
            r = await self.client.get(f"{self.base}/search", params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return SearchResult([], provider=self.name, ms=(time.perf_counter() - t0) * 1000,
                                error=f"{type(e).__name__}: {str(e)[:120]}")
        hits = []
        for x in data.get("results") or []:
            url, title = str(x.get("url") or ""), _clean(x.get("title"))
            if not url.startswith(("http://", "https://")) or not title:
                continue
            hits.append(Hit(title, url, _clean(x.get("content"))[:600], _date(x.get("publishedDate")),
                            tuple(x.get("engines") or ([x["engine"]] if x.get("engine") else []))))
        answers = [_clean(a.get("answer") if isinstance(a, dict) else a) for a in data.get("answers") or []]
        for box in data.get("infoboxes") or []:
            txt = _clean(box.get("content"))
            if txt:
                answers.append(f"{_clean(box.get('infobox'))}: {txt}"[:600])
        return SearchResult(hits, [a for a in answers if a][:3], self.name, (time.perf_counter() - t0) * 1000)


class ChainProvider:
    """First provider that returns hits wins; errors fall through to the next one."""
    name = "chain"

    def __init__(self, providers: list[SearchProvider]):
        self.providers = providers

    async def search(self, q: str, *, time_range: str | None = None, news: bool = False) -> SearchResult:
        last = SearchResult([], error="no search provider configured")
        for p in self.providers:
            res = await p.search(q, time_range=time_range, news=news)
            if res.hits or res.answers:
                return res
            last = res
        return last


# ── ranking ────────────────────────────────────────────────────────────────────────────────────

_STOP = set("""a an and are as at be by did do does for from had has have how i in is it its of on or that the
their them they this to was were what when where which who whom why will with won't you your about after
before than then there these those can could would should tell me know please latest current today""".split())
# Sources whose facts are usually primary or well edited. A small boost, never a filter.
_TRUSTED = ("apnews.com", "reuters.com", "espn.com", "bbc.co.uk", "bbc.com", "npr.org", "weather.gov",
            "nytimes.com", "wsj.com", "bloomberg.com", "cnbc.com", "theguardian.com", "washingtonpost.com",
            "wikipedia.org", "sports-reference.com", "ncaa.com", "nfl.com", "nba.com", "mlb.com", "nhl.com",
            "cbssports.com", "foxsports.com", "si.com", "theathletic.com", "gov", "edu", "sec.gov", "nasa.gov",
            "who.int", "cdc.gov", "noaa.gov", "github.com", "python.org", "apple.com", "microsoft.com")
_LOW = ("pinterest.", "quora.com", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com")


def terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'.&-]*", text.lower()) if w not in _STOP and len(w) > 1]


def bm25(query_terms: list[str], doc: str, avg_len: float = 40.0, k1: float = 1.2, b: float = 0.75) -> float:
    words = terms(doc)
    if not words or not query_terms:
        return 0.0
    tf: dict[str, int] = {}
    for w in words:
        tf[w] = tf.get(w, 0) + 1
    score = 0.0
    for q in set(query_terms):
        f = tf.get(q, 0)
        if f:
            score += f * (k1 + 1) / (f + k1 * (1 - b + b * len(words) / avg_len))
    return score / len(set(query_terms))


def coverage(query_terms: list[str], doc: str) -> float:
    qs = set(query_terms)
    return len(qs & set(terms(doc))) / len(qs) if qs else 0.0


def _quality(domain: str) -> float:
    if any(domain == t or domain.endswith("." + t) for t in _TRUSTED):
        return 0.25
    if any(x in domain for x in _LOW):
        return -0.3
    return 0.0


def rank(q: str, hits: list[Hit], now: datetime | None = None, per_domain: int = 2, limit: int = 8) -> list[Hit]:
    now = now or datetime.now(timezone.utc)
    qt = terms(q)
    for h in hits:
        text = f"{h.title} {h.snippet}"
        s = bm25(qt, text) + 0.5 * coverage(qt, text) + _quality(h.domain) + 0.1 * min(len(h.engines), 3)
        if h.published:
            age_days = max(0.0, (now - h.published).total_seconds() / 86400)
            s += 0.3 * math.exp(-age_days / 7)
        h.score = round(s, 4)
    out, seen, per = [], set(), {}
    for h in sorted(hits, key=lambda h: h.score, reverse=True):
        key = h.url.split("#")[0].rstrip("/")
        if key in seen or per.get(h.domain, 0) >= per_domain:
            continue
        seen.add(key)
        per[h.domain] = per.get(h.domain, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _clean(s: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


def _date(v: object) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
