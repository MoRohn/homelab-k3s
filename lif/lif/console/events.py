"""Server-sent events hub: the server polls upstreams once and fans changes out to every viewer (spec §61).

`hub.publish(type, data, audience)` is synchronous (it only enqueues) so the poller and routes can call
it without awaiting — including from sync route handlers in the threadpool: each subscriber remembers
its event loop and off-loop publishes hop over with call_soon_threadsafe. A frame is encoded once per
publish, not per viewer. Each viewer has a bounded queue; a slow viewer loses its *oldest* frames
(the next status frame supersedes them anyway) instead of growing memory.

GET /api/events streams `event: <EventType>` / `data: <json>` frames with `retry: 3000`, the latest
status snapshot first, and a heartbeat comment every 15 s. Every 15 s the session is re-checked (expiry),
and logout/revocation closes matching streams at once (`hub.disconnect`), so a revoked phone stops
receiving live state immediately. Payload shapes: contracts.EventPayloads.

audience: None = every signed-in viewer; "admin" = admin sessions only; "user:<id>" = one user's sessions
(a paired device acts as its admin's user, so "user:<id>" reaches their phone and desktop alike).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from prometheus_client import Gauge
from pydantic import BaseModel

from lif.common import log
from lif.console import auth
from lif.console.contracts import EventType, User
from lif.console.errors import human

LOG = log.get("lif.console.events")

router = APIRouter(prefix="/api", tags=["events"])     # paths include the area: /api/events

sse_clients = Gauge("lif_console_sse_clients", "Open /api/events streams")

QUEUE_MAX = 256
MAX_PER_SESSION = 8                 # tabs × reconnect overlap; beyond this something is looping
_CLOSE = object()                   # sentinel: end this stream now


def encode(type: str, data: BaseModel | dict[str, Any]) -> str:
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    return f"event: {type}\ndata: {json.dumps(payload, separators=(',', ':'), default=str)}\n\n"


@dataclass(eq=False)
class _Sub:
    user: User
    session: str | None             # token hash, for disconnect/re-check
    device_id: str | None
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[object] = field(default_factory=lambda: asyncio.Queue(QUEUE_MAX))
    dropped: int = 0

    def wants(self, audience: str | None) -> bool:
        if audience is None:
            return True
        if audience == "admin":
            return self.user.role == "admin"
        if audience.startswith("user:"):
            return self.user.id == audience[5:]
        return False

    def put(self, frame: object) -> None:
        """Runs on the subscriber's loop. Full queue → drop the oldest frame, keep the newest."""
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(frame)


class Hub:
    HEARTBEAT_SEC = 15.0

    def __init__(self) -> None:
        self._subs: set[_Sub] = set()
        self.last_status: str | None = None    # latest encoded status frame, for viewers joining late

    def __len__(self) -> int:
        return len(self._subs)

    @staticmethod
    def _deliver(sub: _Sub, frame: object) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is sub.loop:
            sub.put(frame)
        elif not sub.loop.is_closed():
            sub.loop.call_soon_threadsafe(sub.put, frame)

    def publish(self, type: EventType, data: BaseModel | dict[str, Any], audience: str | None = None) -> None:
        """Queue one event for every matching subscriber. Never blocks, never raises."""
        try:
            frame = encode(type, data)
            if type == "status" and audience is None:
                self.last_status = frame
            for sub in list(self._subs):
                if sub.wants(audience):
                    self._deliver(sub, frame)
        except Exception as e:      # a bad payload must not break the poller or a route
            log.event(LOG, "publish_failed", type=type, error=type_name(e))

    def disconnect(self, *, session: str | None = None, device_id: str | None = None,
                   user_id: str | None = None) -> int:
        """Close the streams of a session, a device or a user (logout, revocation). Returns how many."""
        match: Callable[[_Sub], bool] = lambda s: bool(          # noqa: E731
            (session and s.session == session) or (device_id and s.device_id == device_id)
            or (user_id and s.user.id == user_id))
        hits = [s for s in list(self._subs) if match(s)]
        for s in hits:
            self._deliver(s, _CLOSE)
        return len(hits)

    def close(self) -> None:
        """Shutdown: end every stream so uvicorn's graceful window isn't spent waiting on SSE."""
        for s in list(self._subs):
            self._deliver(s, _CLOSE)

    def check_capacity(self, session: str | None) -> None:
        """Called before the response starts (an error mid-stream couldn't carry a human body)."""
        if session and sum(1 for s in self._subs if s.session == session) >= MAX_PER_SESSION:
            raise human(429, "Too many open Labzilla windows", "Live updates are paused in this window.",
                        "Close a few Labzilla tabs, then reload.", [("Reload", "reload")])

    def _add(self, user: User, session: str | None, device_id: str | None) -> _Sub:
        sub = _Sub(user, session, device_id, asyncio.get_running_loop())
        self._subs.add(sub)
        sse_clients.set(len(self._subs))
        return sub

    def _remove(self, sub: _Sub) -> None:
        self._subs.discard(sub)
        sse_clients.set(len(self._subs))

    async def subscribe(self, user: User, *, session: str | None = None, device_id: str | None = None,
                        initial: str | None = None,
                        alive: Callable[[], bool] | None = None) -> AsyncIterator[str]:
        """Encoded SSE frames for this user until the client disconnects (or the session ends)."""
        sub = self._add(user, session, device_id)
        first = initial or self.last_status     # taken now: later statuses arrive through the queue
        loop = asyncio.get_running_loop()
        checked = loop.time()
        try:
            yield "retry: 3000\n\n"
            if first:
                yield first
            while True:
                # The session is re-checked on a clock, not only when the queue goes quiet: the poller
                # publishes status most cycles, so an expired session would otherwise stream forever.
                if alive is not None and loop.time() - checked >= self.HEARTBEAT_SEC:
                    checked = loop.time()
                    if not alive():
                        return
                try:
                    frame = await asyncio.wait_for(sub.queue.get(), self.HEARTBEAT_SEC)
                except TimeoutError:
                    yield ": hb\n\n"
                    continue
                if frame is _CLOSE:
                    return
                assert isinstance(frame, str)
                yield frame
        finally:
            self._remove(sub)


def type_name(e: BaseException) -> str:
    return type(e).__name__


hub = Hub()


def _initial_status() -> str | None:
    """The poller's latest status, if it has completed a cycle (SYS may not have started yet)."""
    try:
        from lif.console import poller
        snap = poller.snapshot()
        return encode("status", snap.status) if snap.updated_at else None
    except Exception:
        return None


@router.get("/events", include_in_schema=False)
async def events(request: Request, user: User = Depends(auth.current_user)) -> StreamingResponse:
    sess = auth.session_of(request)
    th = sess.token_hash if sess else None
    hub.check_capacity(th)
    stream = hub.subscribe(user, session=th, device_id=sess.device_id if sess else None,
                           initial=_initial_status(),
                           alive=(lambda: auth.session_alive(th)) if th else None)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
