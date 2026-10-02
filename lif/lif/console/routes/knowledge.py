"""Knowledge area: home, search, object views and 'Save to Knowledge' over public, filtered knowledge (spec §32–§34).

Two sources, one shape:
- service  — LIF_KNOWLEDGE_URL is set: the knowledge service's read API (/v1/knowledge/*) via upstream.
- bundled  — otherwise: lif.knowledge in-process, read-only, over settings.knowledge_root() (the public repos
             copied into the image), with its index in a private temp dir. Never `Knowledge.open()` /
             `kn.refresh()`: both re-open the workspace from its root and would bring back the private repo.

Strict filtering (brief §9), applied to everything that leaves this module, including nested references:
- only repos whose manifest data class is PUBLIC or INTERNAL, and never `lif-operations`. In bundled mode
  other repos are not even compiled. In service mode the allowlist is the bundled public workspace, so an
  object from any other repo (such as the private operations repo) is dropped;
- no object whose own `data_class` field is CONFIDENTIAL or RESTRICTED. Bundled mode knows every object's class.
  Service mode: search hits carry no fields, so each hit's object is read and checked; home lists and link
  briefs are checked by repo allowlist and whatever fields the service returns (service mode isn't deployed);
- no absolute filesystem paths (fields and body text are rewritten repo-relative);
- a filtered object answers 404 exactly like a missing one, so its existence isn't revealed.

Knowledge 'decision' objects are architecture decision records; Decision Engineering runtime decisions are a
different thing and live under Agents.

Owner: AI.
"""
from __future__ import annotations

import asyncio
import atexit
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends

from lif.common import log
from lif.console import auth, settings, upstream
from lif.console.contracts import (AssumptionRef, DecisionRecord, EvidenceRef, Fact, HistoryEntry, KnowledgeHit,
                                   KnowledgeHome, KnowledgeLink, KnowledgeObjectResponse, KnowledgeObjectView,
                                   NoteRequest, OkResponse, User)
from lif.console.errors import human
from lif.console.upstream import UpstreamError

LOG = log.get("lif.console.knowledge")

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

OPEN_CLASSES = {"PUBLIC", "INTERNAL"}
EXCLUDED_REPOS = {"lif-operations"}
REFRESH_SEC = 30.0
RECENT_TYPES = "decision,assumption,lesson,incident,task,question"
API = "/v1/knowledge"

STATUS_LABEL: dict[str, dict[str, str]] = {
    "decision": {"proposed": "Proposed", "accepted": "In effect", "rejected": "Rejected",
                 "superseded": "Replaced by a newer decision", "deprecated": "No longer recommended"},
    "assumption": {"active": "Holding", "challenged": "Being questioned", "invalidated": "No longer true",
                   "retired": "Retired"},
    "task": {"open": "To do", "in-progress": "In progress", "blocked": "Blocked", "completed": "Done",
             "cancelled": "Cancelled"},
    "question": {"open": "Open question", "answered": "Answered", "dropped": "Dropped"},
    "incident": {"open": "Ongoing", "mitigated": "Contained", "resolved": "Resolved", "closed": "Closed"},
}
PRIORITY_LABEL = {"critical": "Urgent", "high": "High", "normal": "Normal", "low": "Low"}
OUTCOME_LABEL = {"reaffirmed": "Reviewed: still valid", "revised": "Reviewed: updated",
                 "superseded": "Reviewed: replaced", "needs-work": "Reviewed: needs work"}


def status_label(type_: str, status: str | None) -> str | None:
    if not status:
        return None
    return STATUS_LABEL.get(type_, {}).get(status) or status.replace("-", " ").capitalize()


# ── text hygiene ──────────────────────────────────────────────────────────────────────────────

_ABS = re.compile(r"(?<![\w.:/~])/(?:home|Users|root|app|data|knowledge|var|tmp|opt|srv|mnt|workspace)/"
                  r"[^\s`'\"<>)\]]*")
_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def strip_paths(text: str) -> str:
    """Absolute host paths → repo-relative ('/home/x/labzilla/lif/docs/A.md' → 'lif/docs/A.md')."""
    def rel(m: re.Match[str]) -> str:
        p = m.group(0)
        for marker, keep in (("/labzilla/", ""), ("/knowledge/", "knowledge/")):
            if marker in p:
                return keep + p.split(marker, 1)[1]
        return p.rstrip("/").rsplit("/", 1)[-1] or "…"
    return _ABS.sub(rel, text)


