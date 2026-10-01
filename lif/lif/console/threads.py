"""Ask threads and messages in the console database (spec §67–§70).

Conversations live server-side so a prompt sent from a phone can be opened on the desktop (and the
answer keeps arriving there even if the phone locks mid-stream). Tables `threads` / `messages` are
created by db.py's migrations (CORE); this module owns the queries.

Ownership: one household owner. A paired device signs in as its admin's user id (db.py), so filtering
by user id shows the same history on every device of that owner.

Attachment text is stored inside `attachments_json` next to each Attachment's metadata (key `text`);
contracts.Attachment ignores unknown keys, so the text never reaches the browser, but it is still
there to rebuild the conversation context for the next turn. Images are never stored: a message keeps
an image's name, size and dimensions only, and a replayed turn says an image was there ("not kept").

Owner: AI.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any

from lif.console import db
from lif.console.contracts import (AskMode, Attachment, HumanError, Message, PrivacyChoice, Receipt, Thread,
                                   ThreadSummary)

TITLE_MAX = 60
PREVIEW_MAX = 120


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def title_from(text: str) -> str:
    """First non-empty line, trimmed to TITLE_MAX characters on a word boundary."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "").lstrip("#>-* ").strip()
    if len(line) <= TITLE_MAX:
        return line or "New conversation"
    cut = line[:TITLE_MAX].rsplit(" ", 1)[0] or line[:TITLE_MAX]
    return cut.rstrip(" ,.;:") + "…"


def _group(ts: float, now: float) -> str:
    """Today / Yesterday / Earlier by the server's calendar day (§67). The server runs in UTC, so History
    regroups by the browser's calendar day from updated_at; this is the fallback."""
    day = time.localtime(now)
    start_today = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    return "today" if ts >= start_today else "yesterday" if ts >= start_today - 86400 else "earlier"


def _message(r: Any) -> Message:
    atts = [Attachment.model_validate(a) for a in json.loads(r["attachments_json"] or "[]")]
    return Message(id=r["id"], role=r["role"], content=r["content"], created_at=r["created_at"], status=r["status"],
                   error=HumanError.model_validate_json(r["error_json"]) if r["error_json"] else None,
                   receipt=Receipt.model_validate_json(r["receipt_json"]) if r["receipt_json"] else None,
                   attachments=atts)


# ── threads ───────────────────────────────────────────────────────────────────────────────────

def create_thread(user_id: str, mode: AskMode, privacy: PrivacyChoice, title: str | None = None,
                  origin_device: str | None = None) -> Thread:
    now = time.time()
    t = Thread(id=new_id("th"), title=title_from(title or ""), mode=mode, privacy=privacy, created_at=now,
               updated_at=now, origin_device=origin_device)
    with db.tx() as c:
        c.execute("INSERT INTO threads(id, user_id, title, mode, privacy, created_at, updated_at, origin_device) "
                  "VALUES(?,?,?,?,?,?,?,?)", (t.id, user_id, t.title, mode, privacy, now, now, origin_device))
    return t


def _thread_row(user_id: str, thread_id: str) -> Any:
    return db.one("SELECT * FROM threads WHERE id=? AND user_id=?", (thread_id, user_id))


def get_thread(user_id: str, thread_id: str) -> Thread | None:
    r = _thread_row(user_id, thread_id)
    if r is None:
        return None
    msgs = [_message(m) for m in db.q("SELECT * FROM messages WHERE thread_id=? ORDER BY created_at, rowid",
                                      (thread_id,))]
    return Thread(id=r["id"], title=r["title"], mode=r["mode"], privacy=r["privacy"], created_at=r["created_at"],
                  updated_at=r["updated_at"], origin_device=r["origin_device"], messages=msgs)


_SUMMARY_SQL = ("SELECT t.*, (SELECT content FROM messages m WHERE m.thread_id=t.id AND m.role='user' "
                "ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_prompt, "
                "(SELECT attachments_json FROM messages m WHERE m.thread_id=t.id AND m.role='user' "
                "ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_files FROM threads t ")


def _summary(r: Any, now: float, active: set[str]) -> ThreadSummary:
    preview = " ".join((r["last_prompt"] or "").split())[:PREVIEW_MAX]
    if not preview and r["last_files"]:        # an image-only prompt: name what was sent
        names = [str(a.get("name") or "") for a in json.loads(r["last_files"] or "[]") if isinstance(a, dict)]
        preview = ", ".join(n for n in names if n)[:PREVIEW_MAX]
    return ThreadSummary(id=r["id"], title=r["title"], updated_at=r["updated_at"], mode=r["mode"], preview=preview,
                         group=_group(r["updated_at"], now), origin_device=r["origin_device"],  # type: ignore[arg-type]
                         active=r["id"] in active)


