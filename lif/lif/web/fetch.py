"""Read a public web page safely and pull out the passages that answer a query.

Safety (the gateway sits inside the cluster, next to services with no auth of their own):
  - http/https on ports 80/443 only, no credentials in the URL;
  - every address the host resolves to must be public: loopback, private, link-local, CGNAT, multicast,
    reserved and the cluster's pod/service ranges are refused, and redirects are re-checked hop by hop;
  - size, time and redirect caps; HTML and plain text only.
The extracted text is untrusted data: the grounding prompt says so, and it is never executed or followed.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from lif.web.search import bm25, terms

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "0.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "::/128", "64:ff9b::/96")]


class Blocked(Exception):
    pass


def _public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not any(ip in n for n in _BLOCKED_NETS)


async def check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise Blocked("only plain http(s) URLs")
    if u.port not in (None, 80, 443):
        raise Blocked("non-standard port")
    host = u.hostname
    if host.endswith((".local", ".lan", ".internal", ".svc", ".cluster.local", "localhost")):
        raise Blocked("internal host name")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise Blocked(f"dns: {e}") from e
    addrs = {i[4][0] for i in infos}
    if not addrs or not all(_public(a) for a in addrs):
        raise Blocked("resolves to a non-public address")


async def fetch_text(client: httpx.AsyncClient, url: str, timeout: float = 2.0) -> str:
    """The page's readable text, or "" on any failure (a read is an optional enrichment)."""
    try:
        return await asyncio.wait_for(_fetch(client, url), timeout)
    except (Blocked, httpx.HTTPError, asyncio.TimeoutError, UnicodeError, ValueError):
        return ""


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(url)
        async with client.stream("GET", url, follow_redirects=False,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; lif-web/1)",
                                          "Accept": "text/html,text/plain;q=0.9"}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                return ""
            ctype = r.headers.get("content-type", "")
            if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
                return ""
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    break
            text = buf.decode(r.encoding or "utf-8", errors="replace")
            return extract(text) if "html" in ctype else re.sub(r"\s+", " ", text)
    return ""


class _Text(HTMLParser):
    """Readable text: paragraphs, headings, list items and table cells, skipping chrome and scripts."""
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe", "button",
            "select", "template"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "td", "th", "tr", "div", "section", "article", "br", "dd", "dt",
             "blockquote", "figcaption", "caption", "time"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []
        self.cur: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.cur.append(data)

    def _flush(self):
        s = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        if s:
            self.parts.append(s)
        self.cur = []


def extract(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:            # malformed markup: keep what was parsed
        pass
    p._flush()
    # Drop boilerplate lines: very short fragments that aren't numbers (scores, prices, temperatures).
    keep = [s for s in p.parts if len(s) >= 40 or re.search(r"\d", s) and len(s) >= 8]
    return "\n".join(keep)


def passages(text: str, query: str, k: int = 2, words: int = 70) -> list[str]:
    """The k best ~`words`-word windows of the page for the query (BM25 over overlapping windows)."""
    toks = text.split()
    if not toks:
        return []
    qt = terms(query)
    step = max(20, words // 2)
    wins = [" ".join(toks[i:i + words]) for i in range(0, max(1, len(toks) - words // 2), step)]
    scored = sorted(((bm25(qt, w, avg_len=words), i, w) for i, w in enumerate(wins)), reverse=True)
    out, used = [], set()
    for s, i, w in scored:
        if s <= 0 or len(out) >= k:
            break
        if i - 1 in used or i + 1 in used:           # overlapping windows say the same thing
            continue
        used.add(i)
        out.append(w)
    return out
