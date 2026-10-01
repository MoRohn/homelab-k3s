"""Model-server memory limits must hold the weights AND the anonymous memory.

A llama.cpp pod with --no-repack is charged for the mmap'd weights it touches as well as its KV cache and
buffers. tier0 at 3584Mi (weights 2,381 + anon 1,409 MiB) sat at its cgroup limit and re-read weights from
disk on every token (2026-10-01). These checks keep that from coming back.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lif.controller import templates
from lif.models import hardware_fit

ROOT = Path(__file__).resolve().parents[1]
MODELS = yaml.safe_load((ROOT / "config/models.yaml").read_text())
LIF = yaml.safe_load((ROOT / "config/lif.yaml").read_text())


def _servers() -> list[tuple[str, dict]]:
    out = []
    for f in sorted((ROOT / "deploy/k8s/serving").glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if doc and doc.get("kind") == "Deployment" and \
                    doc["spec"]["template"]["metadata"]["labels"].get("lif.dev/role") == "model-server":
                out.append((f.name, doc))
    return out


def _mib(q: str) -> int:
    assert q.endswith("Mi"), q
    return int(q[:-2])


def _arg(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


SERVERS = _servers()


def test_every_model_server_is_checked():
    assert {n for n, _ in SERVERS} >= {"tier0.yaml", "tier0-small.yaml", "embedding.yaml"}


@pytest.mark.parametrize("name,dep", SERVERS, ids=[n for n, _ in SERVERS])
def test_manifest_limit_matches_profile_and_fits(name, dep):
    labels = dep["spec"]["template"]["metadata"]["labels"]
    p = MODELS["profiles"][labels["lif.dev/profile"]]
    c = dep["spec"]["template"]["spec"]["containers"][0]
    req, lim = _mib(c["resources"]["requests"]["memory"]), _mib(c["resources"]["limits"]["memory"])
    assert req == lim == p["memory_budget_mb"], f"{name}: request/limit/profile budget disagree"
    assert int(_arg(c["args"], "--ctx-size")) == p["context"]
    assert int(_arg(c["args"], "--parallel")) == p["concurrency"]
    locked = "--load-mode" in c["args"] and _arg(c["args"], "--load-mode") == "mmap+mlock"
    assert locked == bool(p.get("mlock")), f"{name}: mlock disagrees with the profile"
    assert "--mlock" not in c["args"], "the pinned llama.cpp exits on --mlock; use --load-mode mmap+mlock"
    # an unset --cache-ram is an 8 GiB anonymous prompt cache: always explicit, always in the budget
    assert int(_arg(c["args"], "--cache-ram")) == p["cache_ram_mib"]
    need = hardware_fit.memory_limit_mib(p["weights_mib"], p["anon_mib"])
    assert lim >= need, f"{name}: {lim} Mi < weights + anon + headroom = {need} Mi"


def test_formula_tracks_the_tier0_measurement():
    """hardware_fit's anon estimate for tier0 stays within 5 % of what the pod actually used."""
    p = MODELS["profiles"]["qwen3-4b-instruct-2507-q4km-cpu"]
    meta = {"gguf_pick": {"size": 2_497_281_120}, "num_layers": 36, "num_kv_heads": 8, "head_dim": 128,
            "vocab_size": 151_936, "params_b": 4.0}
    r = hardware_fit.estimate(meta, context=p["context"], device="cpu")
    assert r.weights_mib == p["weights_mib"] and p["cache_ram_mib"] == hardware_fit.PROMPT_CACHE_MIB
    assert abs(r.anon_mib - p["anon_mib"]) / p["anon_mib"] < 0.05
    assert hardware_fit.memory_limit_mib(r.weights_mib, r.anon_mib) > 3584      # the limit that thrashed


def test_embedding_servers_have_no_logits_buffer():
    meta = {"num_layers": 28, "num_kv_heads": 8, "head_dim": 128, "vocab_size": 151_669}
    assert hardware_fit.runtime_overhead_mib({**meta, "category": "embedding"}, embedding=True) \
        < hardware_fit.runtime_overhead_mib(meta)


@pytest.mark.parametrize("group,lo,hi", [("optional", "shed_optional_below_mib", "restore_optional_above_mib"),
                                         ("secondary", "shed_secondary_below_mib", "restore_secondary_above_mib")])
def test_memory_guard_restore_clears_the_pods_footprint(group, lo, hi):
    """Restoring a shed pod spends up to its limit; the restore line must stay above shed + that, or it flaps."""
    g = LIF["memory_guard"]
    budgets = {labels["app"]: _mib(d["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])
               for _, d in SERVERS for labels in [d["spec"]["template"]["metadata"]["labels"]]}
    for dep in g[group]:
        assert g[hi] >= g[lo] + budgets[dep], f"{dep}: restore {g[hi]} < shed {g[lo]} + limit {budgets[dep]}"


def test_controller_refuses_an_undersized_limit():
    p = {**MODELS["profiles"]["qwen3-4b-instruct-2507-q4km-cpu"], "memory_budget_mb": 3584}
    with pytest.raises(ValueError, match="memory_budget_mb"):
        templates.model_server("tier0", "qwen3-4b-instruct-2507-q4km-cpu", p)
    dep, _ = templates.model_server("tier0", "qwen3-4b-instruct-2507-q4km-cpu",
                                    MODELS["profiles"]["qwen3-4b-instruct-2507-q4km-cpu"])
    assert dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "4864Mi"


def test_controller_renders_cache_and_speculation_flags():
    p = dict(MODELS["profiles"]["qwen3-4b-instruct-2507-q4km-cpu"])
    args = templates.llama_args(p)
    assert _arg(args, "--cache-ram") == "256" and _arg(args, "--cache-reuse") == "256" and "--spec-type" not in args
    assert _arg(templates.llama_args({**p, "spec_type": "ngram-simple"}), "--spec-type") == "ngram-simple"
    emb = MODELS["profiles"]["qwen3-embedding-0.6b-q8-cpu"]
    assert _arg(templates.llama_args(emb), "--cache-ram") == "0"
    with pytest.raises(ValueError, match="spec_type"):
        templates.validate_profile({**p, "spec_type": "draft-simple --model-draft /etc/passwd"})
