"""Explanation packages: reusable, versioned ExplanationSpecs (spec §74–§75).

    explanation-packages/<package>/<name>.yaml   {package, name, version, match: [regex], spec: ExplanationSpec}

`find(question)` returns the first entry whose pattern matches, so a known question starts from a
reviewed spec instead of a fresh build, offline and with no model. Roots: $LIF_EXPLANATION_PACKAGES
(one or more directories separated by os.pathsep, earlier roots win), else the repository's
`explanation-packages/`. Shipped packages are generic: no measurements, no claims about a live system.
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


SHIPPED = Path(__file__).resolve().parents[2] / "explanation-packages"


def roots() -> list[Path]:
    env = os.environ.get("LIF_EXPLANATION_PACKAGES")
    return [Path(p) for p in env.split(os.pathsep) if p] if env else [SHIPPED]


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
    if base is not None:
        return _entries(str(base))
    return tuple(e for r in roots() for e in _entries(str(r)))


def find(question: str, base: Path | None = None) -> Entry | None:
    return next((e for e in entries(base) if e.matches(question)), None)
