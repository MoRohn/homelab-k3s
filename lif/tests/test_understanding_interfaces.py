"""API, MCP, CLI and the router evaluation set (spec §95–§99, §103)."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lif.understanding import api, evalset
from lif.understanding import mcp as umcp
from lif.understanding.compiler import Compiler
from lif.understanding.store import Store

GPU = "Why are these Kubernetes pods not reaching available GPUs?"
RES = "How does changing GPU reservation affect throughput?"


@pytest.fixture
def comp(tmp_path):
    return Compiler(Store(str(tmp_path / "u.db")), collectors={})


@pytest.fixture
def client(comp):
    app = FastAPI()
    app.include_router(api.make_router(lambda: comp))
    return TestClient(app)


def test_explain_then_overrides_over_http(client, comp):
    r = client.post("/v1/explain", json={"question": GPU, "audience": "engineer", "time_budget_seconds": 180})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "done"
    assert body["primary"] == "mermaid" and body["supporting"] == ["ste-prose"]
    assert "Node affinity keeps the pending pods off node B" in body["summary"] and body["evaluation"]["semantic_ok"]
    xid, sid = body["explanation_id"], body["session"]
    got = client.get(f"/v1/explanations/{xid}").json()
    assert got["spec"]["schema_version"] == "ExplanationSpec/v1" and got["validation"]["ok"]
    table = client.post(f"/v1/explanations/{xid}/render", json={"format": "html"}).json()
    assert table["primary"] == "static-explainer"
    simple = client.post(f"/v1/explanations/{xid}/simplify", json={}).json()
    assert simple["status"] == "done" and simple["explanation_id"] == xid
    ev = client.post(f"/v1/explanations/{xid}/evaluate", json={"session": sid}).json()
    assert {f["kind"] for f in ev["feedback"]} == {"override", "simplify"}
    assert client.post(f"/v1/sessions/{sid}/feedback", json={"kind": "helpful"}).json() == {"ok": True}
    assert client.post(f"/v1/sessions/{sid}/feedback", json={"kind": "helpfull"}).status_code == 422
    assert client.get(f"/v1/explanations/{xid}/history").status_code == 200
    assert client.get("/v1/explanations/no-such-spec/history").status_code == 404
    assert comp.builder_calls == 1


def test_sse_stream_and_sandboxed_artifacts(client):
    with client.stream("POST", "/v1/explain?stream=1", json={"question": RES}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [ln[7:] for ln in r.iter_lines() if ln.startswith("event: ")]
    assert events[0] == "analysis.started" and "simulation.ready" in events and events[-1] == "done"
    body = client.post("/v1/explain", json={"question": RES}).json()
    a = client.get(f"/v1/sessions/{body['session']}/artifacts/simulation")
    assert a.status_code == 200 and a.headers["content-type"].startswith("text/html")
    assert a.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert client.get(f"/v1/sessions/{body['session']}/artifacts/nope").status_code == 404


def test_renderers_and_errors(client):
    names = {r["name"]: r for r in client.get("/v1/renderers").json()["renderers"]}
    assert names["narrated-video"]["available"] is False
    assert client.get("/v1/explanations/x-missing").status_code == 404
    bad = client.post("/v1/explain", json={"question": "Teach me how transformer attention works"}).json()
    assert bad["status"] == "error" and "no local model" in bad["error"]["detail"]
    assert client.post("/v1/explain", json={"question": ""}).status_code == 422


def test_mcp_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(umcp, "_comp", Compiler(Store(str(tmp_path / "m.db")), collectors={}))
    monkeypatch.setenv("HOME", str(tmp_path))
    listed = umcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in listed} >= {"explanation_create", "explanation_get", "explanation_render",
                                           "explanation_evaluate", "explanation_simplify", "explanation_deepen"}
    call = lambda name, args: json.loads(umcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",   # noqa: E731
                                                      "params": {"name": name, "arguments": args}})
                                         ["result"]["content"][0]["text"])
    made = call("explanation_create", {"question": RES})
    assert made["status"] == "done" and made["primary"] == "simulation"
    assert made["artifacts"]["simulation"]["path"].endswith("simulation.html")
    assert call("explanation_get", {"explanation_id": made["explanation_id"]})["spec"]["simulation"]
    assert call("explanation_simplify", {"session": made["session"]})["status"] == "done"
    assert "evaluation" in call("explanation_evaluate", {"explanation_id": made["explanation_id"]})


def test_cli_explain(tmp_path, capsys, monkeypatch):
    from lif.cli.main import main
    monkeypatch.setenv("LIF_UNDERSTANDING_DB", str(tmp_path / "c.db"))
    assert main(["explain", GPU, "--offline", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr()
    assert "```mermaid" in out.out and "Diagram selected" in out.err
    sid = out.err.split("session ")[-1].split()[0]
    assert main(["explain", sid, "--format", "excalidraw", "--offline", "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "excalidraw.excalidraw").exists()
    assert main(["explain", sid, "--show-ir", "--offline"]) == 0
    assert '"schema_version": "ExplanationSpec/v1"' in capsys.readouterr().out
    assert main(["explain", "--renderers"]) == 0 and main(["explain", "--lint-packages"]) == 0


def test_router_evaluation_set_passes():
    rep = evalset.run()
    assert rep["passed"] == rep["total"] >= 13, [c for c in rep["cases"] if not c["ok"]]
