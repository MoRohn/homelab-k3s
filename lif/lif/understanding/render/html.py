"""Self-contained HTML renderers (spec §31–§33, §66–§67, §83, §86–§89).

  static-explainer   STATIC_VISUAL_EXPLAINER  no script: headline, observed vs inferred, causal flow,
                                               process, comparison, uncertainty, sources
  step-through       INTERACTIVE_HTML         one step of a chain or process at a time (keyboard and
                                               buttons); the mobile alternative to a wide diagram
  simulation         SIMULATION               controls bound to SimulationSpec variables; outcomes are
                                               precomputed by lif.understanding.simulate and looked up

Sandboxing: every page carries a CSP meta that forbids network access (`default-src 'none'`,
`connect-src 'none'`), loads nothing external and needs no Labzilla credentials. Serve them from an
isolated origin or a sandboxed iframe (`sandbox="allow-scripts"`, no allow-same-origin), never inline
in the console origin. Pages follow prefers-color-scheme and prefers-reduced-motion.
"""
from __future__ import annotations

import json

from lif.understanding import simulate
from lif.understanding.render.base import RendererCapabilities, RenderRequest, RenderResult, Segment, SemanticGap
from lif.understanding.render.common import claims_in_order, esc, headline, uncertainty_segments
from lif.understanding.render.prose import _mode, claim_text
from lif.understanding.spec import ExplanationSpec

CSP_STATIC = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
CSP_SCRIPT = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; "
              "connect-src 'none'; base-uri 'none'; form-action 'none'")

CSS = """
:root{--bg:#07110f;--surface:#0b1715;--border:#1d302c;--text:#e7f0ed;--muted:#9fb3ad;--brand:#00d878;
--brand-text:#3fe397;--warn:#f2c14e;--info:#6cb6ff;color-scheme:dark}
@media (prefers-color-scheme: light){:root{--bg:#f4f8f6;--surface:#fff;--border:#d4e0db;--text:#10201c;
--muted:#4d605a;--brand:#007443;--brand-text:#00703f;--warn:#8a5a00;--info:#1d64b3;color-scheme:light}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.45rem;line-height:1.3;margin:0 0 12px}h2{font-size:1.05rem;margin:28px 0 8px;color:var(--brand-text)}
p{margin:6px 0}.lead{font-size:1.12rem}.muted{color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin:10px 0}
.unc{border-left:4px solid var(--warn)}.unc b{color:var(--warn)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:12px}
.tag{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.flow{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:8px 0}
.node{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 10px}
.arrow{color:var(--brand);font-weight:700}ol,ul{padding-left:1.3rem}
table{border-collapse:collapse;width:100%;font-size:.95rem}th,td{border:1px solid var(--border);padding:6px 8px;
text-align:left;vertical-align:top}code{font-size:.85em;word-break:break-all}
button,select,input{font:inherit}button{background:var(--surface);color:var(--text);border:1px solid var(--border);
border-radius:8px;padding:8px 14px;cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{
outline:3px solid var(--brand);outline-offset:2px}
.out{font-size:1.6rem;font-weight:650;color:var(--brand-text)}label{display:block;margin:10px 0 2px}
input[type=range]{width:100%;accent-color:var(--brand)}.grid{display:grid;grid-template-columns:repeat(auto-fit,
minmax(160px,1fr));gap:10px}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important}}
"""


