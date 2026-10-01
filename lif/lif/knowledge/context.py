"""Context assembly, session checkpoints, resume and agent bootstrap.

    task ──► analyze (explicit [[refs]], terms, profile) ──► seeds (refs + hybrid search)
         ──► graph expansion (≤2 hops, both directions) ──► score (distance, profile type weight,
             relevance, recency, criticality, pinning) ──► always-include (project, constraints,
             open reconsiderations, critical diagnostics) ──► token budget ──► context package

Every item in a package carries provenance (object key, repo + version, file:line, last change and
who made it, the evidence it rests on, the owning project). Nothing is restored from transcripts:
sessions resume from the graph plus the last `session` checkpoint object.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from lif.knowledge.parse import WIKILINK
from lif.knowledge.search import terms

if TYPE_CHECKING:
    from lif.knowledge.ops import Knowledge

CRITICAL_STATUS = {"challenged", "invalidated", "open", "blocked", "contested"}
LOW_STATUS = {"superseded", "deprecated", "retired", "rejected", "retracted", "cancelled"}
OPEN_WORK = ("open", "in-progress", "blocked")
CLASS_ORDER = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]


def tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class ContextEngine:
    def __init__(self, kn: "Knowledge"):
        self.kn = kn
        self.s = kn.store
        self.g = kn.graph

    # ── profiles ─────────────────────────────────────────────────────────────
    def profiles(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.kn.ws.all_repos():
            for p in r.glob("profiles", "*.profile.yaml"):
                raw = yaml.safe_load(p.read_text()) or {}
                out[str(raw.get("name", p.stem.split(".")[0]))] = raw
        return out

    def choose_profile(self, task: str) -> dict:
        profiles = self.profiles()
        low = task.lower()
        scores = {n: sum(1 for k in (p.get("keywords") or []) if k.lower() in low) for n, p in profiles.items()}
        scores = {k: v for k, v in scores.items() if v}
        ranked = sorted(scores.values(), reverse=True)
        if ranked and (len(ranked) == 1 or ranked[0] - ranked[1] >= 2):
            # deterministic answer is decisive: no need to ask the fabric
            return {"name": max(scores, key=scores.get), "by": "keywords"}
        from lif.knowledge.decisions import decide
        d = decide("knowledge-context-profile", {"task": task[:500], "keyword_scores": scores})
        return {"name": d["decision"] if d["decision"] in profiles else "engineering", "by": d["provider"],
                "confidence": d["confidence"]}

    # ── assembly ─────────────────────────────────────────────────────────────
    def assemble(self, task: str, project: str | None = None, profile: str | None = None,
                 budget_tokens: int = 2000, pins: list[str] | None = None, depth: int = 2) -> dict:
        t0 = time.perf_counter()
        self.kn.refresh()
        prof_info = {"name": profile, "by": "caller"} if profile else self.choose_profile(task)
        prof = self.profiles().get(prof_info["name"], {"weights": {}, "default_weight": 1.0})
        weights, dflt = prof.get("weights") or {}, float(prof.get("default_weight", 1.0))

        scores: dict[str, float] = {}
        why: dict[str, str] = {}

        def bump(key: str, score: float, reason: str) -> None:
            top = key.split("^", 1)[0]
            if top not in scores or score > scores[top]:
                scores[top], why[top] = max(score, scores.get(top, 0)), reason

        for m in WIKILINK.finditer(task):              # explicit references are the strongest signal
            k = self.s.find_key(m.group(1).strip())
            if k:
                bump(k, 3.0, "named in the task")
        for p in pins or []:
            k = self.s.find_key(p)
            if k:
                bump(k, 3.0, "pinned by the caller")
        for o in self.s.objects("sub IS NULL AND json_extract(fields,'$.pinned')=1", limit=50):
            bump(o["key"], 1.5, "pinned in the knowledge base")
        res = self.kn.search.search(task, project=project, limit=15, log=False) if terms(task) else {"results": []}
        top = max([r["score"] for r in res["results"]] or [1]) or 1
        for r in res["results"]:
            bump(r["key"], 2.0 * r["score"] / top, "matches the task")

        # graph expansion from seeds
        seeds = sorted(scores.items(), key=lambda kv: -kv[1])[:20]
        for k, sc in seeds:
            for direction in ("dependencies", "dependents"):
                for w in self.s.walk(k, direction, depth, propagating=False):
                    via = w["path"][1] if len(w["path"]) > 1 else ""
                    bump(w["node"], sc * (0.5 ** w["depth"]),
                         f"{'cited by' if direction == 'dependencies' else 'depends on'} {k.split('::')[-1]} ({via})")

        # always-include: open reconsiderations and critical diagnostics touching the candidate set
        recons = [r for r in self.s.reconsiderations() if r["key"] in scores or r["trigger"] in scores]
        for r in recons:
            bump(r["key"], 2.5, f"RECONSIDERATION REQUIRED: {r['reason']}")
            bump(r["trigger"], 2.2, "changed input to work in this context")

        today = dt.date.today()
        items = []
        for k, base in scores.items():
            o = self.s.get(k)
            if o is None or (project and o["project"] and o["project"] != project and o["source"] == "workspace"):
                continue
            w = float(weights.get(o["type_name"], dflt))
            age = (today - dt.date.fromisoformat(o["updated"])).days
            recency = 1 + 0.3 / (1 + max(age, 0) / 30)
            crit = 1.4 if o["status"] in CRITICAL_STATUS else 0.5 if o["status"] in LOW_STATUS else 1.0
            if o["type_name"] == "decision" and o["status"] == "accepted":
                crit *= 1.2
            items.append({"key": k, "score": round(base * w * recency * crit, 4), "o": o, "why": why[k]})
        items.sort(key=lambda i: -i["score"])

        # budget
        header = self._project_header(project)
        used = tokens(header["text"]) if header else 0
        chosen, dropped = [], 0
        for it in items:
            text = self._render_item(it["o"])
            cost = tokens(text)
            if used + cost > budget_tokens:
                dropped += 1
                continue
            used += cost
            o = it["o"]
            f = o["fields"]
            summary = str(f.get("statement") or f.get("summary") or f.get("lesson") or f.get("question") or
                          re.sub(r"\s+", " ", o["body"]).strip())[:240]
            chosen.append({"key": o["key"], "type": o["type_name"], "title": o["title"], "status": o["status"],
                           "data_class": self.data_class(o), "summary": summary, "score": it["score"], "why": it["why"], "text": text, "tokens": cost,
                           "provenance": self.g.provenance(o)})
        constraints = [c for c in chosen if c["type"] == "assumption" or "constraint" in
                       (self.s.get(c["key"])["fields"].get("tags") or [])]
        diags = [d for d in self.s.diagnostics() if d["severity"] in ("error", "warning") and d["key"] and
                 d["key"].split("^")[0] in {c["key"] for c in chosen}][:10]
        pkg = {"task": task, "project": project, "profile": prof_info, "budget_tokens": budget_tokens,
               "data_class": max((c["data_class"] for c in chosen), key=CLASS_ORDER.index, default="PUBLIC"),
               "used_tokens": used, "candidates": len(items), "dropped": dropped, "header": header,
               "items": chosen, "constraints": [c["key"] for c in constraints],
               "reconsiderations": [{k: r[k] for k in ("key", "trigger", "reason", "priority")} for r in recons],
               "diagnostics": [{k: d[k] for k in ("code", "severity", "key", "message")} for d in diags],
               "skills": self.skills_for(chosen), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
        pkg["markdown"] = self.render(pkg)
        _observe(pkg["elapsed_ms"] / 1000)
        return pkg

    def data_class(self, o: dict) -> str:
        """Object field, else the repo manifest's `data_class`, else CONFIDENTIAL (LIF's privacy default)."""
        v = str(o["fields"].get("data_class") or "").upper()
        if v in CLASS_ORDER:
            return v
        repo = self.kn.ws.repo(o["repo"])
        v = str((repo.manifest if repo else {}).get("data_class") or "").upper()
        return v if v in CLASS_ORDER else "CONFIDENTIAL"

    def _project_header(self, project: str | None) -> dict | None:
        if not project:
            return None
        k = self.s.find_key(project) or self.s.find_key(f"{project}::{project}")
        o = self.s.get(k) if k else None
        repo = self.kn.ws.repo(project)
        summary = (o["fields"].get("summary") if o else None) or (repo.description if repo else "")
        return {"key": k, "summary": summary, "text": f"Project {project}: {summary}"}

    def _render_item(self, o: dict) -> str:
        f = o["fields"]
        bits = [f"[{o['type_name']}] {o['title']} ({o['key']})" + (f" — {o['status']}" if o["status"] else "")]
        for name in ("statement", "question", "selected", "summary", "lesson", "observation", "answer"):
            if f.get(name) and f.get(name) != o["title"]:
                bits.append(f"  {name}: {str(f[name]).strip()[:300]}")
        links = [e for e in self.s.edges_from(o["key"]) if e["kind"] == "field" and e["dst"]]
        by_field: dict[str, list[str]] = {}
        for e in links:
            by_field.setdefault(e["field"], []).append(e["dst"].split("::")[-1])
        for fld, ts in list(by_field.items())[:6]:
            bits.append(f"  {fld}: " + ", ".join(ts[:6]))
        body = re.sub(r"\s+", " ", re.sub(r"\s\^[\w-]+", "", o["body"])).strip()
        if body:
            bits.append("  " + body[:400])
        bits.append(f"  source: {Path(o['path']).name}:{o['line']} · updated {o['updated']}"
                    + (f" by {o['updated_by']}" if o["updated_by"] else ""))
        return "\n".join(bits)

    def skills_for(self, chosen: list[dict]) -> list[dict]:
        keys = {c["key"] for c in chosen}
        out = []
        for s in self.s.objects("type_name='skill' AND sub IS NULL"):
            linked = {e["dst"] for e in self.s.edges_from(s["key"]) if e["dst"]}
            if s["key"] in keys or linked & keys:
                out.append({"key": s["key"], "name": s["fields"].get("name") or s["id"], "title": s["title"],
                            "description": s["fields"].get("description", "")})
        return out

    @staticmethod
    def render(pkg: dict) -> str:
        lines = [f"# Context for: {pkg['task']}", ""]
        if pkg.get("header"):
            lines += [pkg["header"]["text"], ""]
        if pkg["reconsiderations"]:
            lines.append("## Reconsideration required")
            lines += [f"- {r['key']} ← {r['trigger']}: {r['reason']}" for r in pkg["reconsiderations"]]
            lines.append("")
        lines.append(f"## Knowledge (profile {pkg['profile']['name']}, {pkg['used_tokens']}/{pkg['budget_tokens']} tokens)")
        for it in pkg["items"]:
            lines += [it["text"], f"  why included: {it['why']}", ""]
        if pkg["diagnostics"]:
            lines.append("## Open diagnostics")
            lines += [f"- {d['severity']} {d['code']} {d['key']}: {d['message']}" for d in pkg["diagnostics"]]
        if pkg["skills"]:
            lines.append("## Relevant skills")
            lines += [f"- {s['name']}: {s['description']}" for s in pkg["skills"]]
        return "\n".join(lines).strip() + "\n"

    # ── sessions ─────────────────────────────────────────────────────────────
    def last_checkpoint(self, project: str | None) -> dict | None:
        rows = self.s.objects("type_name='session' AND sub IS NULL" + (" AND project=?" if project else ""),
                              (project,) if project else (), order="json_extract(fields,'$.ended') DESC, updated DESC",
                              limit=1)
        return rows[0] if rows else None

    def resume(self, project: str | None = None, limit: int = 10) -> dict:
        """Everything a fresh agent session needs to continue — from the graph, not a transcript."""
        t0 = time.perf_counter()
        self.kn.refresh()
        pw, pa = (" AND project=?", (project,)) if project else ("", ())
        last = self.last_checkpoint(project)
        since = (last["fields"].get("ended") or last["updated"])[:19] if last else \
            (dt.date.today() - dt.timedelta(days=14)).isoformat()
        try:
            since_ts = dt.datetime.fromisoformat(since).timestamp() + (1 if last else 0)
        except ValueError:
            since_ts = 0.0
        objs = lambda where, args=(), lim=limit, order="updated DESC, key": [
            self._brief(o) for o in self.s.objects(f"sub IS NULL AND source='workspace'{pw} AND " + where,
                                                   pa + tuple(args), order=order, limit=lim)]
        decisions = objs("type_name='decision' AND status IN ('accepted','proposed')")
        for d in decisions:
            w = self.g.why(d["key"])
            d["because"] = [e["key"] for e in w["evidence"]][:4]
            d["assumes"] = [{"key": a["key"], "status": a.get("status")} for a in w["assumptions"]][:4]
        recon = [r for r in self.s.reconsiderations() if not project or (self.s.get(r["key"]) or {}).get("project") == project]
        diag = [d for d in self.s.diagnostics() if d["severity"] in ("error", "warning") and
                (not project or (self.s.get((d["key"] or "").split("^")[0]) or {}).get("project") == project)]
        out = {
            "project": self._project_header(project) if project else None,
            "health": {k: v for k, v in self.g.health().items() if k in (
                "objects", "errors", "warnings", "reconsideration_queue", "open_questions", "open_tasks")},
            "last_checkpoint": self._session(last) if last else None,
            "recent_changes": objs("mtime > ? AND type_name != 'session'", (since_ts,), 25),
            "important_decisions": decisions,
            "open_tasks": objs("type_name='task' AND status IN ('open','in-progress','blocked')", lim=25),
            "open_questions": objs("type_name='question' AND status='open'", lim=25),
            "changed_assumptions": objs("type_name='assumption' AND (status IN ('challenged','invalidated') "
                                        "OR mtime > ?)", (since_ts,)),
            "pending_reviews": [{k: r[k] for k in ("key", "trigger", "reason", "priority", "since")} for r in recon],
            "unresolved_diagnostics": [{k: d[k] for k in ("code", "severity", "key", "message")} for d in diag[:25]],
            "relevant_artifacts": objs("type_name IN ('artifact','result','benchmark') AND mtime > ?", (since_ts,)),
            "since": since,
        }
        out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["markdown"] = self.render_resume(out)
        return out

    def _brief(self, o: dict) -> dict:
        return {"key": o["key"], "type": o["type_name"], "title": o["title"], "status": o["status"],
                "updated": o["updated"], "path": f"{Path(o['path']).name}:{o['line']}"}

    def _session(self, o: dict) -> dict:
        f = o["fields"]
        links = {}
        for e in self.s.edges_from(o["key"]):
            if e["kind"] == "field":
                links.setdefault(e["field"], []).append(e["dst"] or e["raw"])
        return {"key": o["key"], "agent": f.get("agent"), "started": f.get("started"), "ended": f.get("ended"),
                "summary": o["body"].strip()[:1500], "next": f.get("next") or [], **links}

    @staticmethod
    def render_resume(r: dict) -> str:
        L = []
        if r.get("project"):
            L += [f"# Resume: {r['project']['text']}", ""]
        h = r["health"]
        L.append(f"Knowledge: {h['objects']} objects · {h['errors']} errors · {h['warnings']} warnings · "
                 f"{h['reconsideration_queue']} reconsiderations · {h['open_tasks']} open tasks · "
                 f"{h['open_questions']} open questions")
        cp = r.get("last_checkpoint")
        if cp:
            L += ["", f"## Last session {cp['key']} ({cp.get('agent')}, ended {cp.get('ended')})", cp["summary"]]
            if cp["next"]:
                L += ["Next:"] + [f"- {n}" for n in cp["next"]]
        sections = [("Pending reviews (reconsideration required)", "pending_reviews"),
                    ("Changed assumptions", "changed_assumptions"), ("Open tasks", "open_tasks"),
                    ("Open questions", "open_questions"), ("Important decisions", "important_decisions"),
                    (f"Changes since {r['since']}", "recent_changes"), ("Relevant artifacts", "relevant_artifacts"),
                    ("Unresolved diagnostics", "unresolved_diagnostics")]
        for title, k in sections:
            rows = r.get(k) or []
            if not rows:
                continue
            L += ["", f"## {title}"]
            for x in rows:
                if "reason" in x:
                    L.append(f"- [{x['priority']}] {x['key']} ← {x['trigger']}: {x['reason']}")
                elif "message" in x:
                    L.append(f"- {x['severity']} {x['code']} {x['key']}: {x['message']}")
                else:
                    extra = ""
                    if x.get("because"):
                        extra = f" — because {', '.join(b.split('::')[-1] for b in x['because'])}"
                    L.append(f"- {x['title']} ({x['key']}, {x['status'] or x['type']}){extra}")
        return "\n".join(L) + "\n"

    def checkpoint(self, project: str, agent: str, summary: str, completed: list[str] | None = None,
                   decisions: list[str] | None = None, questions: list[str] | None = None,
                   artifacts: list[str] | None = None, changed: list[str] | None = None,
                   next: list[str] | None = None, started: str | None = None, recovered: bool | None = None,
                   repo: str | None = None) -> dict:
        """End-of-session capture: write a `session` object linking the work it did."""
        from lif.knowledge.writer import as_link, slug
        now = dt.datetime.now().replace(microsecond=0)
        prev = self.last_checkpoint(project)
        fields: dict[str, Any] = {"title": f"{agent} session {now:%Y-%m-%d %H:%M}", "project": project,
                                  "agent": agent, "started": started or now.isoformat(), "ended": now.isoformat()}
        for name, vals in (("completed", completed), ("decisions", decisions), ("questions", questions),
                           ("artifacts", artifacts), ("changed", changed)):
            if vals:
                fields[name] = as_link(vals)
        if next:
            fields["next"] = list(next)
        if prev:
            fields["resumed_from"] = f"[[{prev['id']}]]"
            if recovered is not None:
                fields["recovered"] = bool(recovered)
        oid = f"{now:%Y%m%d-%H%M%S}-{slug(agent, 30)}"
        return self.kn.writer(agent).create("session", fields, summary, id=oid, repo=repo or self._repo_of(project),
                                            folder="sessions")

    def _repo_of(self, project: str | None) -> str | None:
        for r in self.kn.ws.repos:
            if r.name == project or (r.manifest.get("project") == project):
                return r.name
        return None

    # ── bootstrap ────────────────────────────────────────────────────────────
    def bootstrap(self, repo: str | None = None) -> dict:
        """What an agent should see on entering a repo (MCP resource / CLAUDE.md-style briefing)."""
        self.kn.refresh()
        r = self.kn.ws.repo(repo) if repo else (self.kn.ws.repos[0] if self.kn.ws.repos else None)
        if r is None:
            return {"error": "no repo"}
        lock = r.read_lock().get("packages", {})
        used = self.s.db.q("SELECT type_name, count(*) n FROM objects WHERE repo=? AND sub IS NULL GROUP BY type_name "
                           "ORDER BY n DESC", (r.name,))
        from lif.knowledge.mcp import TOOLS
        from lif.knowledge.skills import list_skills
        project = r.manifest.get("project") or r.name
        res = self.resume(project)
        return {"repo": r.name, "version": r.version, "description": r.description, "manifest": r.manifest,
                "dependencies": [{"name": d.name, "constraint": d.version,
                                  "locked": (lock.get(d.name) or {}).get("version")} for d in r.dependencies],
                "types_in_use": used, "skills": list_skills(self.kn),
                "tools": [t["name"] for t in TOOLS],
                "critical_diagnostics": [d for d in res["unresolved_diagnostics"] if d["severity"] == "error"],
                "recent_decisions": res["important_decisions"][:5], "open_work": res["open_tasks"][:10],
                "pending_reviews": res["pending_reviews"], "last_checkpoint": res["last_checkpoint"]}


try:
    from prometheus_client import Histogram
    _LAT = Histogram("lif_knowledge_context_seconds", "Context assembly latency",
                     buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
except Exception:                       # pragma: no cover
    _LAT = None


def _observe(sec: float) -> None:
    if _LAT is not None:
        _LAT.observe(sec)


_OPEN: dict[str, "Knowledge"] = {}


def assemble(task: str, *, root: str | Path | None = None, project: str | None = None, profile: str | None = None,
             budget_tokens: int = 2000) -> dict:
    """Module-level entry point for other LIF components (e.g. the decision state compiler)."""
    from lif.knowledge.ops import Knowledge
    k = str(root or "")
    if k not in _OPEN:
        _OPEN[k] = Knowledge.open(root)
    return _OPEN[k].context.assemble(task, project, profile, budget_tokens)
