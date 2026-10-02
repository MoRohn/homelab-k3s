"""Semantic validation of an ExplanationSpec before anything is rendered (spec §23).

Pydantic already enforces shape. This checks meaning: every reference resolves to an element of the
right kind, causal chains and processes run over concepts that exist, timelines are ordered, equations
only use declared variables, claims are attached to concepts and evidence, low-confidence claims carry
an explicit uncertainty, and source references resolve through a pluggable resolver (knowledge keys,
metrics, Kubernetes objects, URLs). Errors block rendering; warnings are reported with the spec.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass

from lif.understanding.spec import ExplanationSpec, SourceRef

LOW_CONFIDENCE = 0.9        # a claim below this must say why in `uncertainties` (§19)

KNOWLEDGE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*::[a-z0-9][a-z0-9_.-]*$")
K8S_REF = re.compile(r"^(?:[a-z0-9-]+/)?[a-z0-9][a-z0-9.-]*(?:/[a-z0-9][a-z0-9.-]*)?$")
URL = re.compile(r"^https?://[^\s]+$")
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
EQ_FUNCS = {"floor", "ceil", "min", "max", "log", "exp", "sqrt", "abs", "round", "sum"}


@dataclass
class Issue:
    code: str
    severity: str           # error | warning
    message: str
    where: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationResult:
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "issues": [i.to_dict() for i in self.issues]}


Resolver = Callable[[SourceRef], str | None]     # None = resolves; otherwise the reason it does not


def syntax_resolver(ref: SourceRef) -> str | None:
    """Offline check: the reference is well-formed for its kind."""
    r = ref.ref.strip()
    if not r:
        return "empty reference"
    if ref.kind == "knowledge" and not KNOWLEDGE_KEY.match(r):
        return f"knowledge key {r!r} is not repo::slug"
    if ref.kind == "url" and not URL.match(r):
        return f"url {r!r} is not http(s)"
    if ref.kind == "k8s" and not K8S_REF.match(r):
        return f"kubernetes ref {r!r} is not [namespace/]kind-or-name[/name]"
    return None


def knowledge_resolver(exists: Callable[[str], bool], fallback: Resolver = syntax_resolver) -> Resolver:
    """Resolve `knowledge` refs against the knowledge index (e.g. lif.knowledge.store.Store.get)."""
    def resolve(ref: SourceRef) -> str | None:
        err = fallback(ref)
        if err or ref.kind != "knowledge":
            return err
        return None if exists(ref.ref) else f"knowledge object {ref.ref!r} not found"
    return resolve


def validate(spec: ExplanationSpec, resolver: Resolver | None = syntax_resolver) -> ValidationResult:
    issues: list[Issue] = []
    err = lambda code, msg, where="": issues.append(Issue(code, "error", msg, where))      # noqa: E731
    warn = lambda code, msg, where="": issues.append(Issue(code, "warning", msg, where))   # noqa: E731

    # ── identity ──
    kinds: dict[str, str] = {}
    for kind, el in spec.elements():
        if el.id in kinds:
            err("duplicate-id", f"id {el.id!r} is used by {kinds[el.id]} and {kind}", el.id)
        else:
            kinds[el.id] = kind

    def need(id_: str, allowed: tuple[str, ...], where: str, what: str) -> bool:
        k = kinds.get(id_)
        if k is None:
            err("unknown-ref", f"{what} {id_!r} does not exist", where)
            return False
        if k not in allowed:
            err("wrong-kind", f"{what} {id_!r} is a {k}, expected {' or '.join(allowed)}", where)
            return False
        return True

    C, CL, EV, SR, VAR = ("concepts",), ("claims",), ("evidence",), ("source_refs",), ("variables",)

    # ── summary and objectives ──
    need(spec.summary.headline, CL, "summary", "summary headline")
    for c in spec.summary.claims:
        need(c, CL, "summary", "summary claim")
    if not spec.learning_objectives:
        warn("no-objectives", "no learning objectives: the critic cannot test understanding (§62)")
    for o in spec.learning_objectives:
        for c in o.claims:
            need(c, CL, o.id, "objective claim")

    # ── terminology ──
    seen: dict[str, str] = {}
    for t in spec.terms:
        for name in {t.canonical, t.abbreviation, *t.synonyms} - {""}:
            low = name.lower()
            if low in seen and seen[low] != t.canonical:
                err("term-collision", f"{name!r} names both {seen[low]!r} and {t.canonical!r}", "terms")
            seen[low] = t.canonical
    canon = {t.canonical for t in spec.terms}

    # ── concepts, sources, evidence, claims ──
    for s in spec.source_refs:
        if resolver is not None and (why := resolver(s)):
            err("unresolved-source", why, s.id)
    for c in spec.concepts:
        if c.term and c.term not in canon:
            err("unknown-term", f"concept {c.id} names term {c.term!r} that is not in terms", c.id)
        for r in c.source_refs:
            need(r, SR, c.id, "source ref")
    for e in spec.evidence:
        need(e.source, SR, e.id, "evidence source")
    referenced_claims: set[str] = {spec.summary.headline, *spec.summary.claims}
    for c in spec.claims:
        if not c.concepts:
            err("orphan-claim", f"claim {c.id} is not attached to any concept", c.id)
        for x in c.concepts:
            need(x, C, c.id, "claim concept")
        for x in c.evidence:
            need(x, EV, c.id, "claim evidence")
        if c.kind == "observed" and not c.evidence:
            err("observed-without-evidence", f"claim {c.id} is observed but cites no evidence", c.id)
        if c.kind == "inferred" and not c.evidence and not spec.uncertainty_for(c.id):
            warn("unsupported-inference", f"inferred claim {c.id} has neither evidence nor an uncertainty", c.id)
        if c.confidence < LOW_CONFIDENCE and not spec.uncertainty_for(c.id):
            err("hidden-uncertainty", f"claim {c.id} has confidence {c.confidence} but no uncertainty entry", c.id)

    # ── structure ──
    for r in spec.relationships:
        need(r.source, C, r.id, "relationship from")
        need(r.target, C, r.id, "relationship to")
        if r.claim and need(r.claim, CL, r.id, "relationship claim"):
            referenced_claims.add(r.claim)
    for ch in spec.causal_chains:
        if len(ch.steps) < 2:
            err("short-chain", f"causal chain {ch.id} needs at least two steps", ch.id)
        for s in ch.steps:
            need(s, C, ch.id, "causal step")
        if ch.claim and need(ch.claim, CL, ch.id, "chain claim"):
            referenced_claims.add(ch.claim)
    for p in spec.processes:
        if not p.steps:
            err("empty-process", f"process {p.id} has no steps", p.id)
        for s in p.steps:
            need(s, C, p.id, "process step")
    for tl in spec.timelines:
        ts = [e.t for e in tl.events]
        if ts != sorted(ts):
            err("unordered-timeline", f"timeline {tl.id} events are not in time order", tl.id)
        for e in tl.events:
            if e.concept:
                need(e.concept, C, e.id, "timeline concept")
            if e.claim and need(e.claim, CL, e.id, "timeline claim"):
                referenced_claims.add(e.claim)
    for h in spec.hierarchies:
        need(h.root, C, h.id, "hierarchy root")
        for e in h.edges:
            need(e.parent, C, h.id, "hierarchy parent")
            need(e.child, C, h.id, "hierarchy child")
    for cmp in spec.comparisons:
        dims = {d.id for d in cmp.dimensions}
        for it in cmp.items:
            need(it, C, cmp.id, "comparison item")
        for cell in cmp.cells:
            if cell.item not in cmp.items:
                err("bad-cell", f"cell item {cell.item!r} is not compared in {cmp.id}", cmp.id)
            if cell.dimension not in dims:
                err("bad-cell", f"cell dimension {cell.dimension!r} is not in {cmp.id}", cmp.id)
            if cell.claim and need(cell.claim, CL, cmp.id, "cell claim"):
                referenced_claims.add(cell.claim)

    # ── quantities ──
    symbols = {v.symbol for v in spec.variables}
    for v in spec.variables:
        if v.concept:
            need(v.concept, C, v.id, "variable concept")
        if v.kind == "enum" and not v.options:
            err("enum-without-options", f"variable {v.id} is an enum with no options", v.id)
        if v.min is not None and v.max is not None and v.min > v.max:
            err("bad-range", f"variable {v.id} has min > max", v.id)
    for eq in spec.equations:
        lhs, _, rhs = eq.expr.rpartition("=")
        defined = set(IDENT.findall(lhs))           # `slots = …` defines slots
        missing = sorted(set(IDENT.findall(rhs)) - EQ_FUNCS - symbols - defined)
        if missing:
            err("unresolved-variable", f"equation {eq.id} uses undeclared symbols {missing}", eq.id)
        for v in eq.variables:
            need(v, VAR, eq.id, "equation variable")
        if eq.meaning and need(eq.meaning, CL, eq.id, "equation meaning"):
            referenced_claims.add(eq.meaning)

    # ── teaching aids ──
    for ex in spec.examples:
        for x in ex.illustrates:
            need(x, C + CL, ex.id, "example target")
            referenced_claims.add(x)
    for a in spec.analogies:
        for x in a.maps:
            need(x, C, a.id, "analogy concept")
    for k in spec.constraints:
        for x in k.concepts:
            need(x, C, k.id, "constraint concept")
    for u in spec.uncertainties:
        if need(u.about, CL, u.id, "uncertainty claim"):
            referenced_claims.add(u.about)
            cl = spec.claim(u.about)
            if cl is not None and u.level > cl.level:
                err("uncertainty-too-deep", f"uncertainty {u.id} (level {u.level}) would be hidden where claim "
                    f"{cl.id} (level {cl.level}) is shown", u.id)
            if cl is not None and abs(cl.confidence - u.confidence) > 1e-9:
                err("confidence-mismatch", f"uncertainty {u.id} says {u.confidence}, claim {u.about} says "
                    f"{cl.confidence}", u.id)
    for m in spec.misconceptions:
        if need(m.correction, CL, m.id, "misconception correction"):
            referenced_claims.add(m.correction)
    for cp in spec.checkpoints:
        for c in cp.answer_claims:
            need(c, CL, cp.id, "checkpoint answer")
        if cp.objective:
            need(cp.objective, ("learning_objectives",), cp.id, "checkpoint objective")
    for o in spec.learning_objectives:
        referenced_claims.update(o.claims)

    # ── simulation ──
    if spec.simulation is not None:
        from lif.understanding import simulate
        sim = spec.simulation
        prim = simulate.PRIMITIVES.get(sim.primitive)
        if prim is None:
            err("unknown-primitive", f"simulation primitive {sim.primitive!r} is not registered", "simulation")
        for v in sim.variables:
            need(v, VAR, "simulation", "simulation variable")
        for vid, name in sim.bindings.items():
            need(vid, VAR, "simulation", "bound variable")
            if prim is not None and name not in prim.inputs:
                err("bad-binding", f"{sim.primitive} has no input {name!r}", "simulation")
        if prim is not None:
            unbound = sorted(set(prim.inputs) - set(sim.bindings.values()) - set(prim.defaults))
            if unbound:
                err("unbound-input", f"{sim.primitive} inputs {unbound} are not bound to variables", "simulation")
        for ctl in sim.controls:
            if ctl.variable not in sim.bindings:
                err("unbound-control", f"control {ctl.variable!r} is not bound to a primitive input", "simulation")
        for o in sim.outputs:
            if prim is not None and o.key not in prim.outputs:
                err("bad-output", f"{sim.primitive} does not compute {o.key!r}", "simulation")
            if o.concept:
                need(o.concept, C, "simulation", "output concept")
        for sc in sim.scenarios:
            for vid in sc.values:
                need(vid, VAR, f"scenario:{sc.id}", "scenario variable")
        for c in sim.claims:
            if need(c, CL, "simulation", "simulation claim"):
                referenced_claims.add(c)

    # ── hints may only point at elements ──
    for h in spec.render_hints:
        for t in h.targets:
            if t not in kinds:
                err("unknown-ref", f"render hint target {t!r} does not exist", "render_hints")

    for c in spec.claims:
        if c.id not in referenced_claims and c.importance != "detail":
            warn("unplaced-claim", f"claim {c.id} is not used by the summary, an objective or any structure", c.id)
    return ValidationResult(issues)
