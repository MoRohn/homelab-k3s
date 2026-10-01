"""Skills: methods made executable.

skills/<name>/
    SKILL.md      front matter `type: skill` (name, version, description, when, inputs, outputs, method)
                  + the steps an agent follows. It is also a graph object, linked to its method.
    checks.yaml   deterministic graph checks run by `knowledge skill run` (before/after the agent acts)
    tools.yaml    MCP tools the skill expects to use
    tests/*.yaml  cases run against tests/workspace/ (a fixture workspace) by `knowledge skill test`

checks.yaml:
    inputs:
      candidate: {type: model, required: true}
    checks:
      - id: has-benchmark
        description: The candidate has local benchmark evidence
        query: {type: benchmark, links_to: "{candidate}"}     # graph.find(); expect nonempty|empty|{min: n}
        expect: nonempty
        because: "[[lesson-oom-during-benchmark]]"            # provenance: why this check exists
      - id: fits
        object: "{candidate}"
        where: ["hardware_fit!=no_fit"]
      - id: clean
        no_diagnostics: "{candidate}"
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from lif.knowledge.parse import split_front_matter

if TYPE_CHECKING:
    from lif.knowledge.ops import Knowledge


def _skill_dirs(kn: "Knowledge") -> list[tuple[Any, Path]]:
    return [(r, d) for r in kn.ws.all_repos() for d in r.skills()]


def _meta(d: Path) -> tuple[dict, str]:
    fm, body, _ = split_front_matter((d / "SKILL.md").read_text())
    return (yaml.safe_load(fm) or {}) if fm else {}, body


def list_skills(kn: "Knowledge") -> list[dict]:
    out = []
    for r, d in _skill_dirs(kn):
        meta, _ = _meta(d)
        out.append({"name": meta.get("name", d.name), "version": meta.get("version", 1), "repo": r.name,
                    "repo_version": r.version, "description": meta.get("description", ""),
                    "when": meta.get("when", ""), "method": meta.get("method"), "path": str(d),
                    "has_checks": (d / "checks.yaml").exists(), "has_tests": (d / "tests").exists()})
    return out


def find_skill(kn: "Knowledge", name: str) -> tuple[Any, Path]:
    hits = [(r, d) for r, d in _skill_dirs(kn) if d.name == name or f"{r.name}::{d.name}" == name]
    if not hits:
        raise KeyError(f"no skill '{name}' (installed packages: {[r.name for r in kn.ws.packages.values()]})")
    hits.sort(key=lambda x: x[0].source != "workspace")    # a project's own skill overrides a package's
    return hits[0]


def _fill(v: Any, inputs: dict) -> Any:
    if isinstance(v, str):
        for k, val in inputs.items():
            v = v.replace("{" + k + "}", str(val))
        return v
    if isinstance(v, list):
        return [_fill(x, inputs) for x in v]
    if isinstance(v, dict):
        return {k: _fill(x, inputs) for k, x in v.items()}
    return v


def run_checks(kn: "Knowledge", spec: dict, inputs: dict) -> dict:
    g = kn.graph
    problems, results = [], []
    for name, d in (spec.get("inputs") or {}).items():
        d = d if isinstance(d, dict) else {"type": d}
        val = inputs.get(name)
        if val is None:
            if d.get("required", True):
                problems.append(f"missing input '{name}'")
            continue
        if d.get("type") and d["type"] not in ("string", "int", "text"):
            k = kn.store.find_key(str(val))
            if k is None:
                problems.append(f"input '{name}': no object '{val}'")
                continue
            o = kn.store.get(k)
            allowed = g._subtypes([d["type"]])
            if o["type_name"] not in allowed:
                problems.append(f"input '{name}': expected {d['type']}, received {o['type_name']}")
            inputs[name] = k
    if problems:
        return {"passed": False, "input_errors": problems, "checks": []}
    for c in spec.get("checks") or []:
        c = _fill(c, inputs)
        res: dict[str, Any] = {"id": c["id"], "description": c.get("description", ""),
                               "severity": c.get("severity", "error"), "because": c.get("because")}
        try:
            if "query" in c:
                rows = g.find(**c["query"])
                exp = c.get("expect", "nonempty")
                n = len(rows)
                ok = (n > 0 if exp == "nonempty" else n == 0 if exp == "empty" else
                      n >= int(exp.get("min", 0)) and n <= int(exp.get("max", 10 ** 9)))
                res.update(passed=ok, found=[r["key"] for r in rows[:10]])
            elif "object" in c:
                o = kn.store.get(g.key(c["object"]))
                ok = all(g._match(o, w) for w in c.get("where") or [])
                res.update(passed=ok)
            elif "no_diagnostics" in c:
                k = g.key(c["no_diagnostics"])
                ds = [d for d in kn.store.diagnostics(key=k) if d["severity"] == "error"]
                res.update(passed=not ds, diagnostics=[d["message"] for d in ds])
            else:
                res.update(passed=False, error="unknown check kind")
        except (KeyError, ValueError) as e:
            res.update(passed=False, error=str(e))
        results.append(res)
    failed = [r for r in results if not r["passed"] and r["severity"] == "error"]
    return {"passed": not failed, "checks": results, "failed": [r["id"] for r in failed],
            "warnings": [r["id"] for r in results if not r["passed"] and r["severity"] != "error"]}


def run_skill(kn: "Knowledge", name: str, inputs: dict | None = None) -> dict:
    kn.refresh()
    repo, d = find_skill(kn, name)
    meta, body = _meta(d)
    spec = yaml.safe_load((d / "checks.yaml").read_text()) if (d / "checks.yaml").exists() else {}
    tools = yaml.safe_load((d / "tools.yaml").read_text()) if (d / "tools.yaml").exists() else {}
    res = run_checks(kn, spec or {}, dict(inputs or {}))
    return {"skill": meta.get("name", d.name), "version": meta.get("version", 1), "repo": repo.name,
            "repo_version": repo.version, "method": meta.get("method"), "inputs": inputs or {}, **res,
            "tools": (tools or {}).get("tools", []), "instructions": body.strip(),
            "outputs": meta.get("outputs", [])}


def test_skill(kn: "Knowledge", name: str) -> dict:
    """Run a skill's test cases against its fixture workspace (compiled in memory)."""
    from lif.knowledge.ops import Knowledge
    repo, d = find_skill(kn, name)
    tdir = d / "tests"
    if not tdir.exists():
        return {"skill": name, "cases": [], "passed": True, "note": "no tests"}
    fixture = Knowledge.open(tdir / "workspace", index=":memory:", registry=kn.ws.registry.root)
    cases = []
    for f in sorted(tdir.glob("*.yaml")):
        for case in yaml.safe_load(f.read_text()) or []:
            got = run_skill(fixture, name, dict(case.get("inputs") or {}))
            exp = case.get("expect") or {}
            ok = True
            if "passed" in exp:
                ok &= got["passed"] == exp["passed"]
            if "failed" in exp:
                ok &= sorted(got.get("failed") or []) == sorted(exp["failed"])
            if "input_errors" in exp:
                ok &= bool(got.get("input_errors")) == bool(exp["input_errors"])
            cases.append({"case": case.get("name", f.stem), "ok": ok, "got": {k: got.get(k) for k in
                                                                              ("passed", "failed", "input_errors")}})
    return {"skill": name, "cases": cases, "passed": all(c["ok"] for c in cases)}
