"""Console state in SQLite (stdlib sqlite3, WAL) on the console's own volume.

Tables: users, sessions, devices, pairings, threads, messages, audit, notif_seen, kv — with
forward-only migrations keyed by `PRAGMA user_version`. Threads and messages live here (not in the
browser) so a conversation started on a phone opens on the desktop (spec §68–§70).

One process, one writer: every write goes through `tx()` on a single connection guarded by a lock
(route handlers run on the event loop *and* in the threadpool). Reads may use `q()`/`one()` (same
connection, same lock) or a private `connect()` connection — WAL lets readers run beside the writer.

The database opens lazily on first use, so a test or tool that never runs the app lifespan still
works, and it reopens when LIF_CONSOLE_DB changes (tests point each case at a fresh file). If the
volume isn't writable the console keeps serving: /readyz reports it and routes return a human 503.

Column conventions (shared with routes/ai.py): ids are TEXT, timestamps are epoch seconds (REAL),
`*_json` columns hold JSON text of the matching contract (Receipt, HumanError, list[Attachment]).
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lif.common import log
from lif.console import settings

LOG = log.get("lif.console.db")

# Forward-only. Never edit a released step; append a new one (user_version = index + 1).
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        role TEXT NOT NULL,                          -- admin | device (devices never sign in by passphrase)
        pass_hash TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE devices (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,   -- the admin who paired it
        name TEXT NOT NULL,
        user_agent TEXT NOT NULL DEFAULT '',
        paired_at REAL NOT NULL,
        last_seen REAL,
        revoked_at REAL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,                 -- sha256 of the cookie value; the token itself is never stored
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        device_id TEXT REFERENCES devices(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        user_agent TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX sessions_device ON sessions(device_id);
    CREATE INDEX sessions_user ON sessions(user_id);
    CREATE TABLE pairings (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        claim_hash TEXT UNIQUE,                      -- sha256 of the phone's lz_pair cookie
        created_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT NOT NULL,                        -- waiting | claimed | approved | rejected | expired
        code TEXT,                                   -- 6-digit verification code, set at claim
        device_name TEXT,
        user_agent TEXT NOT NULL DEFAULT '',
        claimed_at REAL,
        decided_by TEXT,
        decided_at REAL,
        device_id TEXT,
        delivered_at REAL                            -- when the phone received its device session
    );
    CREATE TABLE threads (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,                       -- owner; a paired device acts as its admin's user id
        title TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT 'auto',
        privacy TEXT NOT NULL DEFAULT 'local_only',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        origin_device TEXT
    );
    CREATE INDEX threads_user ON threads(user_id, updated_at);
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        role TEXT NOT NULL,                          -- user | assistant | system
        content TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'done',         -- streaming | done | error | cancelled
        error_json TEXT,
        receipt_json TEXT,
        attachments_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX messages_thread ON messages(thread_id, created_at);
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        user_id TEXT,
        user_name TEXT,
        role TEXT,
        device_name TEXT,
        ip TEXT,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        detail_json TEXT
    );
    CREATE INDEX audit_ts ON audit(ts);
    CREATE TABLE notif_seen (
        user_id TEXT NOT NULL,
        notif_id TEXT NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (user_id, notif_id)
    );
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_path: Path | None = None


def _open(path: Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    have = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, step in enumerate(MIGRATIONS[have:], start=have + 1):
        # executescript commits implicitly; one step = one script + its version bump.
        conn.executescript(f"BEGIN IMMEDIATE;\n{step}\nPRAGMA user_version={i};\nCOMMIT;")
        log.event(LOG, "migrated", version=i)


def init(path: Path | None = None) -> None:
    """Create or migrate the database at `path` (default settings.db_path()). Idempotent."""
    global _conn, _path
    target = Path(path) if path is not None else settings.db_path()
    with _lock:
        if _conn is not None and _path == target:
            return
        close()
        conn = _open(target)
        try:
            _migrate(conn)
        except BaseException:
            conn.close()
            raise
        _conn, _path = conn, target


def close() -> None:
    global _conn, _path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _path = None, None


def _shared() -> sqlite3.Connection:
    """The writer connection, (re)opened lazily so env changes between tests are honoured."""
    if _conn is None or _path != settings.db_path():
        try:
            init()
        except OSError as e:        # unwritable/missing volume: surface as a database error (→ human 503)
            raise sqlite3.OperationalError(f"cannot open console database: {type(e).__name__}") from e
    assert _conn is not None
    return _conn


def connect() -> sqlite3.Connection:
    """A private connection to the initialised database (row_factory = sqlite3.Row), for reads.

    The caller closes it. Writes belong in `tx()` so there is exactly one writer.
    """
    with _lock:
        _shared()
        assert _path is not None
        return _open(_path)


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """One write transaction: commit on success, roll back on error. Nested calls join the outer one."""
    with _lock:
        conn = _shared()
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def q(sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with _lock:
        return _shared().execute(sql, args).fetchall()


def one(sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with _lock:
        return _shared().execute(sql, args).fetchone()


def ok() -> bool:
    """Readiness: the database opens and answers."""
    try:
        return one("SELECT 1") is not None
    except Exception:       # unwritable volume, corrupt file …: report, never crash
        return False


def kv_get(key: str) -> str | None:
    row = one("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else None


def kv_set(key: str, value: str, now: float) -> None:
    with tx() as c:
        c.execute("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (key, value, now))
