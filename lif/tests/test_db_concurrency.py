"""Several processes writing one SQLite file through lif.common.db.DB lose nothing and never fail with
"database is locked". Evidence for the decision to keep SQLite (knowledge-index-sqlite): on the host, every
Claude Code session runs its own knowledge MCP server against the same index, so "one writer per file"
does not hold there; WAL + BEGIN IMMEDIATE + busy_timeout must serialise them instead."""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from lif.common.db import DB

SCHEMA = "CREATE TABLE IF NOT EXISTS t(writer INT, n INT); CREATE TABLE IF NOT EXISTS counter(v INT);"
WRITERS = 6
TX_PER_WRITER = 150


def _writer(path: str, wid: int) -> int:
    db = DB(path, SCHEMA)
    failures = 0
    for i in range(TX_PER_WRITER):
        try:
            with db.tx() as c:
                c.execute("INSERT INTO t(writer, n) VALUES(?, ?)", (wid, i))
                c.execute("UPDATE counter SET v = v + 1")
        except Exception:
            failures += 1
    return failures


def test_concurrent_writer_processes_are_serialised(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    db = DB(path, SCHEMA)
    with db.tx() as c:
        c.execute("INSERT INTO counter VALUES (0)")
    ctx = mp.get_context("spawn")
    with ctx.Pool(WRITERS) as pool:
        failures = pool.starmap(_writer, [(path, w) for w in range(WRITERS)])
    assert sum(failures) == 0
    total = WRITERS * TX_PER_WRITER
    assert db.q("SELECT COUNT(*) AS n FROM t")[0]["n"] == total
    assert db.q("SELECT v FROM counter")[0]["v"] == total          # read-modify-write never raced
