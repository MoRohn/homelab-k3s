"""Explanation packages: reusable, versioned ExplanationSpecs (spec §74–§75).

    explanation-packages/<package>/<name>.yaml   {package, name, version, match: [regex], spec: ExplanationSpec}

`find(question)` returns the first entry whose pattern matches, so a known question starts from a
reviewed spec instead of a fresh build, offline and with no model. Root: $LIF_EXPLANATION_PACKAGES,
else the repository's `explanation-packages/`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from lif.understanding.spec import ExplanationSpec, load


@dataclass(frozen=True)
class Entry:
    package: str
    name: str
    version: int
    patterns: tuple[re.Pattern[str], ...]
    spec: ExplanationSpec
    path: str

    @property
    def ref(self) -> str:
        return f"{self.package}/{self.name}@{self.version}"

    def matches(self, question: str) -> bool:
        return any(p.search(question) for p in self.patterns)


def root() -> Path:
    env = os.environ.get("LIF_EXPLANATION_PACKAGES")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "explanation-packages"


def load_entry(path: Path) -> Entry:
    raw = yaml.safe_load(path.read_text())
    spec = load(raw["spec"])
    return Entry(raw["package"], raw["name"], int(raw.get("version", 1)),
                 tuple(re.compile(p, re.I) for p in raw.get("match", [])), spec, str(path))


@lru_cache(maxsize=4)
def _entries(base: str) -> tuple[Entry, ...]:
    p = Path(base)
    return tuple(load_entry(f) for f in sorted(p.glob("*/*.yaml"))) if p.exists() else ()


def entries(base: Path | None = None) -> tuple[Entry, ...]:
    return _entries(str(base or root()))


def find(question: str, base: Path | None = None) -> Entry | None:
    return next((e for e in entries(base) if e.matches(question)), None)