def plain(text: str, limit: int = 240) -> str:
    """Markdown-ish body → one readable line for a summary."""
    t = _WIKI.sub(lambda m: m.group(2) or m.group(1).split("::")[-1], text)
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"^\s{0,3}(#+|[-*>]|\d+\.)\s*", "", t, flags=re.M)
    t = re.sub(r"[*_`]|\[(.*?)\]\(.*?\)", lambda m: m.group(1) or "", t)
    t = " ".join(strip_paths(t).split())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def _summary(o: dict[str, Any]) -> str:
    f = o.get("fields") or {}
    for k in ("summary", "statement", "selected", "lesson", "question", "description"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return plain(v)
    return plain(str(o.get("body") or ""))


# ── sources ───────────────────────────────────────────────────────────────────────────────────

class Unavailable(Exception):
    """Knowledge can't be read (nothing bundled, or the service is down). str() is a human reason."""


def _repo_ok(repo: Any) -> bool:
    cls = str((getattr(repo, "manifest", None) or {}).get("data_class") or "CONFIDENTIAL").upper()
    return repo.name not in EXCLUDED_REPOS and cls in OPEN_CLASSES


def public_workspace(root: Path) -> Any:
    """The workspace at `root` with every non-public repo removed before anything is read."""
    from lif.knowledge.repo import Workspace
    if not (root / "workspace.yaml").exists():
        raise Unavailable("No knowledge is bundled with this console.")
    ws = Workspace.open(root)
    ws.repos = [r for r in ws.repos if _repo_ok(r)]
    ws.resolve()
    ws.packages = {n: p for n, p in ws.packages.items() if _repo_ok(p)}
    return ws


class Bundled:
    """Read-only lif.knowledge over the public repos, compiled into a private temp index."""
    mode = "bundled"

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.kn: Any = None
        self.tmp: Path | None = None
        self.checked = 0.0
        self.allowed: set[str] = set()
        self.blocked: set[str] = set()

    def close(self) -> None:
        """Drop the private index (source replaced, or process exit)."""
        with self.lock:
            self.kn = None
            if self.tmp is not None:
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.tmp = None

    def _ensure(self) -> Any:
        from lif.knowledge.compiler import Compiler
        from lif.knowledge.ops import Knowledge
        from lif.knowledge.search import Embedder
        from lif.knowledge.store import Store

        if self.kn is not None and time.monotonic() - self.checked < REFRESH_SEC:
            return self.kn
        ws = public_workspace(self.root)
        if self.kn is None:
            if self.tmp is None:
                self.tmp = Path(tempfile.mkdtemp(prefix="lz-knowledge-"))
            index = self.tmp / "index.db"
            emb = Embedder()
            emb.url = ""           # no embeddings: search must not call the gateway from a read
            self.kn = Knowledge(ws, Store(index), emb)
        else:
            self.kn.ws = ws
        Compiler(ws, self.kn.store).compile()          # incremental; repos removed above are dropped from the index
        self.allowed = {r.name for r in ws.all_repos()}
        self.blocked = {o["key"] for o in self.kn.store.objects("1=1", (), limit=10 ** 6)
                        if str((o.get("fields") or {}).get("data_class") or "PUBLIC").upper() not in OPEN_CLASSES}
        self.checked = time.monotonic()
        return self.kn

    async def _run(self, fn: Any) -> Any:
        def call() -> Any:
            with self.lock:
                try:
                    return fn(self._ensure())
                except (KeyError, Unavailable):
                    raise
                except Exception as e:      # a broken repo file must not become a 500 on every page
                    log.event(LOG, "knowledge_read_failed", error=type(e).__name__)
                    raise Unavailable("The bundled project knowledge couldn't be read.") from e
        return await asyncio.to_thread(call)

    async def find(self, type_: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self._run(lambda kn: kn.graph.find(type=type_, limit=limit))

    async def search(self, q: str, limit: int) -> list[dict[str, Any]]:
        return await self._run(lambda kn: kn.search.search(q, limit=limit, log=False)["results"])

    async def get(self, key: str) -> dict[str, Any]:
        return await self._run(lambda kn: kn.graph.get(key))

    async def why(self, key: str) -> dict[str, Any]:
        return await self._run(lambda kn: kn.graph.why(key))

    async def history(self, key: str) -> dict[str, Any]:
        return await self._run(lambda kn: kn.graph.decision_history(key))

    async def reconsiderations(self) -> list[dict[str, Any]]:
        return await self._run(lambda kn: kn.store.reconsiderations(True))

    async def scope(self) -> tuple[set[str], set[str]]:
        await self._run(lambda kn: None)
        return self.allowed, self.blocked


class Service:
    """The knowledge service over HTTP (admin key, server-side). Read endpoints only."""
    mode = "service"

    def __init__(self, root: Path):
        self.root = root
        self._allowed: set[str] | None = None

    def close(self) -> None:
        pass

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            return await upstream.get("knowledge", API + path, params=params or None)
        except UpstreamError as e:
            if e.status == 404:
                raise KeyError(path) from e
            raise Unavailable(upstream.reason(e)) from e

    async def find(self, type_: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self._get("/objects", type=type_, limit=limit) or []

    async def search(self, q: str, limit: int) -> list[dict[str, Any]]:
        return (await self._get("/search", q=q, limit=limit) or {}).get("results") or []

    async def get(self, key: str) -> dict[str, Any]:
        return await self._get(f"/objects/{quote(key, safe=':')}")

    async def why(self, key: str) -> dict[str, Any]:
        return await self._get(f"/why/{quote(key, safe=':')}")

    async def history(self, key: str) -> dict[str, Any]:
        return await self._get(f"/history/{quote(key, safe=':')}")

    async def reconsiderations(self) -> list[dict[str, Any]]:
        return await self._get("/reconsiderations") or []

    async def scope(self) -> tuple[set[str], set[str]]:
        # Allowlist = the public repos this console bundles; the service may also serve private ones.
        if self._allowed is None:
            try:
                ws = await asyncio.to_thread(public_workspace, self.root)
                self._allowed = {r.name for r in ws.all_repos()}
            except Exception as e:          # nothing bundled: show nothing rather than everything
                log.event(LOG, "knowledge_allowlist_empty", error=type(e).__name__)
                self._allowed = set()
        return self._allowed, set()


_source: Bundled | Service | None = None
_source_key: tuple[str, str] | None = None


def source() -> Bundled | Service:
    """The configured source (re-created when the env changes, e.g. between tests)."""
    global _source, _source_key
    url, root = settings.knowledge_url(), settings.knowledge_root()
    key = (url or "", str(root))
    if _source is None or _source_key != key:
        if _source is not None:
            _source.close()
        _source, _source_key = (Service(root) if url else Bundled(root)), key
    return _source


@atexit.register
def _close_source() -> None:
    if _source is not None:
        _source.close()


# ── filtering + mapping ───────────────────────────────────────────────────────────────────────

class Scope:
    def __init__(self, allowed: set[str], blocked: set[str]):
        self.allowed, self.blocked = allowed, blocked

    def key_ok(self, key: str | None) -> bool:
        if not key or "::" not in key:
            return False
        top = key.split("^", 1)[0]
        return top.split("::", 1)[0] in self.allowed and top not in self.blocked

    def obj_ok(self, o: dict[str, Any] | None) -> bool:
        if not isinstance(o, dict) or not self.key_ok(o.get("key")):
            return False
        if o.get("repo") and o["repo"] not in self.allowed:
            return False
        cls = (o.get("fields") or {}).get("data_class")
        return cls is None or str(cls).upper() in OPEN_CLASSES


async def _scope() -> Scope:
    return Scope(*await source().scope())


def hit(o: dict[str, Any], links: list[KnowledgeLink] | None = None) -> KnowledgeHit:
    t = str(o.get("type") or o.get("type_name") or "")
    summary = plain(re.sub(r"\[([^\]]*)\]", r"\1", str(o["snippet"]))) if o.get("snippet") else _summary(o)
    return KnowledgeHit(key=o["key"], type=t, title=strip_paths(str(o.get("title") or o["key"])), summary=summary,
                        status=status_label(t, o.get("status")), updated=o.get("updated") or None, links=links or [])


def _brief_link(rel: str, b: dict[str, Any] | None, sc: Scope) -> KnowledgeLink | None:
    if not b or not sc.obj_ok(b):
        return None
    return KnowledgeLink(rel=rel.replace("_", " "), key=b["key"], title=strip_paths(str(b.get("title") or b["key"])))


def _not_found() -> Exception:
    return human(404, "Not found in project knowledge", "It may have been renamed or it isn't part of the public "
                 "knowledge this console can show.", "Search for it instead.", [("Open Knowledge", "/knowledge")])


def _unavailable(reason: str) -> Exception:
    return human(503, "Knowledge isn't available", reason, "Retry in a moment.", [("Retry", "retry")])


async def _confirmed(src: Bundled | Service, rows: list[dict[str, Any]], sc: Scope, limit: int) -> list[dict[str, Any]]:
    """Rows that pass the filter, at most `limit`. The service's search results carry no `fields`, so an
    object-level data_class can't be seen on the hit itself: in service mode each candidate's object is
    read and filtered too (a few in-cluster reads per search, bounded by `limit`)."""
    rows = [r for r in rows if sc.obj_ok(r)]
    if src.mode != "service":
        return rows[:limit]
    sem = asyncio.Semaphore(6)

    async def full(r: dict[str, Any]) -> bool:
        if isinstance(r.get("fields"), dict):
            return True
        async with sem:
            try:
                return sc.obj_ok(await src.get(str(r["key"])))
            except (KeyError, Unavailable):
                return False            # can't confirm → don't show
    out: list[dict[str, Any]] = []
    for i in range(0, len(rows), limit):
        batch = rows[i:i + limit]
        oks = await asyncio.gather(*(full(r) for r in batch))
        out += [r for r, ok in zip(batch, oks, strict=True) if ok]
        if len(out) >= limit:
            break
    return out[:limit]


async def search_hits(q: str, limit: int = 20) -> list[KnowledgeHit]:
    """Filtered search (also used by the command bar for knowledge questions)."""
    src, sc = source(), await _scope()
    rows = await src.search(q, limit=limit * 3)
    return [hit(r) for r in await _confirmed(src, rows, sc, limit)]


@router.get("", response_model=KnowledgeHome)
async def home(user: User = Depends(auth.require("read"))) -> KnowledgeHome:
    src = source()
    try:
        sc = await _scope()
        projects, recent, decisions, assumptions, recon = await asyncio.gather(
            src.find("project", 20), src.find(RECENT_TYPES, 60), src.find("decision", 60), src.find("assumption", 60),
            src.reconsiderations())
    except Unavailable as e:
        return KnowledgeHome(connected=False, mode="unavailable" if src.mode == "bundled" else "service", note=str(e))
    keep = lambda rows, n: [hit(r) for r in rows if sc.obj_ok(r)][:n]   # noqa: E731
    titles = {r["key"]: r for r in [*recent, *decisions, *assumptions] if sc.obj_ok(r)}
    review: list[KnowledgeHit] = []
    for r in recon:
        k = str(r.get("key") or "")
        if not sc.key_ok(k) or any(h.key == k for h in review):
            continue
        o = titles.get(k) or {}
        review.append(KnowledgeHit(key=k, type=str(o.get("type") or ""), title=strip_paths(str(o.get("title") or
                                   k.split("::")[-1].replace("-", " ").capitalize())),
                                   summary=plain(str(r.get("reason") or "Something it relies on changed.")),
                                   status=PRIORITY_LABEL.get(str(r.get("priority")), "Normal"), updated=o.get("updated")))
    note = ("Read-only: the public project knowledge bundled with Labzilla." if src.mode == "bundled" else
            "Read-only view of the knowledge service (public repositories only).")
    return KnowledgeHome(projects=keep(projects, 10), recent_changes=keep(recent, 12), decisions=keep(decisions, 20),
                         assumptions=keep(assumptions, 20), needs_review=review[:10], connected=True, mode=src.mode,
                         note=note)


@router.get("/search", response_model=list[KnowledgeHit])
async def search(q: str = "", user: User = Depends(auth.require("read"))) -> list[KnowledgeHit]:
    q = q.strip()
    if len(q) < 2:
        return []
    if len(q) > 300:
        raise human(422, "That search is too long", "Nothing was searched.", "Use a few key words.")
    try:
        return await search_hits(q)
    except Unavailable as e:
        raise _unavailable(str(e)) from e


SAFE_KEY = re.compile(r"^[\w.\-]+(?:::[\w.\-^]+)?$")
SKIP_FIELDS = {"title", "status", "data_class", "path", "pinned", "summary", "statement"}


def _fields(f: dict[str, Any]) -> list[Fact]:
    out = []
    for k, v in f.items():
        if k in SKIP_FIELDS or v in (None, "", [], {}):
            continue
        if isinstance(v, list):
            vals = [x for x in v if isinstance(x, (str, int, float))]
            if not vals or all(isinstance(x, str) and x.startswith("[[") for x in vals):
                continue        # references are shown as links
            val = ", ".join(_WIKI.sub(lambda m: m.group(2) or m.group(1).split("::")[-1], str(x)) for x in vals)
        elif isinstance(v, (str, int, float, bool)):
            if isinstance(v, str) and v.startswith("[["):
                continue
            val = str(v)
        else:
            continue
        out.append(Fact(label=k.replace("_", " ").capitalize(), value=plain(val, 300)))
    return out[:20]


REL_OUT = {"assumptions": "relies on", "evidence": "evidence", "supported_by": "supported by", "affects": "affects",
           "supersedes": "replaces", "implements": "implements", "depends_on": "depends on"}
REL_IN = {"assumptions": "relied on by", "evidence": "cited by", "supported_by": "cited by", "affects": "affected by",
          "supersedes": "replaced by", "implements": "implemented by", "depends_on": "needed by"}


def object_view(o: dict[str, Any], sc: Scope) -> KnowledgeObjectView:
    t = str(o.get("type_name") or o.get("type") or "").split("::")[-1]
    rel = lambda e, table: table.get(str(e.get("field") or e.get("rel")), str(e.get("rel") or "related"))  # noqa: E731
    links = [lk for lk in (_brief_link(rel(e, REL_OUT), e.get("brief"), sc)
                           for e in o.get("links_out") or [] if e.get("resolved")) if lk]
    links += [lk for lk in (_brief_link(rel(e, REL_IN), e.get("brief"), sc) for e in o.get("links_in") or []) if lk]
    body = strip_paths(_WIKI.sub(lambda m: m.group(2) or m.group(1).split("::")[-1], str(o.get("body") or "")))
    return KnowledgeObjectView(key=o["key"], type=t, title=strip_paths(str(o.get("title") or o["key"])),
                               status=status_label(t, o.get("status")), summary=_summary(o), body=body[:20000],
                               fields=_fields(o.get("fields") or {}), links=links[:40], updated=o.get("updated") or None)


def decision_record(key: str, w: dict[str, Any], h: dict[str, Any], sc: Scope) -> DecisionRecord:
    """graph.why + graph.decision_history → a readable decision record (§34)."""
    obj = w.get("object") or {}
    ok = lambda b: isinstance(b, dict) and not b.get("missing") and sc.obj_ok(b)   # noqa: E731
    why = [plain(p, 1200) for p in re.split(r"\n\s*\n", str(w.get("rationale") or "")) if p.strip()]
    evidence = [EvidenceRef(key=b["key"], title=strip_paths(str(b.get("title") or b["key"])),
                            summary=plain(b["text"]) if b.get("text") else None)
                for b in w.get("evidence") or [] if ok(b)]
    assumptions = [AssumptionRef(key=b["key"], title=strip_paths(str(b.get("title") or b["key"])),
                                 status=status_label("assumption", b.get("status")) or "")
                   for b in w.get("assumptions") or [] if ok(b)]
    affected = [strip_paths(str(b.get("title"))) for b in w.get("affects") or [] if ok(b)]
    for t in ("service", "host", "deployment", "model"):
        affected += [strip_paths(str(b.get("title"))) for b in (w.get("dependents") or {}).get(t) or [] if ok(b)]
    history: list[HistoryEntry] = []
    if w.get("date"):
        history.append(HistoryEntry(ts=str(w["date"]), change="Decided"))
    for b in w.get("supersedes") or []:
        if ok(b):
            history.append(HistoryEntry(ts=str(b.get("updated") or ""), change=f"Replaced “{b.get('title')}”"))
    for b in w.get("superseded_by") or []:
        if ok(b):
            history.append(HistoryEntry(ts=str(b.get("updated") or ""), change=f"Replaced by “{b.get('title')}”"))
    for b in h.get("reviews") or []:
        if ok(b):
            history.append(HistoryEntry(ts=str(b.get("updated") or ""),
                                        change=OUTCOME_LABEL.get(str(b.get("outcome")), "Reviewed")))
    for b in h.get("changes") or []:
        if ok(b):
            history.append(HistoryEntry(ts=str(b.get("updated") or ""), change=strip_paths(str(b.get("title")))))
    if obj.get("updated") and not any(e.ts == obj["updated"] for e in history):
        history.append(HistoryEntry(ts=str(obj["updated"]), change="Last updated"))
    history.sort(key=lambda e: e.ts)
    recon = [r for r in w.get("reconsiderations") or [] if not r.get("resolved_by")]
    return DecisionRecord(
        key=key, title=strip_paths(str(obj.get("title") or key)), status=status_label("decision", obj.get("status")) or "",
        question=plain(str(w["question"]), 400) if w.get("question") else None,
        decision=decision_sentence(strip_paths(str(obj.get("title") or key)), w.get("selected")), why=why, evidence=evidence, assumptions=assumptions,
        affected=list(dict.fromkeys(a for a in affected if a and a != "None")),
        alternatives=[plain(str(a), 300) for a in w.get("alternatives") or [] if a], history=history,
        reconsideration=" ".join(plain(str(r.get("reason") or ""), 300) for r in recon) or None)


def decision_sentence(title: str, selected: Any) -> str:
    """The decision as a sentence (§34). Some records store only the chosen option's token
    (`selected: k3s`); then the title is the sentence and the token is kept in brackets."""
    if isinstance(selected, (list, tuple)):
        sel = ", ".join(str(x) for x in selected if x not in (None, ""))
    else:
        sel = str(selected or "")
    sel = plain(sel, 600)
    if not sel:
        return title
    if len(sel.split()) < 4:
        return f"{title} (selected: {sel})"
    return sel


@router.get("/objects/{key:path}", response_model=KnowledgeObjectResponse)
async def get_object(key: str, user: User = Depends(auth.require("read"))) -> KnowledgeObjectResponse:
    if not SAFE_KEY.match(key) or ".." in key:      # the key becomes an upstream path in service mode
        raise _not_found()
    src = source()
    try:
        sc = await _scope()
        o = await src.get(key)
    except KeyError as e:
        raise _not_found() from e
    except Unavailable as e:
        raise _unavailable(str(e)) from e
    if not sc.obj_ok(o):
        raise _not_found()
    if str(o.get("type_name") or o.get("type") or "").split("::")[-1] == "decision":
        try:
            w, h = await asyncio.gather(src.why(o["key"]), src.history(o["key"]))
        except KeyError as e:               # the service mode's 404 for a missing why/history record
            raise _not_found() from e
        except Unavailable as e:
            raise _unavailable(str(e)) from e
        return KnowledgeObjectResponse(kind="decision", decision=decision_record(o["key"], w, h, sc))
    return KnowledgeObjectResponse(kind="object", object=object_view(o, sc))


@router.post("/notes", response_model=OkResponse)
async def save_note(req: NoteRequest, user: User = Depends(auth.require("read"))) -> OkResponse:
    """'Save to Knowledge' (§21). Not available: bundled knowledge is a read-only copy, and the console's
    key for the knowledge service is read-only by design (the knowledge repos are public)."""
    if user.role != "admin":
        raise human(403, "Not allowed from this session", "Only an admin can add to project knowledge. Nothing "
                    "was saved.", "Use an admin session on a desktop.")
    if source().mode == "bundled":
        raise human(409, "Knowledge is read-only until the knowledge service is deployed",
                    "This console shows a read-only copy of the public project knowledge, so nothing was saved.",
                    "Copy the answer and add it to the knowledge repository instead.")
    raise human(409, "Knowledge is read-only from the console",
                "The console's access to the knowledge service is read-only, so nothing was saved.",
                "Copy the answer and add it to the knowledge repository instead.")