def list_threads(user_id: str, limit: int = 100, active: set[str] | None = None) -> list[ThreadSummary]:
    """Newest first. `active` = thread ids with an answer being produced right now (routes/ai.py _RUNS):
    the database alone can't tell, a row left 'streaming' by a restart is not live."""
    now = time.time()
    rows = db.q(_SUMMARY_SQL + "WHERE t.user_id=? ORDER BY t.updated_at DESC LIMIT ?", (user_id, limit))
    return [_summary(r, now, active or set()) for r in rows]


def thread_summary(user_id: str, thread_id: str, active: set[str] | None = None) -> ThreadSummary | None:
    r = db.one(_SUMMARY_SQL + "WHERE t.id=? AND t.user_id=?", (thread_id, user_id))
    return _summary(r, time.time(), active or set()) if r else None


RENAME_MAX = 120


def rename_thread(user_id: str, thread_id: str, title: str) -> bool:
    """A title the user chose. updated_at stays: renaming must not move the conversation in the list."""
    with db.tx() as c:
        return c.execute("UPDATE threads SET title=? WHERE id=? AND user_id=?",
                         (title, thread_id, user_id)).rowcount > 0


def delete_thread(user_id: str, thread_id: str) -> bool:
    with db.tx() as c:
        c.execute("DELETE FROM messages WHERE thread_id IN (SELECT id FROM threads WHERE id=? AND user_id=?)",
                  (thread_id, user_id))
        return c.execute("DELETE FROM threads WHERE id=? AND user_id=?", (thread_id, user_id)).rowcount > 0


