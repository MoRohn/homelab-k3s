"""Persistent knowledge layer — acceptance tests A–P (docs/KNOWLEDGE.md) and the full learning loop.

Each test builds its own workspace in tmp_path over a COPY of the real package registry, so the
tracked registry and the dogfood repo are never modified. No network: Jev is never called (the
knowledge decisions are INTERNAL/CONFIDENTIAL) and embeddings are disabled unless a test enables them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from lif.knowledge.compiler import build
from lif.knowledge.ops import Knowledge
from lif.knowledge.writer import WriteError

LIF = Path(__file__).resolve().parents[1]
REGISTRY = LIF / "knowledge" / "registry"


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip())
    return p


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.delenv("LIF_KNOWLEDGE_EMBED_URL", raising=False)
    monkeypatch.delenv("LIF_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LIF_KNOWLEDGE_INDEX", raising=False)
    root = tmp_path / "ws"
    shutil.copytree(REGISTRY, root / "registry")
    write(root / "workspace.yaml", """
        name: test
        events_repo: ops
        repos:
          - path: repos/proj
          - path: repos/ops
            optional: true
        permissions:
          default: {scopes: [read:knowledge]}
          writer-bot: {scopes: [read:knowledge, write:knowledge, execute:skills]}
          ops-bot: {scopes: [read:knowledge, write:knowledge], write_repos: [ops]}
    """)
    p = root / "repos" / "proj"
    write(p / "repo.yaml", """
        name: proj
        version: 0.1.0
        kind: project
        project: proj
        dependencies:
          - {name: knowledge-governance, version: ^1.0}
          - {name: agent-workflows, version: ^1.0}
    """)
    write(p / "knowledge" / "proj.md", """
        ---
        type: project
        summary: Test project for the knowledge layer
        ---
    """)
    write(p / "knowledge" / "dgx-baseline.md", """
        ---
        type: source
        kind: measurement
        location: baseline.txt
        quality: primary
        ---
        # DGX resource baseline

        The node has one GPU with unified memory. ^p1

        Admissible GPU memory for secondary work is about one GiB. ^p2
    """)
    write(p / "knowledge" / "k3s-eval.md", """
        ---
        type: source
        kind: documentation
        url: https://docs.k3s.io
        ---
        K3s is a conformant Kubernetes distribution in a single binary. ^p1
    """)
    write(p / "knowledge" / "single-node-primary.md", """
        ---
        type: assumption
        statement: One node is the whole platform.
        status: active
        confidence: high
        supported_by: ["[[dgx-baseline]]"]
        ---
    """)
    write(p / "knowledge" / "k3s-is-conformant.md", """
        ---
        type: claim
        statement: K3s is conformant Kubernetes.
        status: supported
        grounds:
          - id: g1
            source: "[[k3s-eval]]"
            passage: "[[k3s-eval^p1]]"
            stance: supports
            basis: reported
        ---
        See [[^g1]].
    """)
    return root


def open_kn(root: Path) -> Knowledge:
    return Knowledge.open(root, index=":memory:")


def decision_fields(**over):
    f = {"title": "Use K3s on the single node", "status": "accepted", "date": "2026-10-01",
         "question": "Which orchestrator?", "selected": "k3s", "alternatives": ["kubeadm", "compose"],
         "evidence": ["[[dgx-baseline]]", "[[k3s-eval]]"], "assumptions": ["[[single-node-primary]]"]}
    f.update(over)
    return f


# ── A–C: typed objects and diagnostics ────────────────────────────────────────

def test_A_typed_decision_linked_to_evidence_and_assumptions(ws):
    kn = open_kn(ws)
    res = kn.writer("human").create("decision", decision_fields(), "Because one node.", id="use-k3s", repo="proj")
    assert not [d for d in res["diagnostics"] if d["severity"] == "error"]
    o = kn.graph.get("use-k3s")
    assert o["type"] == "knowledge-governance::decision"
    rels = {(l["field"], l["target"]) for l in o["links_out"]}
    assert ("evidence", "proj::dgx-baseline") in rels and ("assumptions", "proj::single-node-primary") in rels
    assert (ws / "repos/proj/knowledge/decisions/use-k3s.md").read_text().startswith("---\ntype: decision")


def test_B_missing_required_field_is_a_diagnostic(ws):
    write(ws / "repos/proj/knowledge/bad-decision.md", """
        ---
        type: decision
        status: accepted
        evidence: ["[[dgx-baseline]]"]
        ---
    """)
    _, store, _ = build(ws, ":memory:")
    d = [x for x in store.diagnostics("error") if x["key"] == "proj::bad-decision"]
    assert any(x["code"] == "K002" and x["detail"]["field"] == "date" for x in d)


def test_B_writer_rejects_a_write_that_adds_errors(ws):
    kn = open_kn(ws)
    with pytest.raises(WriteError) as e:
        kn.writer("human").create("decision", {"status": "accepted"}, id="no-date", repo="proj")
    assert "missing required field" in str(e.value)
    assert not (ws / "repos/proj/knowledge/decisions/no-date.md").exists()


def test_C_wrong_reference_type_is_a_diagnostic(ws):
    write(ws / "repos/proj/knowledge/some-task.md", "---\ntype: task\nstatus: open\n---\n")
    write(ws / "repos/proj/knowledge/bad-ref.md", """
        ---
        type: decision
        status: accepted
        date: 2026-10-01
        evidence: ["[[some-task]]"]
        ---
    """)
    _, store, _ = build(ws, ":memory:")
    d = next(x for x in store.diagnostics("error") if x["code"] == "K006")
    assert "INVALID REFERENCE" in d["message"]
    assert d["detail"]["expected"] == "evidence[]".rstrip("[]") or d["detail"]["expected"] == "evidence"
    assert d["detail"]["received"] == "task"


def test_C_enum_unknown_type_duplicate_cycle(ws):
    k = ws / "repos/proj/knowledge"
    write(k / "e1.md", "---\ntype: decision\nstatus: maybe\ndate: 2026-10-01\nevidence: ['[[dgx-baseline]]']\n---\n")
    write(k / "u1.md", "---\ntype: gizmo\n---\n")
    write(k / "sub/single-node-primary.md", "---\ntype: document\n---\n")
    write(k / "d1.md", "---\ntype: decision\nstatus: accepted\ndate: 2026-10-01\nevidence: ['[[dgx-baseline]]']\n"
                       "supersedes: '[[d2]]'\n---\n")
    write(k / "d2.md", "---\ntype: decision\nstatus: accepted\ndate: 2026-10-01\nevidence: ['[[dgx-baseline]]']\n"
                       "supersedes: '[[d1]]'\n---\n")
    _, store, _ = build(ws, ":memory:")
    codes = {d["code"] for d in store.diagnostics("error")}
    assert {"K003", "K001", "K007", "K010"} <= codes


# ── D / P: sessions resume from the graph, not a transcript ──────────────────

def test_D_later_session_reconstructs_why(ws):
    kn = open_kn(ws)
    kn.writer("agent:a1").create("decision", decision_fields(), "Rationale in prose.", id="use-k3s", repo="proj")
    kn.context.checkpoint("proj", "agent:a1", "Decided on K3s.", decisions=["use-k3s"], next=["deploy"])
    del kn
    later = open_kn(ws)                               # a different process would see exactly this
    w = later.graph.why("use-k3s")
    assert {e["key"] for e in w["evidence"]} == {"proj::dgx-baseline", "proj::k3s-eval"}
    assert w["assumptions"][0]["key"] == "proj::single-node-primary"
    assert w["alternatives"] == ["kubeadm", "compose"]
    assert w["provenance"]["updated_by"] == "agent:a1"
    r = later.context.resume("proj")
    assert r["last_checkpoint"]["next"] == ["deploy"]
    assert any(d["key"] == "proj::use-k3s" for d in r["important_decisions"])


def test_P_fresh_process_resumes_without_transcript(ws, tmp_path):
    kn = open_kn(ws)
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")
    kn.writer("human").create("task", {"title": "Deploy K3s", "status": "open", "implements": "[[use-k3s]]"},
                              id="deploy-k3s", repo="proj")
    kn.writer("human").create("question", {"title": "Do we need HA?", "status": "open"}, id="ha", repo="proj")
    kn.context.checkpoint("proj", "agent:a1", "Planned the cluster.", completed=["use-k3s"], next=["deploy-k3s"])
    env = {**os.environ, "PYTHONPATH": str(LIF), "LIF_KNOWLEDGE_INDEX": str(tmp_path / "fresh.db")}
    out = subprocess.run([sys.executable, "-m", "lif.knowledge.cli", "--root", str(ws), "--json", "session", "resume",
                          "proj"], capture_output=True, text=True, env=env, check=True)
    r = json.loads(out.stdout)
    assert r["last_checkpoint"]["summary"] == "Planned the cluster."
    assert [t["key"] for t in r["open_tasks"]] == ["proj::deploy-k3s"]
    assert [q["key"] for q in r["open_questions"]] == ["proj::ha"]
    assert r["important_decisions"][0]["because"]


# ── E: assumption changes → dependent decisions ──────────────────────────────

def test_E_changing_an_assumption_finds_dependent_decisions(ws):
    kn = open_kn(ws)
    w = kn.writer("human")
    w.create("decision", decision_fields(), id="use-k3s", repo="proj")
    w.create("decision", decision_fields(title="Single-replica storage", assumptions=[]),
             id="single-replica-storage", repo="proj")
    w.create("task", {"title": "Build cluster", "status": "open", "implements": "[[use-k3s]]"}, id="build", repo="proj")
    assert not kn.store.reconsiderations()
    dependents = kn.graph.find(type="decision", depends_on="single-node-primary")
    assert [d["key"] for d in dependents] == ["proj::use-k3s"]
    w.update("single-node-primary", set={"status": "invalidated"})
    queue = {r["key"]: r for r in kn.store.reconsiderations()}
    assert "proj::use-k3s" in queue and "proj::build" in queue
    assert "proj::single-replica-storage" not in queue
    assert queue["proj::use-k3s"]["priority"] == "high"
    assert kn.store.get("proj::use-k3s")["status"] == "accepted"          # never auto-reversed
    # a review closes it
    kn.writer("human").create("review", {"reviews": ["[[use-k3s]]"], "trigger": "[[single-node-primary]]",
                                         "outcome": "reaffirmed", "date": "2026-10-02"}, repo="proj")
    assert "proj::use-k3s" not in {r["key"] for r in kn.store.reconsiderations()}


# ── F: claim → evidence → source passage ─────────────────────────────────────

def test_F_trace_claim_to_source_passage(ws):
    kn = open_kn(ws)
    t = kn.graph.trace_claim("k3s-is-conformant")
    g = t["groundings"][0]
    assert g["stance"] == "supports" and g["evidence"]["key"] == "proj::k3s-eval"
    assert g["passage"]["text"] == "K3s is a conformant Kubernetes distribution in a single binary."
    assert t["verdict"].startswith("the graph records evidence")


def test_F_contradiction_is_surfaced_not_resolved(ws):
    write(ws / "repos/proj/knowledge/k3s-is-conformant.md", """
        ---
        type: claim
        statement: K3s is conformant Kubernetes.
        status: supported
        grounds:
          - {id: g1, source: "[[k3s-eval]]", stance: supports}
          - {id: g2, source: "[[dgx-baseline]]", stance: contradicts}
        ---
    """)
    _, store, _ = build(ws, ":memory:")
    assert any(d["code"] == "K016" for d in store.diagnostics())


# ── G, H, I: packages ────────────────────────────────────────────────────────

def test_G_install_skill_from_package(ws):
    kn = open_kn(ws)
    from lif.knowledge import packages, skills
    assert "evaluate-candidate" not in {s["name"] for s in skills.list_skills(kn)}
    with pytest.raises(packages.ApprovalRequired):
        packages.install(kn, "proj", "model-evaluation@^1.0")              # agents can't approve
    res = packages.install(kn, "proj", "model-evaluation@^1.0", approved=True)
    assert res["version"] == "1.0.0" and "evaluate-candidate" in res["skills"]
    lock = yaml.safe_load((ws / "repos/proj/knowledge.lock").read_text())
    assert lock["packages"]["model-evaluation"]["skills"]["evaluate-candidate"] == 1
    assert lock["packages"]["model-evaluation"]["hash"].startswith("sha256:")
    kn.writer("human").create("model", {"title": "m1", "profile": "m1"}, id="m1", repo="proj")
    run = skills.run_skill(kn, "evaluate-candidate", {"candidate": "m1"})
    assert run["passed"] is False and run["failed"] == ["has-benchmark"]
    kn.writer("human").create("benchmark", {"title": "m1 bench", "model": "[[m1]]", "suite": "core",
                                            "location": "bench.json"}, id="m1-bench", repo="proj")
    assert skills.run_skill(kn, "evaluate-candidate", {"candidate": "m1"})["passed"] is True
    wrong = skills.run_skill(kn, "evaluate-candidate", {"candidate": "dgx-baseline"})
    assert "expected model" in wrong["input_errors"][0]


def test_H_cross_repo_links_resolve_with_types_and_versions(ws):
    o = ws / "repos/ops"
    write(o / "repo.yaml", """
        name: ops
        kind: project
        project: proj
        dependencies: [{name: proj}, {name: knowledge-governance, version: ^1.0}]
    """)
    write(o / "knowledge/ops-decision.md", """
        ---
        type: decision
        status: accepted
        date: 2026-10-01
        evidence: ["[[proj::dgx-baseline]]", "[[proj::dgx-baseline^p2]]"]
        assumptions: ["[[proj::single-node-primary]]"]
        method: "[[knowledge-governance::semantic-review-method]]"
        ---
    """)
    write(o / "knowledge/bad.md", """
        ---
        type: decision
        status: accepted
        date: 2026-10-01
        evidence: ["[[proj::single-node-primary]]"]
        related: ["[[model-evaluation::model-promotion]]"]
        ---
    """)
    kn = open_kn(ws)
    out = {(e["field"], e["dst"]) for e in kn.store.edges_from("ops::ops-decision")}
    assert ("assumptions", "proj::single-node-primary") in out
    assert ("method", "knowledge-governance::semantic-review-method") in out
    assert kn.store.get("knowledge-governance::semantic-review-method")["repo_version"] == "1.0.0"
    bad = {d["code"] for d in kn.store.diagnostics(key="ops::bad")}
    assert "K006" in bad            # an assumption is not evidence
    assert "K023" in bad            # model-evaluation is not a dependency of ops
    # the original project's decision now has a cross-repo dependent
    assert "ops::ops-decision" in {d["key"] for d in kn.graph.dependents("single-node-primary")}


def _publish_governance_v2(ws: Path) -> None:
    src = ws / "registry/knowledge-governance/1.0.0"
    dst = ws / "registry/knowledge-governance/2.0.0"
    shutil.copytree(src, dst)
    m = yaml.safe_load((dst / "repo.yaml").read_text())
    m["version"] = "2.0.0"
    (dst / "repo.yaml").write_text(yaml.safe_dump(m, sort_keys=False))
    t = yaml.safe_load((dst / "types/decision.type.yaml").read_text())
    t["version"] = 2
    t["fields"]["chosen"] = t["fields"].pop("selected")              # renamed field
    t["fields"]["owner"] = {"type": "string", "required": True}       # newly required
    t["fields"]["status"]["values"].remove("proposed")                # enum value removed
    (dst / "types/decision.type.yaml").write_text(yaml.safe_dump(t, sort_keys=False))
    (dst / "migrations").mkdir()
    (dst / "migrations/decision-1-to-2.migration.yaml").write_text(yaml.safe_dump({
        "type": "decision", "from": 1, "to": 2,
        "ops": [{"rename_field": {"from": "selected", "to": "chosen"}},
                {"set_default": {"field": "owner", "value": "unassigned"}},
                {"map_values": {"field": "status", "map": {"proposed": "accepted"}}}]}, sort_keys=False))


def test_I_package_update_detects_incompatible_schema_changes(ws):
    kn = open_kn(ws)
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")
    from lif.knowledge import packages
    packages.lock(kn.ws, kn.ws.repo("proj"))
    _publish_governance_v2(ws)
    kn = open_kn(ws)
    rep = packages.check_update(kn, "proj", "knowledge-governance")
    assert rep["to"] == "2.0.0" and rep["major"] and rep["breaking"] and not rep["within_constraint"]
    changes = {c["change"] for c in rep["type_changes"]["decision"]}
    assert "removed" in changes and "now required" in {c["change"] for c in rep["type_changes"]["decision"]} | {
        c["change"].split(" (")[0] for c in rep["type_changes"]["decision"]} or any("required" in c for c in changes)
    assert any("proj::use-k3s" == d["key"] for d in rep["would_break"])
    assert rep["migrations"] and not rep["compatible"]
    # nothing changed on disk without approval
    res = packages.update(kn, "proj", "knowledge-governance", migrate=True)
    assert res["applied"] is False and "approval" in res
    assert "selected: k3s" in (ws / "repos/proj/knowledge/decisions/use-k3s.md").read_text()


def test_I_migration_dry_run_then_apply(ws):
    kn = open_kn(ws)
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")
    from lif.knowledge import packages
    packages.lock(kn.ws, kn.ws.repo("proj"))
    _publish_governance_v2(ws)
    kn = open_kn(ws)
    res = packages.update(kn, "proj", "knowledge-governance", migrate=True, approved=True)
    assert res["applied"] and res["migrations_applied"][0]["applied"]
    text = (ws / "repos/proj/knowledge/decisions/use-k3s.md").read_text()
    assert "chosen: k3s" in text and "owner: unassigned" in text
    assert not [d for d in kn.store.diagnostics("error") if d["key"] == "proj::use-k3s"]
    assert yaml.safe_load((ws / "repos/proj/knowledge.lock").read_text())["packages"]["knowledge-governance"][
        "version"] == "2.0.0"


def test_lock_hash_detects_modified_package(ws):
    kn = open_kn(ws)
    from lif.knowledge import packages
    packages.lock(kn.ws, kn.ws.repo("proj"))
    (ws / "registry/knowledge-governance/1.0.0/types/risk.type.yaml").write_text("name: risk\nversion: 9\n")
    kn = open_kn(ws)
    assert any("does not match knowledge.lock" in d["message"] for d in kn.store.diagnostics("error"))


# ── J: MCP ───────────────────────────────────────────────────────────────────

def test_J_mcp_agent_can_query_project_knowledge(ws, tmp_path):
    kn = open_kn(ws)
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")
    env = {**os.environ, "PYTHONPATH": str(LIF), "LIF_KNOWLEDGE_INDEX": str(tmp_path / "mcp.db")}
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search_objects", "arguments": {"query": "orchestrator k3s", "type": "decision"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "why_decision", "arguments": {"key": "use-k3s"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "create_object", "arguments": {"type": "task", "fields": {"status": "open"}}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "query_decisions_depending_on", "arguments": {"object": "single-node-primary"}}}]
    p = subprocess.run([sys.executable, "-m", "lif.knowledge.mcp", "--root", str(ws), "--agent", "reader"],
                       input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True,
                       env=env, timeout=60)
    replies = {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4, 5, 6}                    # the notification got no reply
    assert replies[1]["result"]["serverInfo"]["name"] == "lif-knowledge"
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert {"search_objects", "get_object", "trace_relationship", "assemble_context", "execute_skill",
            "render_view", "query_decisions_depending_on"} <= names
    found = json.loads(replies[3]["result"]["content"][0]["text"])
    assert found["results"][0]["key"] == "proj::use-k3s"
    why = json.loads(replies[4]["result"]["content"][0]["text"])
    assert {e["key"] for e in why["evidence"]} == {"proj::dgx-baseline", "proj::k3s-eval"}
    assert replies[5]["result"]["isError"] and "permission denied" in replies[5]["result"]["content"][0]["text"]
    assert [d["key"] for d in json.loads(replies[6]["result"]["content"][0]["text"])] == ["proj::use-k3s"]


# ── K: views read the same graph objects ─────────────────────────────────────

def test_K_view_uses_same_graph_objects_as_agent(ws):
    kn = open_kn(ws)
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")
    from lif.knowledge import views
    from lif.knowledge.mcp import call
    v = views.render(kn, "decisions")
    row = next(r for r in v["rows"] if r["key"] == "proj::use-k3s")
    agent_view = call(kn, "agent:any", "get_object", {"key": row["key"]})
    assert row["status"] == agent_view["status"] and row["title"] == agent_view["title"]
    assert set(row["evidence"]) == {l["target"] for l in agent_view["links_out"] if l["field"] == "evidence"}
    kn.writer("human").update("use-k3s", set={"status": "superseded"})
    assert next(r for r in views.render(kn, "decisions")["rows"] if r["key"] == "proj::use-k3s")["status"] == "superseded"
    lin = views.render(kn, "decision-lineage", {"object": "use-k3s"})
    assert "proj::single-node-primary" in lin["graph"]["upstream"]


# ── L: model promotion → persistent knowledge ────────────────────────────────

def _ops_repo(ws: Path) -> None:
    write(ws / "repos/ops/repo.yaml", """
        name: ops
        kind: project
        project: proj
        dependencies:
          - {name: proj}
          - {name: knowledge-governance, version: ^1.0}
          - {name: agent-workflows, version: ^1.0}
          - {name: ai-infrastructure-core, version: ^1.0}
          - {name: model-evaluation, version: ^1.0}
          - {name: incident-response, version: ^1.0}
    """)


def test_L_model_promotion_generates_knowledge(ws, tmp_path):
    _ops_repo(ws)
    from lif.models.registry import Registry
    reg = Registry(str(tmp_path / "registry.db"))
    sha = "a" * 40
    reg.upsert("m-4b", "org/m-4b", sha, "llm", "APPROVED", profile={"params_b": 4})
    reg.add_benchmark("m-4b", "core", {}, {"quality": 0.81, "decode_tps_p50": 21.1})
    reg.set_alias("local/default", ["m-4b"], "operator", note="seed")
    reg.transition("m-4b", "PRODUCTION", "promoted to local/default", "operator")
    kn = open_kn(ws)
    from lif.knowledge.events import EventSink
    sink = EventSink(kn)
    written = sink.consume(reg.activity(500))
    assert written and sink.cursor > 0
    dec = kn.graph.find(type="decision", repo="ops")
    assert len(dec) == 1 and dec[0]["fields"]["kind"] == "model-promotion"
    why = kn.graph.why(dec[0]["key"])
    assert why["evidence"][0]["type"] == "benchmark"
    dep = kn.graph.find(type="deployment", repo="ops")[0]
    assert dep["fields"]["alias"] == "local/default"
    assert dec[0]["key"] in {a["key"] for a in kn.graph.why(dep["key"]).get("affects", [])} or \
        dep["key"] in {x["key"] for x in why["affects"]}
    assert kn.graph.find(type="event", where=["kind=MODEL_PROMOTED"])
    assert not [d for d in kn.store.diagnostics("error") if d["key"] and d["key"].startswith("ops::")]
    assert sink.consume(reg.activity(500)) == []                 # idempotent: re-consuming adds nothing new


def test_events_refuse_without_private_repo(ws):
    kn = open_kn(ws)
    from lif.knowledge.events import EventSink
    with pytest.raises(WriteError):
        EventSink(kn)


# ── M: incident → lesson → method change → skill/check update ────────────────

def _incident_setup(ws: Path) -> None:
    _ops_repo(ws)
    k = ws / "repos/ops/knowledge"
    write(k / "benchmarks-can-run-anytime.md", """
        ---
        type: assumption
        statement: Large candidate benchmarks can run whenever the primary workload is idle at start.
        status: active
        supported_by: ["[[proj::dgx-baseline]]"]
        ---
    """)
    write(k / "benchmark-admission.md", """
        ---
        type: method
        status: active
        version: 2
        title: Benchmark admission
        steps: [Check the primary-workload state before starting]
        ---
    """)
    write(k / "run-candidate-benchmarks.md", """
        ---
        type: decision
        status: accepted
        date: 2026-09-01
        evidence: ["[[proj::dgx-baseline]]"]
        assumptions: ["[[benchmarks-can-run-anytime]]"]
        ---
    """)
    write(k / "incident-2026-10-14.md", """
        ---
        type: incident
        title: "SYNTHETIC: OOM — candidate benchmark + primary-workload image job"
        synthetic: true
        status: resolved
        severity: sev2
        date: 2026-10-14
        location: test-fixture
        root_cause: A large candidate benchmark held unified memory when an image job started.
        failed_assumptions: ["[[benchmarks-can-run-anytime]]"]
        ---
        Test fixture, not a real event.
    """)
    sk = ws / "repos/ops/skills/benchmark-guard"
    write(sk / "SKILL.md", """
        ---
        type: skill
        name: benchmark-guard
        version: 1
        method: "[[benchmark-admission]]"
        ---
        Run before any candidate benchmark.
    """)
    write(sk / "checks.yaml", "inputs:\n  job: {type: object}\nchecks: []\n")


def test_M_gpu_incident_creates_lesson_method_change_and_check(ws):
    _incident_setup(ws)
    kn = open_kn(ws)
    from lif.knowledge.learn import learn
    from lif.knowledge.skills import run_skill
    res = learn(kn, "ops", "incident-2026-10-14",
                observation="Candidate benchmark caused unified-memory pressure while an image job started.",
                lesson="Large candidate benchmarks must stop when primary-workload demand becomes HIGH.",
                lesson_id="lesson-stop-benchmarks-on-high-demand", invalidates=["benchmarks-can-run-anytime"],
                method="benchmark-admission", method_change="require a hardware-fit margin; abort on HIGH",
                new_step="Abort when primary-workload state becomes HIGH or IMMINENT",
                skill="benchmark-guard",
                check={"id": "fit-margin", "description": "Job declares a hardware-fit margin",
                       "object": "{job}", "where": ["fit_margin_gib>=8"]})
    lesson = kn.graph.get(res["lesson"])
    assert ("evidence", "ops::incident-2026-10-14") in {(l["field"], l["target"]) for l in lesson["links_out"]}
    m = kn.store.get("ops::benchmark-admission")
    assert m["fields"]["version"] == 3 and m["fields"]["revisions"][0]["because"] == "[[lesson-stop-benchmarks-on-high-demand]]"
    assert "Abort when primary-workload state becomes HIGH or IMMINENT" in m["fields"]["steps"]
    assert "ops::lesson-stop-benchmarks-on-high-demand" in {d["key"] for d in kn.graph.dependencies("benchmark-admission")}
    checks = yaml.safe_load((ws / "repos/ops/skills/benchmark-guard/checks.yaml").read_text())["checks"]
    assert checks[0]["id"] == "fit-margin" and "lesson-stop-benchmarks-on-high-demand" in checks[0]["because"]
    assert kn.store.get("ops::benchmark-guard")["fields"]["version"] == 2
    # the invalidated assumption queued its dependent decision for review
    assert "ops::run-candidate-benchmarks" in {r["key"] for r in res["reconsiderations"]}
    # later agents see the new check when they run the skill
    kn.writer("human").create("task", {"title": "bench big model", "status": "open", "fit_margin_gib": 2},
                              id="bench-job", repo="ops")
    out = run_skill(kn, "benchmark-guard", {"job": "bench-job"})
    assert out["failed"] == ["fit-margin"] and out["checks"][0]["because"]


# ── N: rebuild from canonical text ───────────────────────────────────────────

def test_N_graph_rebuilds_from_canonical_repos(ws, tmp_path):
    db = tmp_path / "index.db"
    kn = Knowledge.open(ws, index=db)
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")

    def snapshot(k):
        objs = {(o["key"], o["sha"], json.dumps(o["fields"], sort_keys=True, default=str))
                for o in k.store.objects(limit=10 ** 6)}
        edges = {(e["src"], e["rel"], e["dst"], e["kind"]) for e in k.store.db.q("SELECT * FROM edges")}
        diags = {(d["code"], d["key"]) for d in k.store.diagnostics()}
        return objs, edges, diags

    before = snapshot(kn)
    del kn
    db.unlink()
    for side in ("-wal", "-shm"):
        Path(str(db) + side).unlink(missing_ok=True)
    rebuilt = Knowledge.open(ws, index=db)
    assert snapshot(rebuilt) == before
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    with pytest.raises(Exception):
        Knowledge.open(ws, index=corrupt)
    corrupt.unlink()
    assert snapshot(Knowledge.open(ws, index=corrupt)) == before         # delete + rebuild recovers


def test_incremental_compile_reuses_unchanged_files(ws, tmp_path):
    kn = Knowledge.open(ws, index=tmp_path / "i.db")
    first = kn.store.meta("last_compile")
    assert first["files_parsed"] > 0
    st = kn.refresh(force=True)
    assert st["files_parsed"] == 0 and st["changed"] == 0
    write(ws / "repos/proj/knowledge/new-q.md", "---\ntype: question\nstatus: open\n---\n")
    st = kn.refresh(force=True)
    assert st["files_parsed"] == 1 and st["changed"] == 1


# ── O: offline ───────────────────────────────────────────────────────────────

def test_O_usable_without_internet(ws, monkeypatch):
    import httpx
    from lif.knowledge.search import Embedder

    def no_net(*a, **k):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "post", no_net)
    monkeypatch.setattr(httpx.Client, "send", no_net)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_net)
    monkeypatch.setenv("TYPE_SAFE_JEV_API_KEY", "x" * 20)
    kn = Knowledge.open(ws, index=":memory:", embedder=Embedder(url="http://gateway.invalid"))
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")
    res = kn.search.search("k3s orchestrator")
    assert res["results"][0]["key"] == "proj::use-k3s" and "semantic" not in res["retrieval"]
    pkg = kn.context.assemble("incident: why k3s? outage root cause on the single node")
    assert pkg["items"] and pkg["profile"]["name"]
    from lif.knowledge.decisions import decide
    d = decide("knowledge-object-type", {"text": "We decided to use K3s for the single node."})
    assert d["decision"] == "decision" and d["provider"].startswith("rules")
    assert kn.context.resume("proj")["important_decisions"]


def test_semantic_retrieval_when_embeddings_available(ws):
    from lif.knowledge.search import Embedder

    class Fake(Embedder):
        def __init__(self):
            super().__init__(url="http://fake")

        def embed(self, texts):
            return [[1.0 if "single binary" in t.lower() or "lightweight" in t.lower() else 0.0, 1.0] for t in texts]
    kn = Knowledge.open(ws, index=":memory:", embedder=Fake())
    res = kn.search.search("lightweight kubernetes")
    assert "semantic" in res["retrieval"]


# ── context assembly: budget + provenance ────────────────────────────────────

def test_context_package_is_budgeted_and_carries_provenance(ws):
    kn = open_kn(ws)
    kn.writer("agent:a1").create("decision", decision_fields(), "x " * 50, id="use-k3s", repo="proj")
    pkg = kn.context.assemble("Should we keep [[use-k3s]]?", project="proj", budget_tokens=300)
    assert pkg["used_tokens"] <= 300
    first = pkg["items"][0]
    assert first["key"] == "proj::use-k3s" and first["why"] == "named in the task"
    prov = first["provenance"]
    assert prov["updated_by"] == "agent:a1" and prov["path"].endswith("use-k3s.md") and prov["project"] == "proj"
    assert "proj::dgx-baseline" in prov["sources"]
    small = kn.context.assemble("Should we keep [[use-k3s]]?", project="proj", budget_tokens=120)
    assert small["dropped"] > 0 or len(small["items"]) < len(pkg["items"])


def test_api_serves_knowledge_with_scopes(ws, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LIF_KNOWLEDGE_ROOT", str(ws))
    monkeypatch.setenv("LIF_KNOWLEDGE_INDEX", ":memory:")
    monkeypatch.setenv("LIF_ADMIN_KEYS", "ops:admin-key-123")
    monkeypatch.setenv("LIF_KNOWLEDGE_KEYS", "reader:reader-key-123\nwriter-bot:writer-key-123")
    from lif.knowledge import app as kapp
    with TestClient(kapp.app) as c:
        assert c.get("/v1/knowledge/health").status_code == 401
        h = {"Authorization": "Bearer reader-key-123"}
        assert c.get("/v1/knowledge/health", headers=h).json()["objects"] >= 4
        r = c.post("/v1/knowledge/objects", headers=h, json={"type": "task", "fields": {"status": "open"}})
        assert r.status_code == 403
        w = {"Authorization": "Bearer writer-key-123"}
        r = c.post("/v1/knowledge/objects", headers=w,
                   json={"type": "decision", "id": "use-k3s", "repo": "proj", "fields": decision_fields()})
        assert r.status_code == 200, r.text
        assert c.get("/v1/knowledge/why/use-k3s", headers=h).json()["selected"] == "k3s"
        v = c.get("/v1/knowledge/views/decisions", headers=h).json()
        assert any(r["key"] == "proj::use-k3s" for r in v["rows"])
        bad = c.post("/v1/knowledge/objects", headers=w, json={"type": "decision", "fields": {"status": "nope"}})
        assert bad.status_code == 422 and bad.json()["diagnostics"]


# ── the full loop (§112) ─────────────────────────────────────────────────────

def test_full_loop_work_to_reusable_method(ws, tmp_path):
    """source → claim → assumption → decision → task → result → new evidence contradicts → dependents traced →
    reconsideration → review → lesson → method improved → skill updated → package published → next project reuses."""
    _incident_setup(ws)
    kn = open_kn(ws)
    w = kn.writer("agent:claude-code")
    from lif.knowledge.learn import ingest, learn
    # 1. a source enters the workspace and typed candidates are extracted, linked to passages
    notes = write(tmp_path / "capacity-notes.md", """
        # Capacity notes

        The primary workload uses the GPU intermittently and is idle most nights.

        We decided to run candidate benchmarks overnight.

        Should benchmarks pause during image jobs?
    """)
    ing = ingest(kn, notes, "ops", kind="meeting", actor="agent:claude-code")
    kinds = {c["type"] for c in ing["created"] if "type" in c}
    assert {"claim", "decision", "question"} <= kinds
    claim = next(c["key"] for c in ing["created"] if c.get("type") == "claim")
    assert kn.graph.trace_claim(claim)["extracted_from"]["text"].startswith("The primary workload uses the GPU")
    # 2. assumption recorded from the claim, decision made on it, work created and done
    w.create("assumption", {"title": "Primary workload is idle at night", "statement": "Idle at night.",
                            "status": "active", "supported_by": ["[[proj::dgx-baseline]]"]},
             id="idle-at-night", repo="ops")
    w.create("decision", {"title": "Benchmark overnight", "status": "accepted", "date": "2026-10-01",
                          "evidence": ["[[proj::dgx-baseline]]"], "assumptions": ["[[idle-at-night]]"]},
             id="benchmark-overnight", repo="ops")
    w.create("task", {"title": "Schedule nightly benchmarks", "status": "completed",
                      "implements": "[[benchmark-overnight]]"}, id="schedule-nightly", repo="ops")
    w.create("result", {"title": "First nightly run", "outcome": "positive", "task": "[[schedule-nightly]]",
                        "location": "runs/nightly-1.json"}, id="nightly-result", repo="ops")
    assert not kn.store.reconsiderations()
    # 3. new evidence arrives and contradicts the assumption → dependents traced → reconsideration surfaced
    w.create("benchmark", {"title": "Night load sample", "summary": {"gpu_busy_pct": 70}, "date": "2026-10-03",
                           "location": "metrics/night.json"}, id="night-load", repo="ops")
    w.update("idle-at-night", append={"contradicted_by": ["[[night-load]]"]})
    queue = {r["key"]: r for r in kn.store.reconsiderations()}
    assert "ops::benchmark-overnight" in queue and "ops::schedule-nightly" in queue
    assert kn.store.get("ops::benchmark-overnight")["status"] == "accepted"      # surfaced, not reversed
    impact = kn.graph.impact("idle-at-night")
    assert {"decision", "task"} <= set(impact["by_type"])
    # 4. human/agent review, lesson, method improved, skill updated
    w.create("review", {"reviews": ["[[benchmark-overnight]]"], "trigger": "[[idle-at-night]]", "outcome": "revised",
                        "date": "2026-10-04"}, repo="ops")
    assert "ops::benchmark-overnight" not in {r["key"] for r in kn.store.reconsiderations()}
    res = learn(kn, "ops", "incident-2026-10-14", observation="Night is not idle.",
                lesson="Admit benchmarks on measured load, not on time of day.", lesson_id="lesson-measure-load",
                invalidates=["idle-at-night"], method="benchmark-admission",
                new_step="Admit only when measured GPU load is below 20%", skill="benchmark-guard",
                check={"id": "measured-load", "description": "Job cites a load measurement",
                       "object": "{job}", "where": ["load_measurement~ "]}, actor="agent:claude-code")
    assert res["skill"]["version"] == 2
    # 5. the improved practice becomes a package; the next project reuses it
    from lif.knowledge import packages
    pkg_src = tmp_path / "gpu-benchmark-practice"
    packages.extract(kn, "ops", "gpu-benchmark-practice", "1.0.0", pkg_src, objects=["benchmark-admission"],
                     skills=["benchmark-guard"], description="Benchmark admission learned from a (synthetic) incident",
                     dependencies=[{"name": "knowledge-governance", "version": "^1.0"}])
    assert "ops::lesson-measure-load" in (pkg_src / "methods/benchmark-admission.md").read_text()
    packages.publish(kn, pkg_src, approved=True)
    nxt = ws / "repos/next"
    write(nxt / "repo.yaml", """
        name: next
        kind: project
        dependencies: [{name: knowledge-governance, version: ^1.0}]
    """)
    wsy = yaml.safe_load((ws / "workspace.yaml").read_text())
    wsy["repos"].append({"path": "repos/next"})
    (ws / "workspace.yaml").write_text(yaml.safe_dump(wsy))
    kn2 = open_kn(ws)
    out = packages.install(kn2, "next", "gpu-benchmark-practice@^1.0", approved=True)
    assert "benchmark-guard" in out["skills"]
    from lif.knowledge.skills import list_skills
    reused = [s for s in list_skills(kn2) if s["name"] == "benchmark-guard" and s["repo"] == "gpu-benchmark-practice"]
    assert reused and reused[0]["version"] == 2
    published_checks = yaml.safe_load((ws / "registry/gpu-benchmark-practice/1.0.0/skills/benchmark-guard/checks.yaml")
                                      .read_text())["checks"]
    assert {"measured-load"} <= {c["id"] for c in published_checks}


def test_sink_records_durable_facts_only_when_enabled(ws):
    _ops_repo(ws)
    from lif.knowledge.sink import KnowledgeSink
    assert KnowledgeSink(root=ws).record("decision-release", {"id": "r1"}) is None        # off by default
    s = KnowledgeSink(root=ws, actor="decision-engineering", enabled=True)
    k = s.record("decision-release", {"id": "batch-priority-v3", "title": "batch-priority v3 → production",
                                      "evidence": {"accuracy": 0.97}}, [("version-of", "batch-priority")])
    o = s.kn.store.get(k)
    assert o["type_name"] == "event" and o["fields"]["kind"] == "decision-release"
    assert o["fields"]["detail"]["unresolved_links"] == ["version-of → batch-priority"]
    assert s.record("decision-release", {"id": "batch-priority-v3"}) == k                  # idempotent
    t = s.record("task", {"id": "t-1", "title": "Follow up", "status": "open"}, [("related", "proj::use-nothing")])
    assert s.kn.store.get(t)["type_name"] == "task"


# ── shipped packages: compile cleanly, type tests, skill tests (§97, §98) ─────

def test_registry_packages_compile_and_pass_their_tests(tmp_path):
    from lif.knowledge import packages, skills
    from lif.knowledge.repo import Registry
    reg = Registry(REGISTRY)
    for name, versions in reg.packages().items():
        root = tmp_path / name
        write(root / "workspace.yaml", "repos: [p]\n")
        write(root / "p/repo.yaml", f"name: p\ndependencies: [{{name: {name}, version: '{versions[-1]}'}}]\n")
        kn = Knowledge.open(root, index=":memory:", registry=REGISTRY)
        errs = [d for d in kn.store.diagnostics("error")]
        assert not errs, (name, errs[:3])
        assert packages.test_types(kn, name)["passed"], name
        for s in skills.list_skills(kn):
            if s["has_tests"]:
                assert skills.test_skill(kn, s["name"])["passed"], s["name"]


def test_dogfood_workspace_has_no_errors():
    kn = Knowledge.open(LIF / "knowledge", index=":memory:")
    assert not kn.store.diagnostics("error"), kn.store.diagnostics("error")[:3]
    assert kn.graph.why("cpu-tiers-for-local-inference")["evidence"]


def test_semantic_diff_against_git_revision(ws, tmp_path):
    git = lambda *a: subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True)
    kn = open_kn(ws)
    kn.writer("human").create("decision", decision_fields(status="proposed", evidence=["[[dgx-baseline]]"]),
                              id="use-k3s", repo="proj")
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    kn.writer("human").update("use-k3s", set={"status": "accepted"}, append={"evidence": ["[[k3s-eval]]"]},
                              unset=["assumptions"])
    env = {**os.environ, "PYTHONPATH": str(LIF), "LIF_KNOWLEDGE_INDEX": str(tmp_path / "d.db")}
    out = subprocess.run([sys.executable, "-m", "lif.knowledge.cli", "--root", str(ws), "--json", "diff", "HEAD"],
                         capture_output=True, text=True, env=env, check=True)
    entry = next(e for e in json.loads(out.stdout) if e["key"] == "proj::use-k3s")
    assert entry["change"] == "changed" and entry["fields"]["status"] == {"from": "proposed", "to": "accepted"}
    assert {"field": "evidence", "target": "proj::k3s-eval"} in entry["links_added"]
    assert {"field": "assumptions", "target": "proj::single-node-primary"} in entry["links_removed"]


def test_resume_reports_only_changes_after_the_checkpoint(ws):
    kn = open_kn(ws)
    kn.writer("human").create("decision", decision_fields(), id="use-k3s", repo="proj")
    kn.context.checkpoint("proj", "agent:a1", "Before the change.")
    cp = kn.context.last_checkpoint("proj")
    later = dt_ts(cp["fields"]["ended"]) + 5
    p = ws / "repos/proj/knowledge/single-node-primary.md"
    p.write_text(p.read_text().replace("status: active", "status: challenged"))
    os.utime(p, (later, later))
    kn.refresh_interval = 0                       # an agent editing then resuming within the same second
    r = kn.context.resume("proj")
    assert [x["key"] for x in r["recent_changes"]] == ["proj::single-node-primary"]
    assert [x["key"] for x in r["changed_assumptions"]] == ["proj::single-node-primary"]
    assert any(x["key"] == "proj::use-k3s" for x in r["pending_reviews"])


def dt_ts(iso: str) -> float:
    import datetime as _dt
    return _dt.datetime.fromisoformat(iso).timestamp()


def test_L_rollback_is_recorded_as_rollback_not_promotion(ws, tmp_path):
    _ops_repo(ws)
    from lif.knowledge.events import EventSink
    from lif.models.registry import Registry
    reg = Registry(str(tmp_path / "registry.db"))
    for mid in ("m-a", "m-b"):
        reg.upsert(mid, f"org/{mid}", mid[-1] * 40, "llm", "APPROVED")
        reg.add_benchmark(mid, "core", {}, {"quality": 0.8})
    reg.set_alias("local/default", ["m-a"], "operator")
    reg.transition("m-a", "PRODUCTION", "promoted to local/default", "operator")
    reg.transition("m-b", "PRODUCTION", "promoted to local/default", "operator")      # what Lifecycle.promote does
    reg.set_alias("local/default", ["m-b", "m-a"], "operator", note="promote m-b")
    reg.rollback_alias("local/default", "operator")                                     # what Lifecycle.rollback does
    reg.transition("m-a", "PRODUCTION", "rollback of local/default", "operator", force=True)
    reg.transition("m-b", "STANDBY", "rolled back from local/default", "operator", force=True)
    kn = open_kn(ws)
    EventSink(kn).consume(reg.activity(500))
    decisions = kn.graph.find(type="decision", repo="ops")
    assert sorted(d["fields"]["selected"] for d in decisions) == ["m-a", "m-b"]          # no decision for the restore
    deps = {d["fields"]["profile"]: d for d in kn.graph.find(type="deployment", repo="ops")
            if d["status"] == "active"}
    assert set(deps) == {"m-a"} and deps["m-a"]["fields"]["alias"] == "local/default"
    gone = [d for d in kn.graph.find(type="deployment", repo="ops") if d["fields"]["profile"] == "m-b"]
    assert gone and all(d["status"] == "superseded" for d in gone)
    kinds = {e["fields"]["kind"] for e in kn.graph.find(type="event", repo="ops")}
    assert {"MODEL_PROMOTED", "MODEL_ROLLBACK", "MODEL_RESTORED"} <= kinds
    assert not [d for d in kn.store.diagnostics("error") if (d["key"] or "").startswith("ops::")]


def test_write_repos_are_enforced_for_agents(ws):
    _ops_repo(ws)
    kn = open_kn(ws)
    from lif.knowledge.mcp import call
    with pytest.raises(WriteError, match="may not write to repo proj"):
        call(kn, "agent:ops-bot", "create_object", {"type": "task", "fields": {"status": "open"}, "repo": "proj"})
    with pytest.raises(WriteError, match="may not write"):
        call(kn, "agent:ops-bot", "update_object", {"key": "proj::single-node-primary", "set": {"confidence": "low"}})
    res = call(kn, "agent:ops-bot", "create_object", {"type": "task", "fields": {"status": "open"}})
    assert res["key"].startswith("ops::")                      # defaults to an allowed repo
    assert kn.writer("human").create("task", {"status": "open"}, id="h1", repo="proj")["key"] == "proj::h1"


def test_context_items_carry_data_class(ws):
    _ops_repo(ws)
    write(ws / "repos/ops/knowledge/secret-ish.md", """
        ---
        type: assumption
        statement: Production runs nightly on the single node.
        status: active
        supported_by: ["[[proj::dgx-baseline]]"]
        ---
    """)
    m = yaml.safe_load((ws / "repos/proj/repo.yaml").read_text())
    m["data_class"] = "PUBLIC"
    (ws / "repos/proj/repo.yaml").write_text(yaml.safe_dump(m))
    kn = open_kn(ws)
    pkg = kn.context.assemble("single node [[ops::secret-ish]] [[proj::single-node-primary]]")
    classes = {i["key"]: i["data_class"] for i in pkg["items"]}
    assert classes["proj::single-node-primary"] == "PUBLIC"
    assert classes["ops::secret-ish"] == "CONFIDENTIAL"          # no manifest class → privacy default
    assert pkg["data_class"] == "CONFIDENTIAL"
