"""Generate apps/console/src/api/contracts.gen.ts from contracts.py (spec §90: typed contracts).

    python -m lif.console.gen_ts            # write the file
    python -m lif.console.gen_ts --check    # exit 1 if the committed file has drifted

Deterministic by construction: models and Literal aliases are emitted in source-definition order,
output is LF-only with one trailing newline, and nothing depends on the cwd, clock or hash order.
Mapping: str→string, int/float→number, bool→boolean, None→null, Any→unknown, list[T]→T[],
dict[str, T]→Record<string, T>, models by name, module-level Literal aliases as `export type`.
A field whose default is None becomes `name?: T | null`; every other field is required (the server
always serialises it). Class docstrings, Field descriptions and trailing `  # comments` on field lines
become JSDoc, so page authors see the same guidance as backend authors.
"""
from __future__ import annotations

import inspect
import re
import sys
import types
import typing
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from lif.console import contracts

OUT = Path(__file__).resolve().parents[2] / "apps" / "console" / "src" / "api" / "contracts.gen.ts"
HEADER = ("// generated — do not edit. Source: lif/lif/console/contracts.py\n"
          "// Regenerate: lif/.venv/bin/python -m lif.console.gen_ts\n")


def _members() -> tuple[dict[Any, str], list[tuple[str, Any]]]:
    """(Literal value → alias name, ordered [(name, alias|model)]) from the contracts module."""
    aliases: dict[Any, str] = {}
    ordered: list[tuple[str, Any]] = []
    for name, obj in vars(contracts).items():
        if get_origin(obj) is Literal:
            if obj in aliases:
                raise SystemExit(f"duplicate Literal alias {name} == {aliases[obj]}")
            aliases[obj] = name
            ordered.append((name, obj))
        elif (isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == contracts.__name__
              and obj is not contracts.Contract):
            ordered.append((name, obj))
    return aliases, ordered


def _lit(values: tuple) -> str:
    return " | ".join(_js(v) for v in values)


def _js(v: Any) -> str:
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _ts(tp: Any, aliases: dict[Any, str]) -> str:
    if tp is type(None):
        return "null"
    if tp is Any:
        return "unknown"
    if tp in (str,):
        return "string"
    if tp in (int, float):
        return "number"
    if tp is bool:
        return "boolean"
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return tp.__name__
    origin = get_origin(tp)
    if origin is Literal:
        return aliases.get(tp) or _lit(get_args(tp))
    if origin in (Union, types.UnionType):
        parts: list[str] = []
        for a in get_args(tp):
            t = _ts(a, aliases)
            if t not in parts:
                parts.append(t)
        return " | ".join(parts)
    if origin in (list, typing.List):
        (inner,) = get_args(tp)
        t = _ts(inner, aliases)
        return f"({t})[]" if " | " in t else f"{t}[]"
    if origin in (dict, typing.Dict):
        k, v = get_args(tp)
        if k is not str:
            raise SystemExit(f"only str-keyed dicts are supported, got {tp!r}")
        return f"Record<string, {_ts(v, aliases)}>"
    raise SystemExit(f"gen_ts: unsupported annotation {tp!r}")


_FIELD_COMMENT = re.compile(r"^\s+(\w+):[^#]*?\s{2,}#\s?(.*)$")


def _comments(model: type) -> dict[str, str]:
    """Trailing `# …` comments on field lines, keyed by field name."""
    out: dict[str, str] = {}
    for line in inspect.getsource(model).splitlines():
        if m := _FIELD_COMMENT.match(line):
            out[m.group(1)] = m.group(2).strip()
    return out


def _doc(text: str | None, indent: str = "") -> list[str]:
    if not text:
        return []
    lines = [ln.strip() for ln in text.strip().splitlines()]
    if len(lines) == 1:
        return [f"{indent}/** {lines[0]} */"]
    return [f"{indent}/**", *(f"{indent} * {ln}".rstrip() for ln in lines), f"{indent} */"]


def render() -> str:
    aliases, ordered = _members()
    out = [HEADER]
    for name, obj in ordered:
        if get_origin(obj) is Literal:
            out.append(f"export type {name} = {_lit(get_args(obj))};\n")
            continue
        body = _doc(obj.__doc__)
        body.append(f"export interface {name} {{")
        notes = _comments(obj)
        for fname, f in obj.model_fields.items():
            body += _doc(f.description or notes.get(fname), "  ")
            t = _ts(f.annotation, aliases)
            if not f.is_required() and f.default is None and f.default_factory is None:
                if "null" not in t.split(" | "):
                    t = f"{t} | null"
                body.append(f"  {fname}?: {t};")
            else:
                body.append(f"  {fname}: {t};")
        body.append("}\n")
        out.append("\n".join(body))
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = render()
    if "--check" in args:
        current = OUT.read_bytes() if OUT.exists() else b""
        if current != text.encode():
            print(f"{OUT} is out of date: run python -m lif.console.gen_ts", file=sys.stderr)
            return 1
        print(f"{OUT.name} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(text.encode())
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