def touch_thread(thread_id: str, *, mode: AskMode | None = None, privacy: PrivacyChoice | None = None,
                 title: str | None = None) -> None:
    sets, args = ["updated_at=?"], [time.time()]
    for col, val in (("mode", mode), ("privacy", privacy), ("title", title)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    with db.tx() as c:
        c.execute(f"UPDATE threads SET {', '.join(sets)} WHERE id=?", (*args, thread_id))


# ── messages ──────────────────────────────────────────────────────────────────────────────────

def add_message(thread_id: str, role: str, content: str, *, status: str = "done",
                attachments: list[dict[str, Any]] | None = None, created_at: float | None = None) -> Message:
    """`attachments` are Attachment dicts, optionally carrying the inlined `text` (kept server-side)."""
    mid, now = new_id("msg"), created_at or time.time()
    with db.tx() as c:
        c.execute("INSERT INTO messages(id, thread_id, role, content, created_at, status, attachments_json) "
                  "VALUES(?,?,?,?,?,?,?)", (mid, thread_id, role, content, now, status,
                                            json.dumps(attachments or [])))
        c.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
    return Message(id=mid, role=role, content=content, created_at=now, status=status,  # type: ignore[arg-type]
                   attachments=[Attachment.model_validate(a) for a in attachments or []])


def start_turn(thread_id: str, content: str, attachments: list[dict[str, Any]], *, mode: AskMode,
               privacy: PrivacyChoice, title: str | None) -> tuple[Message, Message]:
    """The user's turn and the empty streaming answer, in one transaction: a storage failure leaves
    neither behind (never a question with no answer). Attachment text is kept only while the turn
    could still be replayed as context; a bigger turn keeps its metadata (and the model never saw
    more of it in later turns anyway, see context())."""
    now = time.time()
    if len(compose(content, attachments)) > CONTEXT_CHARS:
        attachments = [{k: v for k, v in a.items() if k != "text"} | ({"text_omitted": True} if a.get("text") else {})
                       for a in attachments]
    uid, aid = new_id("msg"), new_id("msg")
    sets, args = ["updated_at=?", "mode=?", "privacy=?"], [now, mode, privacy]
    if title is not None:
        sets.append("title=?")
        args.append(title)
    with db.tx() as c:
        c.execute(f"UPDATE threads SET {', '.join(sets)} WHERE id=?", (*args, thread_id))
        c.execute("INSERT INTO messages(id, thread_id, role, content, created_at, status, attachments_json) "
                  "VALUES(?,?,?,?,?,?,?)", (uid, thread_id, "user", content, now, "done", json.dumps(attachments)))
        c.execute("INSERT INTO messages(id, thread_id, role, content, created_at, status, attachments_json) "
                  "VALUES(?,?,?,?,?,?,?)", (aid, thread_id, "assistant", "", now, "streaming", "[]"))
    return (Message(id=uid, role="user", content=content, created_at=now, status="done",
                    attachments=[Attachment.model_validate(a) for a in attachments]),
            Message(id=aid, role="assistant", content="", created_at=now, status="streaming"))


# Per-user storage ceiling for conversations (the console's volume is 1 GiB and also holds sign-in
# records): past it, the oldest conversations are removed, never the one being written to. That one
# is bounded by THREAD_QUOTA_BYTES instead: past it, the conversation takes no new messages (routes/ai.py).
# The thread cap must stay well under the user cap, or one long conversation would push every other
# one out at the next check.
USER_QUOTA_BYTES = 256 * 1024 * 1024
THREAD_QUOTA_BYTES = 64 * 1024 * 1024
QUOTA_CHECK_SEC = 30.0
_quota_checked: dict[str, float] = {}
# What a message costs on disk, for both caps.
_MESSAGE_BYTES = ("COALESCE(SUM(length(CAST(m.content AS BLOB)) + length(CAST(m.attachments_json AS BLOB))"
                  " + length(CAST(COALESCE(m.receipt_json, '') AS BLOB))), 0)")


def thread_bytes(thread_id: str) -> int:
    """Stored size of one conversation's messages (the same accounting as enforce_quota)."""
    row = db.one(f"SELECT {_MESSAGE_BYTES} AS bytes FROM messages m WHERE m.thread_id=?", (thread_id,))
    return int(row["bytes"]) if row else 0


def enforce_quota(user_id: str, keep: str, *, now: float | None = None, force: bool = False) -> list[str]:
    """Delete this user's oldest threads beyond USER_QUOTA_BYTES (checked at most every QUOTA_CHECK_SEC).
    Returns the deleted thread ids."""
    now = time.time() if now is None else now
    if not force and now - _quota_checked.get(user_id, 0.0) < QUOTA_CHECK_SEC:
        return []
    _quota_checked[user_id] = now
    rows = db.q(f"SELECT t.id, {_MESSAGE_BYTES} AS bytes FROM threads t "
                "LEFT JOIN messages m ON m.thread_id = t.id WHERE t.user_id=? GROUP BY t.id "
                "ORDER BY t.updated_at DESC", (user_id,))
    total, gone = 0, []
    for r in rows:
        total += r["bytes"]
        if total > USER_QUOTA_BYTES and r["id"] != keep:
            gone.append(r["id"])
    if gone:
        with db.tx() as c:
            for tid in gone:
                c.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
                c.execute("DELETE FROM threads WHERE id=? AND user_id=?", (tid, user_id))
    return gone


def update_message(message_id: str, *, content: str | None = None, status: str | None = None,
                   error: HumanError | None = None, receipt: Receipt | None = None) -> None:
    sets, args = [], []
    for col, val in (("content", content), ("status", status),
                     ("error_json", error.model_dump_json() if error else None),
                     ("receipt_json", receipt.model_dump_json() if receipt else None)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if sets:
        with db.tx() as c:
            c.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", (*args, message_id))


def message_row(thread_id: str, message_id: str) -> Any:
    return db.one("SELECT * FROM messages WHERE id=? AND thread_id=?", (message_id, thread_id))


CONTEXT_CHARS = 16000


def context(thread_id: str, *, before: float, max_messages: int = 12, max_chars: int = CONTEXT_CHARS) -> list[dict[str, str]]:
    """Earlier turns for the next request: newest first until the budget is spent, returned oldest first.

    Only finished turns count (a cancelled or failed answer would teach the model the wrong thing).
    A user turn is rebuilt with its attachment text so "Continue" still sees the file.
    """
    rows = db.q("SELECT role, content, attachments_json FROM messages WHERE thread_id=? AND created_at<? "
                "AND status='done' AND role IN ('user','assistant') ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (thread_id, before, max_messages))
    out: list[dict[str, str]] = []
    used = 0
    for r in rows:
        atts = json.loads(r["attachments_json"] or "[]") if r["role"] == "user" else []
        text = compose(r["content"], atts) if r["role"] == "user" else r["content"]
        images = [str(a.get("name") or "image") for a in atts if a.get("kind") == "image" and a.get("included")]
        if images:          # images are never stored: the model only learns that one was there
            text = (text + "\n\n" if text else "") + f"[Earlier image, not kept: {', '.join(images)}]"
        if used + len(text) > max_chars or any(a.get("text_omitted") for a in atts):     # too big to replay
            break
        used += len(text)
        out.append({"role": r["role"], "content": text})
    return out[::-1]


def compose(content: str, attachments: list[dict[str, Any]]) -> str:
    """The prompt the model sees: the user's text, then each included file under its own header."""
    parts = [content.strip()]
    for a in attachments:
        if a.get("included") and a.get("text"):
            parts.append(f"--- File: {a.get('name') or 'attachment'} ({a.get('kind') or 'text'}) ---\n"
                         f"{a['text']}\n--- End of {a.get('name') or 'attachment'} ---")
    return "\n\n".join(p for p in parts if p)


def latest_receipt(user_id: str) -> tuple[str, str, Message] | None:
    """(thread id, thread title, assistant message) of this user's most recent answered request."""
    r = db.one("SELECT m.*, t.title AS thread_title FROM messages m JOIN threads t ON t.id=m.thread_id "
               "WHERE t.user_id=? AND m.role='assistant' AND m.receipt_json IS NOT NULL "
               "ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1", (user_id,))
    return (r["thread_id"], r["thread_title"], _message(r)) if r else None


def stale_streaming(thread_id: str, active: set[str]) -> list[str]:
    """Assistant messages still marked streaming that no producer is working on (console restarted)."""
    rows = db.q("SELECT id FROM messages WHERE thread_id=? AND status='streaming'", (thread_id,))
    return [r["id"] for r in rows if r["id"] not in active]
