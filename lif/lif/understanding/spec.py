"""ExplanationSpec/v1: the canonical explanation IR (spec §13–§28, §66, §115).

Every element that carries meaning has a stable `id`, unique across the whole spec, so renderers,
critics, highlight-to-ask and semantic diffs can all point at the same thing. Presentation guidance
lives in `level` (the shallowest depth an element appears at), `importance` and `render_hints`; the
hints are enumerations, never free text, so no renderer-specific semantic content can hide in them.

Depth levels (§25–§26): 0 glance · 1 summary · 2 learn · 3 deep. A renderer at depth d shows the
elements with level ≤ d.

`migrate()` lifts older documents to the current schema; v1 is the first, so it only checks the tag.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lif.understanding import SCHEMA_VERSION

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")

Expertise = Literal["novice", "intermediate", "expert"]
Role = Literal["executive", "operator", "developer", "software_engineer", "researcher", "student",
               "domain_expert", "general"]
Depth = Literal["glance", "summary", "learn", "deep"]
DEPTH_LEVEL: dict[str, int] = {"glance": 0, "summary": 1, "learn": 2, "deep": 3}
DEPTH_SECONDS: dict[str, int] = {"glance": 15, "summary": 45, "learn": 180, "deep": 600}   # §26
Importance = Literal["primary", "supporting", "detail"]
ClaimKind = Literal["observed", "inferred", "definition", "assumption", "general"]
RelType = Literal["depends_on", "causes", "blocks", "contains", "routes_to", "competes_with", "precedes",
                  "follows", "consumes", "produces", "supports", "contradicts", "limits", "maps_to",
                  "scheduled_on", "is_a", "part_of", "uses"]
SourceKind = Literal["knowledge", "metric", "log", "k8s", "document", "url", "state", "package"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Elem(_M):
    id: str
    level: int = Field(2, ge=0, le=3)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"id {v!r} must match {ID_RE.pattern}")
        return v


class Audience(_M):
    role: Role = "general"
    expertise: Expertise = "intermediate"
    math_fluency: Literal["low", "medium", "high"] = "medium"
    known_terms: list[str] = []              # canonical terms the reader already knows
    decision_context: str = ""               # "deciding whether to add a GPU node"


class Term(_M):
    canonical: str
    abbreviation: str = ""
    synonyms: list[str] = []
    plain: str = ""                          # wording for novices ("the part that picks a node")


class SourceRef(_Elem):
    """A pointer to canonical evidence elsewhere; the spec never copies the evidence itself (§21)."""
    kind: SourceKind
    ref: str                                 # knowledge key `repo::slug`, PromQL, `ns/pod`, URL, state path
    title: str = ""
    observed_at: float | None = None


class Concept(_Elem):
    label: str
    definition: str = ""
    term: str = ""                           # canonical term in `terms`, if any
    kind: Literal["entity", "state", "event", "quantity", "process", "idea"] = "entity"
    source_refs: list[str] = []


class Evidence(_Elem):
    text: str
    source: str                              # SourceRef id
    value: float | None = None
    unit: str = ""


class Claim(_Elem):
    text: str
    kind: ClaimKind = "general"
    concepts: list[str] = []
    evidence: list[str] = []
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    importance: Importance = "supporting"


class Relationship(_Elem):
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    type: RelType
    label: str = ""                          # verb phrase shown on an edge; defaults to the type
    claim: str = ""                          # the claim this edge asserts, when it asserts one
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CausalChain(_Elem):
    label: str
    steps: list[str]                         # concept ids, cause first
    claim: str = ""


class Process(_Elem):
    name: str
    steps: list[str]                         # concept ids, in order


class TimelineEvent(_Elem):
    t: float
    label: str
    concept: str = ""
    claim: str = ""


class Timeline(_Elem):
    label: str
    unit: str = "s"
    events: list[TimelineEvent]


class HierarchyEdge(_M):
    parent: str
    child: str


class Hierarchy(_Elem):
    label: str
    root: str
    edges: list[HierarchyEdge]


class Cell(_M):
    item: str                                # concept id
    dimension: str                           # Dimension id
    value: str
    claim: str = ""


class Dimension(_M):
    id: str
    label: str


class Comparison(_Elem):
    label: str
    items: list[str]
    dimensions: list[Dimension]
    cells: list[Cell]


class Variable(_Elem):
    symbol: str
    label: str
    unit: str = ""
    kind: Literal["int", "float", "enum"] = "float"
    min: float | None = None
    max: float | None = None
    step: float | None = None
    default: float | str | None = None
    options: list[str] = []
    concept: str = ""


class Equation(_Elem):
    expr: str                                # "slots = floor((M - R) / m)"
    meaning: str = ""                        # claim id the equation states
    variables: list[str] = []                # Variable ids


class Example(_Elem):
    text: str
    illustrates: list[str] = []              # claim or concept ids
    counter: bool = False                    # a counterexample (§64)


class Analogy(_Elem):
    text: str
    maps: dict[str, str] = {}                # concept id → what it corresponds to in the analogy
    breaks_down: str = ""


class Constraint(_Elem):
    text: str
    concepts: list[str] = []


class Uncertainty(_Elem):
    about: str                               # claim id
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    evidence_needed: list[str] = []


class Misconception(_Elem):
    text: str
    correction: str                          # claim id


class Objective(_Elem):
    text: str
    claims: list[str] = []


class Checkpoint(_Elem):
    question: str
    answer_claims: list[str]
    objective: str = ""


class Summary(_M):
    headline: str                            # claim id: the one-sentence answer
    claims: list[str] = []                   # the short version, in order


class RenderHint(_M):
    kind: Literal["emphasize", "prefer", "avoid", "group", "order"]
    targets: list[str] = []                  # element ids
    value: Literal["primary", "secondary", "diagram", "table", "timeline", "simulation", "prose", "animation",
                   "left_to_right", "top_down", ""] = ""


class SimControl(_M):
    variable: str                            # Variable id
    widget: Literal["slider", "toggle", "select", "number"] = "slider"


class SimOutput(_M):
    key: str                                 # an output the primitive computes
    label: str
    unit: str = ""
    concept: str = ""


class Scenario(_M):
    id: str
    label: str
    values: dict[str, float | str]           # Variable id → value


class SimulationSpec(_M):
    """A deterministic model (§66): `primitive` names code in understanding/simulate.py, so outcomes are
    computed, never invented by a model."""
    primitive: str                           # "gpu-placement/v1"
    variables: list[str]                     # Variable ids bound to the primitive's inputs
    bindings: dict[str, str]                 # Variable id → primitive input name
    controls: list[SimControl]
    outputs: list[SimOutput]
    scenarios: list[Scenario] = []
    claims: list[str] = []                   # claims the simulation demonstrates


class Metadata(_M):
    created_at: float = Field(default_factory=time.time)
    builder: str = ""                        # "package:kubernetes/what-is-kubernetes", "state:gpu", "llm:local/default"
    model: str = ""
    data_class: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"] = "CONFIDENTIAL"
    parent: str = ""                         # spec id this one extends (deepen)
    knowledge_snapshot: str = ""
    source_snapshot: str = ""
    package: str = ""


class ExplanationSpec(_M):
    schema_version: Literal["ExplanationSpec/v1"] = SCHEMA_VERSION
    id: str
    version: int = 1
    question: str
    audience: Audience = Audience()
    terms: list[Term] = []
    learning_objectives: list[Objective] = []
    summary: Summary
    concepts: list[Concept] = []
    claims: list[Claim] = []
    evidence: list[Evidence] = []
    relationships: list[Relationship] = []
    causal_chains: list[CausalChain] = []
    processes: list[Process] = []
    timelines: list[Timeline] = []
    hierarchies: list[Hierarchy] = []
    comparisons: list[Comparison] = []
    variables: list[Variable] = []
    equations: list[Equation] = []
    examples: list[Example] = []
    analogies: list[Analogy] = []
    constraints: list[Constraint] = []
    uncertainties: list[Uncertainty] = []
    misconceptions: list[Misconception] = []
    checkpoints: list[Checkpoint] = []
    source_refs: list[SourceRef] = []
    simulation: SimulationSpec | None = None
    render_hints: list[RenderHint] = []
    metadata: Metadata = Metadata()

    # ── lookups ───────────────────────────────────────────────────────────────────────────

    def index(self) -> dict[str, tuple[str, Any]]:
        """id → (kind, element). Duplicates keep the first; validate() reports them."""
        out: dict[str, tuple[str, Any]] = {}
        for kind, el in self.elements():
            out.setdefault(el.id, (kind, el))
        return out

    def elements(self):
        for kind in ("source_refs", "concepts", "evidence", "claims", "relationships", "causal_chains", "processes",
                     "timelines", "hierarchies", "comparisons", "variables", "equations", "examples", "analogies",
                     "constraints", "uncertainties", "misconceptions", "learning_objectives", "checkpoints"):
            for el in getattr(self, kind):
                yield kind, el
                if kind == "timelines":
                    for ev in el.events:
                        yield "timeline_events", ev

    def get(self, id_: str) -> Any:
        hit = self.index().get(id_)
        return hit[1] if hit else None

    def claim(self, id_: str) -> Claim | None:
        return next((c for c in self.claims if c.id == id_), None)

    def concept(self, id_: str) -> Concept | None:
        return next((c for c in self.concepts if c.id == id_), None)

    def uncertainty_for(self, claim_id: str) -> list[Uncertainty]:
        return [u for u in self.uncertainties if u.about == claim_id]

    def term_for(self, text: str) -> Term | None:
        low = text.lower()
        return next((t for t in self.terms if low in {t.canonical.lower(), t.abbreviation.lower(),
                                                      *(s.lower() for s in t.synonyms)}), None)

    # ── identity ──────────────────────────────────────────────────────────────────────────

    def semantic_dict(self) -> dict:
        """The meaning only: no timestamps, builder names or render hints. Two specs with equal semantic
        dicts say the same thing, whoever built them and whenever."""
        d = self.model_dump(mode="json", by_alias=True)
        d.pop("metadata", None)
        d.pop("render_hints", None)
        d.pop("id", None)
        d.pop("version", None)
        return d

    def semantic_hash(self) -> str:
        blob = json.dumps(self.semantic_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:20]

    def to_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


def migrate(doc: dict) -> dict:
    """Bring a stored document to the current schema. v1 is the first version; future versions add a
    step here (v1 → v2 …) and keep old documents loadable (§115)."""
    v = doc.get("schema_version", SCHEMA_VERSION)
    if v != SCHEMA_VERSION:
        raise ValueError(f"unknown schema_version {v!r}; this build reads {SCHEMA_VERSION}")
    return doc


def load(doc: dict) -> ExplanationSpec:
    return ExplanationSpec.model_validate(migrate(dict(doc)))
