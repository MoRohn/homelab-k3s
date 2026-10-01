import json

import httpx
import pytest

from lif.cli.main import Api, build_parser, main, table

OVERVIEW = {
    "blerbz": {"state": "LOW", "reason": "P(production within 1h) = 14%", "admissible_mib": 1217.0,
               "mem_available_mib": 9806.0, "reachable": True},
    "capabilities": {"aliases": {
        "local/fast": {"available": True, "served_by": "qwen3-4b", "fallback": False, "degraded": False, "reason": ""},
        "local/default": {"available": True, "served_by": "qwen3-4b", "fallback": False, "degraded": False,
                          "reason": ""},
        "local/embedding": {"available": True, "served_by": "emb", "fallback": False, "degraded": False, "reason": ""},
        "local/vision": {"available": False, "reason": "no local model is deployed"}}},
    "decision_fabric": {"jev_enabled": True, "jev_breaker_open": False, "jev_model": "jev-1.13.0"},
    "batch": {"paused": False},
    "availability_24h": {"gateway": {"samples": 10, "availability": 1.0}},
    "models": {"PRODUCTION": 3},
}


def make_api(routes: dict, seen: list | None = None) -> Api:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((req.method, req.url.path, req.content, req.headers.get("authorization")))
        key = (req.method, req.url.path)
        if key not in routes:
            return httpx.Response(404, json={"error": "no route"})
        status, body = routes[key]
        return httpx.Response(status, json=body)
    return Api(transport=httpx.MockTransport(handler))


def test_parser_models_actions():
    a = build_parser().parse_args(["models", "canary", "m1", "--alias", "local/fast", "--percent", "5"])
    assert (a.cmd, a.action, a.target, a.alias, a.percent) == ("models", "canary", "m1", "local/fast", 5.0)
    a = build_parser().parse_args(["--json", "models", "rollback", "local/default", "--to-version", "2"])
    assert a.json and a.to_version == 2
    with pytest.raises(SystemExit):
        build_parser().parse_args(["models", "explode"])


def test_table_alignment():
    t = table([["a", 1], ["bbb", None]], ["x", "y"]).splitlines()
    assert t[0].startswith("x  ") and t[1].startswith("---") and t[3].startswith("bbb")


def test_status_renders(capsys, monkeypatch):
    monkeypatch.setenv("LIF_ADMIN_KEY", "k")
    seen = []
    api = make_api({("GET", "/v1/overview"): (200, OVERVIEW)}, seen)
    assert main(["status"], api) == 0
    o = capsys.readouterr().out
    assert "Workload state      LOW" in o and "local/fast" in o and "100.0%" in o
    assert seen[0][3] == "Bearer k"


def test_doctor_warns_but_passes_without_vision(capsys):
    api = make_api({("GET", "/v1/overview"): (200, OVERVIEW),
                    ("GET", "/v1/health"): (200, {"status": "ok", "useful_local_ai": True})})
    assert main(["doctor"], api) == 0
    o = capsys.readouterr().out
    assert "[WARN] alias local/vision" in o and "[PASS] gateway" in o


def test_doctor_fails_when_fast_missing(capsys):
    ov = json.loads(json.dumps(OVERVIEW))
    ov["capabilities"]["aliases"]["local/fast"] = {"available": False, "reason": "all models unavailable"}
    api = make_api({("GET", "/v1/overview"): (200, ov),
                    ("GET", "/v1/health"): (200, {"status": "degraded", "useful_local_ai": True})})
    assert main(["doctor"], api) == 1
    assert "[FAIL] alias local/fast" in capsys.readouterr().out


def test_http_error_message_and_exit_code(capsys):
    api = make_api({("POST", "/v1/models/m1/promote"): (409, {"error": "m1 is STAGED; never promote an untested model"})})
    assert main(["models", "promote", "m1"], api) == 2
    assert "never promote an untested model" in capsys.readouterr().err


def test_mutation_bodies():
    seen = []
    api = make_api({("POST", "/v1/models/m1/unload"): (200, {"deployment": "x"}),
                    ("POST", "/v1/settings"): (200, {"maintenance": True, "batch_paused": True}),
                    ("POST", "/v1/aliases/rollback"): (200, {"alias": "local/default", "version": 3, "chain": ["a"]})},
                   seen)
    assert main(["models", "unload", "m1", "--force"], api) == 0
    assert main(["maintenance", "on"], api) == 0
    assert main(["batch", "pause"], api) == 0
    assert main(["models", "rollback", "local/default"], api) == 0
    bodies = [json.loads(c) for _, _, c, _ in seen]
    assert bodies == [{"force": True}, {"maintenance": True}, {"batch_paused": True}, {"alias": "local/default"}]


def test_json_output(capsys):
    api = make_api({("GET", "/v1/gpu"): (200, {"state": "IMMINENT", "reason": "1 production lease(s) live"})})
    assert main(["--json", "gpu"], api) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "IMMINENT"


def test_unreachable(capsys):
    def boom(req):
        raise httpx.ConnectError("refused")
    api = Api(transport=httpx.MockTransport(boom))
    assert main(["gpu"], api) == 2
    assert "cannot reach" in capsys.readouterr().err