class Page:
    """Builds HTML while recording which spec IDs each piece of text comes from."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.segs: list[Segment] = []

    def raw(self, html: str) -> None:
        self.parts.append(html)

    def text(self, tag: str, text: str, refs: list[str], cls: str = "", role: str = "semantic") -> None:
        self.segs.append(Segment(text, refs, role))
        attrs = f' class="{cls}"' if cls else ""
        attrs += f' data-ids="{esc(" ".join(refs))}"' if refs else ""
        self.parts.append(f"<{tag}{attrs}>{esc(text)}</{tag}>")

    def heading(self, text: str, level: int = 2) -> None:
        self.segs.append(Segment(text, [], "structural"))
        self.parts.append(f"<h{level}>{esc(text)}</h{level}>")

    def doc(self, title: str, csp: str, script: str = "") -> str:
        body = "".join(self.parts)
        js = f"<script>{script}</script>" if script else ""
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
                f'<meta name="referrer" content="no-referrer"><title>{esc(title)}</title><style>{CSS}</style></head>'
                f'<body><main>{body}</main>{js}</body></html>')


def _unc_block(p: Page, spec: ExplanationSpec, claim_ids: list[str]) -> None:
    for s in uncertainty_segments(spec, claim_ids):
        p.raw('<div class="card unc"><b>Uncertain.</b> ')
        p.text("span", s.text, s.refs)
        p.raw("</div>")


def _title(req: RenderRequest) -> str:
    return headline(req).text


class StaticExplainer:
    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="static-explainer", version="1.0.0", target="STATIC_VISUAL_EXPLAINER",
            best_for=("causal", "comparison", "process", "narrative", "dependency"),
            outputs=("text/html",), latency_class="fast", resource_class="CPU", clarity=0.75, consume_seconds=120,
            permissions=(), verification=("contract", "csp", "no-network"))

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return None

    def render(self, req: RenderRequest) -> RenderResult:
        spec, mode = req.spec, _mode(req)
        p = Page()
        head = headline(req)
        p.text("h1", claim_text(spec, head, mode), [head.id])
        _unc_block(p, spec, [head.id])
        claims = [c for c in claims_in_order(req) if c.id != head.id]
        obs = [c for c in claims if c.kind == "observed"]
        inf = [c for c in claims if c.kind == "inferred"]
        other = [c for c in claims if c.kind not in ("observed", "inferred")]
        if obs or inf:
            p.raw('<div class="cols">')
            for label, group in (("Observed", obs), ("Inferred", inf)):
                if group:
                    p.raw(f'<section class="card"><div class="tag">{label}</div><ul>')
                    for c in group:
                        p.text("li", claim_text(spec, c, mode), [c.id, *c.evidence])
                    p.raw("</ul></section>")
            p.raw("</div>")
        for c in other:
            p.text("p", claim_text(spec, c, mode), [c.id])
        for ch in spec.causal_chains:
            if req.visible(ch):
                p.heading(ch.label)
                p.raw('<div class="flow" role="list">')
                for i, sid in enumerate(ch.steps):
                    if i:
                        p.raw('<span class="arrow" aria-hidden="true">→</span>')
                    p.text("span", spec.concept(sid).label, [sid], "node", "label")
                p.raw("</div>")
        for pr in spec.processes:
            if req.visible(pr) and req.level >= 2:
                p.heading(pr.name)
                p.raw("<ol>")
                for sid in pr.steps:
                    p.text("li", spec.concept(sid).label, [sid, pr.id], role="label")
                p.raw("</ol>")
        for cmp in spec.comparisons:
            if req.visible(cmp):
                p.heading(cmp.label)
                cells = {(c.item, c.dimension): c for c in cmp.cells}
                p.raw("<table><thead><tr><th></th>")
                for d in cmp.dimensions:
                    p.text("th", d.label, [cmp.id], role="label")
                p.raw("</tr></thead><tbody>")
                for it in cmp.items:
                    p.raw("<tr>")
                    p.text("th", spec.concept(it).label, [it], role="label")
                    for d in cmp.dimensions:
                        c = cells.get((it, d.id))
                        p.text("td", c.value if c else "", [it, cmp.id] + ([c.claim] if c and c.claim else []))
                    p.raw("</tr>")
                p.raw("</tbody></table>")
        if req.level >= 2:
            for ex in spec.examples:
                if req.visible(ex):
                    p.text("p", ("Counterexample: " if ex.counter else "Example: ") + ex.text, [ex.id], "card")
        _unc_block(p, spec, [c.id for c in claims])
        if spec.source_refs:
            p.heading("Sources")
            p.raw("<ul>")
            for s in spec.source_refs:
                p.text("li", f"{s.title or s.kind}: {s.ref}", [s.id])
            p.raw("</ul>")
        caps = self.capabilities()
        return RenderResult(caps.name, caps.version, caps.target, p.doc(_title(req), CSP_STATIC), "text/html", p.segs,
                            emphasis=[head.id], verification=verify_html(p.doc(_title(req), CSP_STATIC), False))


STEP_JS = """
const s=[...document.querySelectorAll('.step')],n=document.getElementById('n'),b=document.getElementById('prev'),
f=document.getElementById('next');let i=0;function show(k){i=Math.max(0,Math.min(s.length-1,k));
s.forEach((e,j)=>{e.hidden=j!==i});n.textContent=(i+1)+' / '+s.length;b.disabled=i===0;f.disabled=i===s.length-1;}
b.onclick=()=>show(i-1);f.onclick=()=>show(i+1);
document.addEventListener('keydown',e=>{if(e.key==='ArrowRight')show(i+1);if(e.key==='ArrowLeft')show(i-1);});show(0);
"""


class StepThrough:
    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="step-through", version="1.0.0", target="INTERACTIVE_HTML", best_for=("process", "causal"),
            unsupported=("comparison",), outputs=("text/html",), interactive=True, latency_class="fast",
            resource_class="BROWSER", clarity=0.6, consume_seconds=90, permissions=("browser",),
            verification=("contract", "csp", "no-network"))

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return None if spec.causal_chains or spec.processes else "the spec has no chain or process to step through"

    def render(self, req: RenderRequest) -> RenderResult:
        spec, mode = req.spec, _mode(req)
        seq = next(([*c.steps], c.id, c.label) for c in spec.causal_chains if req.visible(c)) \
            if any(req.visible(c) for c in spec.causal_chains) else \
            next(([*x.steps], x.id, x.name) for x in spec.processes if req.visible(x)) \
            if any(req.visible(x) for x in spec.processes) else None
        if seq is None:
            raise SemanticGap("no chain or process is visible at this depth")
        steps, src, label = seq
        p = Page()
        head = headline(req)
        p.text("h1", claim_text(spec, head, mode), [head.id])
        _unc_block(p, spec, [head.id])
        p.text("h2", label, [src], role="label")
        for k, sid in enumerate(steps):
            con = spec.concept(sid)
            p.raw(f'<section class="card step" aria-live="polite" {"hidden" if k else ""}>')
            p.text("h3", con.label, [sid], role="label")
            if con.definition:
                p.text("p", con.definition, [sid])
            for c in claims_in_order(req):
                if sid in c.concepts and c.id != head.id:
                    p.text("p", claim_text(spec, c, mode), [c.id, *c.evidence], "muted")
                    _unc_block(p, spec, [c.id])
            p.raw("</section>")
        p.raw('<p><button id="prev" type="button">Previous</button> <span id="n" class="muted"></span> '
              '<button id="next" type="button">Next</button></p>')
        doc = p.doc(_title(req), CSP_SCRIPT, STEP_JS)
        caps = self.capabilities()
        return RenderResult(caps.name, caps.version, caps.target, doc, "text/html", p.segs, emphasis=[head.id],
                            edges=list(zip(steps, steps[1:])), verification=verify_html(doc, True))


SIM_JS = """
const D=JSON.parse(document.getElementById('grid').textContent);
const ins=D.controls.map(id=>document.getElementById('c-'+id));
const idx=el=>el.dataset.kind==='enum'?el.selectedIndex:+el.value;
function update(){const r=D.results[ins.map(idx).join('|')];
ins.forEach((el,k)=>{const o=document.getElementById('v-'+D.controls[k]);if(o)o.textContent=String(D.axes[k][idx(el)])});
document.querySelectorAll('[data-out]').forEach(e=>{e.textContent=r?String(r[e.dataset.out]):'–'});}
ins.forEach(el=>el.addEventListener('input',update));
document.querySelectorAll('[data-scenario]').forEach(b=>b.addEventListener('click',()=>{
const v=JSON.parse(b.dataset.scenario);ins.forEach((el,k)=>{const id=D.controls[k];if(!(id in v))return;
const j=D.axes[k].findIndex(x=>Number(x)===Number(v[id])||String(x)===String(v[id]));if(j<0)return;
if(el.dataset.kind==='enum'){el.selectedIndex=j}else{el.value=j}});update();}));update();
"""


class SimulationApp:
    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="simulation", version="1.0.0", target="SIMULATION", best_for=("parameter", "quantitative"),
            unsupported=("definition",), outputs=("text/html",), interactive=True, latency_class="seconds",
            resource_class="BROWSER", clarity=0.8, consume_seconds=150, permissions=("browser",),
            verification=("contract", "csp", "no-network", "grid"))

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return None if spec.simulation is not None else "the spec has no SimulationSpec"

    def render(self, req: RenderRequest) -> RenderResult:
        spec, mode = req.spec, _mode(req)
        sim = spec.simulation
        if sim is None:
            raise SemanticGap("no SimulationSpec: an interactive model needs deterministic rules in the IR")
        g = simulate.grid(spec)
        p = Page()
        head = headline(req)
        p.text("h1", claim_text(spec, head, mode), [head.id])
        _unc_block(p, spec, [head.id])
        for cid in sim.claims:
            if cid != head.id and (c := spec.claim(cid)) is not None and req.visible(c):
                p.text("p", claim_text(spec, c, mode), [c.id], "muted")
                _unc_block(p, spec, [c.id])
        p.raw('<div class="cols"><section class="card">')
        p.heading("Controls", 2)
        defaults = {v.id: v.default for v in spec.variables}
        for k, ctl in enumerate(sim.controls):
            v = spec.get(ctl.variable)
            axis = g["axes"][k]
            p.raw(f'<label for="c-{esc(v.id)}">')
            p.text("span", f"{v.label}" + (f" ({v.unit})" if v.unit else ""), [v.id], role="label")
            p.raw(f': <output id="v-{esc(v.id)}"></output></label>')
            if v.kind == "enum":
                opts = "".join(f'<option{" selected" if o == defaults[v.id] else ""}>{esc(o)}</option>' for o in axis)
                p.raw(f'<select id="c-{esc(v.id)}" data-kind="enum">{opts}</select>')
            else:
                d = defaults[v.id]
                start = min(range(len(axis)), key=lambda j: abs(float(axis[j]) - float(d))) if d is not None else 0
                p.raw(f'<input type="range" id="c-{esc(v.id)}" min="0" max="{len(axis) - 1}" step="1" '
                      f'value="{start}" aria-describedby="v-{esc(v.id)}">')
        if sim.scenarios:
            p.raw('<p class="muted">Scenarios</p><p>')
            for sc in sim.scenarios:
                p.raw(f"<button type=\"button\" data-scenario='{esc(json.dumps(sc.values))}'>")
                p.text("span", sc.label, [*sc.values], role="label")
                p.raw("</button> ")
            p.raw("</p>")
        p.raw('</section><section class="card" aria-live="polite">')
        p.heading("Result", 2)
        p.raw('<div class="grid">')
        for o in sim.outputs:
            p.raw("<div>")
            p.text("div", o.label + (f" ({o.unit})" if o.unit else ""), [o.concept] if o.concept else [],
                   "tag", "label" if o.concept else "structural")
            p.raw(f'<div class="out" data-out="{esc(o.key)}">–</div></div>')
        p.raw("</div>")
        p.raw("</section></div>")
        p.raw('<p class="muted">Every number is computed by the deterministic model '
              f'<code>{esc(sim.primitive)}</code>; nothing here is estimated by a language model.</p>')
        p.segs.append(Segment("deterministic model note", [], "structural"))
        p.raw(f'<script type="application/json" id="grid">{json.dumps(g).replace("</", "<\\/")}</script>')
        doc = p.doc(_title(req), CSP_SCRIPT, SIM_JS)
        caps = self.capabilities()
        ver = verify_html(doc, True)
        ver["grid"] = {"points": len(g["results"]), "primitive": sim.primitive}
        return RenderResult(caps.name, caps.version, caps.target, doc, "text/html", p.segs, emphasis=[head.id],
                            verification=ver)


def verify_html(doc: str, scripts: bool) -> dict:
    """Static checks: the CSP meta is present and forbids network; nothing external is referenced."""
    import re
    ext = re.findall(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", doc, re.I)
    fetches = re.findall(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\()", doc)
    ok = ("Content-Security-Policy" in doc and "default-src 'none'" in doc and not ext and not fetches
          and (scripts or "<script" not in doc))
    return {"csp": {"ok": ok, "external_refs": len(ext), "network_apis": sorted(set(fetches)), "scripts": scripts}}
