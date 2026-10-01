"""Context-quality evaluation and the agent regression suite.

evals/<name>.eval.yaml (in any repo):
    - name: why-k3s
      task: "Why do we run K3s instead of full Kubernetes?"
      project: local-intelligence-fabric
      budget_tokens: 1500
      must_include: [use-k3s-single-node, dgx-spark-host]      # critical context (recall)
      relevant: [single-node-primary]                          # acceptable extras (precision)
      must_not_include: []
      answer:                                                  # graph-answerable questions (regression)
        why: use-k3s-single-node
        expect_evidence: [k3s-vs-kubernetes-notes]

Metrics per case: critical recall (missing critical context), precision (irrelevant context),
tokens used. Aggregate `context_recovery` = cases with full critical recall / cases.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from lif.knowledge.ops import Knowledge


def _ids(keys: list[str]) -> set[str]:
    return {k.split("::")[-1] for k in keys}


def run(kn: "Knowledge", suite: str | None = None) -> dict:
    cases = []
    for r in kn.ws.all_repos():
        for f in r.glob("evals", "*.eval.yaml"):
            if suite and f.name.split(".")[0] != suite:
                continue
            for c in yaml.safe_load(f.read_text()) or []:
                cases.append((f.name, c))
    results = []
    for fname, c in cases:
        pkg = kn.context.assemble(c["task"], c.get("project"), c.get("profile"), int(c.get("budget_tokens", 2000)))
        got = _ids([i["key"] for i in pkg["items"]])
        must, rel, bad = set(c.get("must_include") or []), set(c.get("relevant") or []), set(c.get("must_not_include") or [])
        missing = sorted(must - got)
        useful = got & (must | rel)
        res = {"suite": fname, "case": c.get("name", c["task"][:40]), "items": len(got), "tokens": pkg["used_tokens"],
               "critical_recall": round(1 - len(missing) / len(must), 3) if must else 1.0, "missing": missing,
               "precision": round(len(useful) / len(got), 3) if got and (must or rel) else None,
               "forbidden": sorted(got & bad), "profile": pkg["profile"]["name"]}
        ans = c.get("answer") or {}
        if ans.get("why"):
            try:
                w = kn.graph.why(ans["why"])
                ev = _ids([e["key"] for e in w["evidence"]])
                res["why_ok"] = set(ans.get("expect_evidence") or []) <= ev
            except KeyError:
                res["why_ok"] = False
        if ans.get("reconsider"):
            open_keys = _ids([r["key"] for r in kn.store.reconsiderations()])
            res["reconsider_ok"] = set(ans["reconsider"]) <= open_keys
        if ans.get("evidence_for"):
            t = kn.graph.trace_claim(ans["evidence_for"])
            res["trace_ok"] = any(g["passage"] for g in t["groundings"])
        res["ok"] = not missing and not res["forbidden"] and all(
            res.get(k, True) for k in ("why_ok", "reconsider_ok", "trace_ok"))
        results.append(res)
    n = len(results) or 1
    prec = [r["precision"] for r in results if r["precision"] is not None]
    return {"cases": results, "passed": sum(r["ok"] for r in results), "total": len(results),
            "context_recovery": round(sum(1 for r in results if not r["missing"]) / n, 3),
            "mean_precision": round(sum(prec) / len(prec), 3) if prec else None,
            "mean_tokens": round(sum(r["tokens"] for r in results) / n, 1)}
