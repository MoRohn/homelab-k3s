"""Structured JSON logs on stdout (collected by the K3s node; no extra log stack)."""
from __future__ import annotations

import json
import logging
import sys
import time


class _Json(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {"ts": round(time.time(), 3), "lvl": r.levelname, "logger": r.name, "msg": r.getMessage()}
        out.update(getattr(r, "fields", {}) or {})
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out, default=str)


def setup(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_Json())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def event(logger: logging.Logger, msg: str, **fields) -> None:
    logger.info(msg, extra={"fields": fields})
