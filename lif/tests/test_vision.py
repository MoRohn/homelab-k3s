"""Local vision models: discovery (mmproj gate), sizing, serving args, download, evaluation.
No network, no cluster, no weights."""
from __future__ import annotations

import base64
import os
import shutil
import struct
import subprocess
import time
import zlib

import pytest

from lif.controller import templates
from lif.decision.dag import DagRuntime
from lif.decision.fabric import DecisionFabric
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.models import evaluator, hardware_fit, hf
from lif.models import discovery as disc
from lif.models.registry import Registry

SHA = "a" * 40
H64 = lambda c: c * 64          # noqa: E731


def _sib(name, size=None, sha=None):
    s = {"rfilename": name}
    if size:
        s["size"] = size
    if sha:
        s["lfs"] = {"sha256": sha, "size": size}
    return s


def _vl_info(mid="Qwen/Qwen3-VL-4B-Instruct-GGUF", tag="image-text-to-text", mmproj=True, downloads=200_000):
    sibs = [_sib("Qwen3VL-4B-Instruct-Q4_K_M.gguf", 2_497_000_000, H64("1")),
            _sib("Qwen3VL-4B-Instruct-Q8_0.gguf", 4_280_000_000, H64("2"))]
    if mmproj:
        sibs += [_sib("mmproj-Qwen3VL-4B-Instruct-F16.gguf", 836_000_000, H64("3")),
                 _sib("mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", 453_000_000, H64("4"))]
    return {"id": mid, "sha": SHA, "pipeline_tag": tag, "downloads": downloads, "likes": 10,
            "tags": ["gguf", "license:apache-2.0"], "cardData": {"license": "apache-2.0"},
            "gguf": {"total": 4_020_000_000, "architecture": "qwen3vl"}, "siblings": sibs,
            "lastModified": "2026-09-01T00:00:00Z"}


# ── hf: projector picking ────────────────────────────────────────────────────

def test_mmproj_kept_out_of_weight_picker():
    files = hf.gguf_files(_vl_info())
    assert {f["file"] for f in files} == {"Qwen3VL-4B-Instruct-Q4_K_M.gguf", "Qwen3VL-4B-Instruct-Q8_0.gguf"}
    assert hf.pick_gguf(files)["quant"] == "Q4_K_M"


@pytest.mark.parametrize("names,want", [
    (["mmproj-F32.gguf", "mmproj-F16.gguf", "mmproj-BF16.gguf", "mmproj-M-Q8_0.gguf"], "mmproj-M-Q8_0.gguf"),
    (["mmproj-F32.gguf", "mmproj-BF16.gguf", "mmproj-F16.gguf"], "mmproj-F16.gguf"),
    (["mmproj-F32.gguf", "mmproj-BF16.gguf"], "mmproj-BF16.gguf"),
    (["Model.mmproj-Q8_0.gguf"], "Model.mmproj-Q8_0.gguf"),
    (["mmproj-F32.gguf"], None),                     # never F32
    (["Model-mmproj.gguf"], None),                   # precision unknown → honest reject, never a guess
    (["mmproj-model-f16.gguf"], "mmproj-model-f16.gguf"),
])
def test_pick_mmproj_preference(names, want):
    info = {"siblings": [_sib(n, 100, H64("b")) for n in names]}
    got = hf.pick_mmproj(hf.mmproj_files(info))
    assert (got or {}).get("file") == want


def test_pick_mmproj_requires_sha_and_size():
    info = {"siblings": [_sib("mmproj-Q8_0.gguf", 100, None), _sib("mmproj-F16.gguf", None, H64("c")),
                         _sib("mmproj-BF16.gguf", 100, H64("d"))]}
    assert hf.pick_mmproj(hf.mmproj_files(info))["file"] == "mmproj-BF16.gguf"


def test_normalize_attaches_mmproj_only_for_vision():
    v = hf.normalize(_vl_info(), "vision")
    assert v["gguf_pick"]["mmproj"] == {"file": "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", "size": 453_000_000,
                                        "sha256": H64("4"), "quant": "Q8_0"}
    assert v["mmproj_listed"] is True
    assert "mmproj" not in hf.normalize(_vl_info(), "general")["gguf_pick"]


def test_params_ignore_clip_summary_and_moe_active_count():
    info = {"id": "org/Some-VL-7B-GGUF", "gguf": {"total": 600_000_000, "architecture": "clip"}}
    assert hf.params_b(info) == 7.0
    assert hf.normalize({**info, "sha": SHA}, "vision")["architecture"] is None
    assert hf.params_b({"id": "org/Coder-35B-A3B-GGUF"}) == 35.0


# ── discovery: the vision category ───────────────────────────────────────────

def test_vision_category_is_registered():
    assert "vision" in disc.CATEGORIES and "vision" in disc.WEIGHTS
    assert abs(sum(disc.WEIGHTS["vision"].values()) - 1.0) < 1e-9


def test_vision_filter_gates():
    f = lambda info, stage="full": disc.metadata_filter(hf.normalize(info, "vision"), "vision", set(), stage)  # noqa
    assert f(_vl_info()) is None
    assert f(_vl_info(mmproj=False)) == "no image projector"
    assert f(_vl_info(mmproj=False), "listing") == "no image projector"      # siblings in the listing
    listing = {k: v for k, v in _vl_info(mmproj=False).items() if k != "siblings"}
    assert f(listing, "listing") is None                                    # unknown → detail fetch decides
    # ggml-org repos often have no pipeline_tag: the name must say VL/vision/omni
    assert f(_vl_info("ggml-org/Qwen2.5-VL-3B-Instruct-GGUF", tag=None)) is None
    assert f(_vl_info("ggml-org/Some-Chat-3B-GGUF", tag=None)) == "name does not match category"
    # …but a repo tagged image-text-to-text passes on the tag alone
    assert f(_vl_info("org/SmolThing-3B-GGUF")) is None
    assert f(_vl_info("org/Big-VL-32B-GGUF") | {"gguf": {"total": 32e9}}).startswith("32.0B outside")
    assert f(_vl_info(tag="text-to-image")).startswith("pipeline_tag")
    # projector with no sha256 is not pinned → rejected
    info = _vl_info(mmproj=False)
    info["siblings"].append(_sib("mmproj-Q8_0.gguf", 400_000_000, None))
    assert f(info) == "no image projector"
    assert disc._bucket("no image projector") == "no image projector"


def test_base_key_merges_bartowski_repacks():
    assert disc._base_key("bartowski/Qwen_Qwen3.5-4B-GGUF") == disc._base_key("unsloth/Qwen3.5-4B-GGUF")


class FakeHF:
    def __init__(self, infos):
        self.infos = {i["id"]: i for i in infos}

    async def search(self, **kw):
        return [{k: v for k, v in i.items()} for i in self.infos.values()]

    async def model_info(self, mid, revision=None):
        return self.infos[mid]


async def test_vision_discovery_end_to_end_rules_only(tmp_path):
    fab = DecisionFabric(load_definitions(), rules, jev=None)
    rt = DagRuntime(fab)
    rt.load()
    reg = Registry(str(tmp_path / "r.db"))
    infos = [_vl_info(), _vl_info("Qwen/Qwen3-VL-2B-NoProj-GGUF", mmproj=False)]
    out = await disc.Discovery(reg, rt, hfc=FakeHF(infos)).run(["vision"], actor="test")
    f = out["funnel"]["categories"]["vision"]
    assert out["status"] == "succeeded", out
    assert f["shortlisted"] == ["Qwen/Qwen3-VL-4B-Instruct-GGUF"]
    assert f["rejected"] == {"no image projector": 1}
    assert f["providers"] == {"rules": 1}                    # nothing went to Jev
    (m,) = reg.list(["CANDIDATE"])
    p = m["profile"]
    assert m["category"] == "vision" and m["id"] == "qwen3-vl-4b-instruct-q4km-cpu"
    assert p["mmproj"] == {"file": "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", "sha256": H64("4"), "size": 453_000_000}
    assert m["fit"]["mmproj_mib"] == 432 and p["memory_budget_mb"] >= m["fit"]["weights_mib"] + 432
    templates.validate_profile(p)
    assert "--mmproj" in templates.llama_args(p)


# ── hardware fit ─────────────────────────────────────────────────────────────

def test_hardware_fit_counts_projector_and_image_headroom():
    meta = hf.normalize(_vl_info(), "vision")
    meta.update(num_layers=36, num_kv_heads=8, head_dim=128)
    v = hardware_fit.estimate(meta, device="cpu")
    t = hardware_fit.estimate({**meta, "gguf_pick": {k: x for k, x in meta["gguf_pick"].items() if k != "mmproj"}},
                              device="cpu")
    assert v.verdict == "fits_cpu" and v.mmproj_mib == 432
    assert v.image_headroom_mib == hardware_fit.VISION_IMAGE_HEADROOM_MIB
    assert v.total_mib == t.total_mib + 432 + v.image_headroom_mib
    assert v.anon_mib == t.anon_mib + 432 + v.image_headroom_mib       # drives the headroom gates
    assert t.mmproj_mib == 0 and t.image_headroom_mib == 0
    huge = {**meta, "gguf_pick": {**meta["gguf_pick"], "mmproj": {"size": 3 << 30}}}
    r = hardware_fit.estimate(huge, device="cpu")
    assert r.verdict == "no_fit" and "vision budget" in r.reasons[0]


# ── profile, templates, download ─────────────────────────────────────────────

def _vprof(**kw):
    p = {"category": "vision", "hf_repo": "Qwen/Qwen3-VL-4B-Instruct-GGUF", "revision": SHA,
         "file": "Qwen3VL-4B-Instruct-Q4_K_M.gguf", "sha256": H64("1"), "context": 4096,
         "mmproj": {"file": "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", "sha256": H64("4"), "size": 453_000_000}}
    p.update(kw)
    return p


@pytest.mark.parametrize("bad", ["../etc/passwd.gguf", "x/../../y.gguf", "mmproj.bin", "-rf.gguf", "a b.gguf", ""])
def test_validate_profile_rejects_unsafe_mmproj(bad):
    with pytest.raises(ValueError):
        templates.validate_profile(_vprof(mmproj={"file": bad, "sha256": H64("4")}))


def test_validate_profile_mmproj_sha_and_identity():
    with pytest.raises(ValueError):
        templates.validate_profile(_vprof(mmproj={"file": "mmproj-Q8_0.gguf", "sha256": "nope"}))
    with pytest.raises(ValueError):
        templates.validate_profile(_vprof(mmproj={"file": "Qwen3VL-4B-Instruct-Q4_K_M.gguf", "sha256": H64("4")}))
    with pytest.raises(ValueError):
        templates.validate_profile(_vprof(mmproj="mmproj-Q8_0.gguf"))


def test_llama_args_vision():
    a = templates.llama_args(_vprof())
    i = a.index("--mmproj")
    assert a[i + 1] == f"/models/Qwen/Qwen3-VL-4B-Instruct-GGUF/{SHA}/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"
    assert "--no-mmproj-auto" in a and "--jinja" in a
    assert a[a.index("--ctx-size") + 1] == "8192"                         # raised from 4096 for images
    assert templates.llama_args(_vprof(context=16384))[a.index("--ctx-size") + 1] == "16384"
    text = templates.llama_args(_vprof(category="general", mmproj=None, context=4096))
    assert "--mmproj" not in text and text[text.index("--ctx-size") + 1] == "4096"


def test_download_job_fetches_every_file():
    job = templates.download_job("dl-x", _vprof())
    env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["N"] == "2" and env["FILE_0"] == "Qwen3VL-4B-Instruct-Q4_K_M.gguf" and env["SHA_1"] == H64("4")
    with pytest.raises(ValueError, match="sha256"):
        templates.download_job("dl-x", _vprof(mmproj={"file": "mmproj-Q8_0.gguf"}))
    one = templates.download_job("dl-y", _vprof(mmproj=None))
    assert {e["name"] for e in one["spec"]["template"]["spec"]["containers"][0]["env"]} == \
        {"REPO", "REV", "N", "FILE_0", "SHA_0"}


@pytest.mark.skipif(not shutil.which("busybox"), reason="busybox (the curl image's sha256sum) not installed")
def test_download_script_resumable_verified_atomic(tmp_path):
    """Run the real Job script with a stub curl: cached files skip, good files land atomically,
    a bad second file is quarantined and the Job fails with MISMATCH (→ QUARANTINED)."""
    import hashlib
    src, root, bin_ = tmp_path / "src", tmp_path / "models", tmp_path / "bin"
    src.mkdir(), root.mkdir(), bin_.mkdir()
    (src / "w.gguf").write_bytes(b"weights")
    (src / "mmproj-Q8_0.gguf").write_bytes(b"projector")
    (bin_ / "sha256sum").symlink_to(shutil.which("busybox"))       # busybox applet (supports -s)
    curl = bin_ / "curl"
    curl.write_text('#!/bin/sh\nout=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; '
                    'url="$1"; shift; done; echo "$url" >> "$LOG"; cp "$SRC/${url##*/}" "$out"\n')
    curl.chmod(0o755)
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    p = _vprof(file="w.gguf", sha256=sha(b"weights"), mmproj={"file": "mmproj-Q8_0.gguf", "sha256": sha(b"projector")})

    def run(prof):
        env = {e["name"]: e["value"] for e in templates.download_env(prof)}
        env.update(PATH=f"{bin_}:{os.environ['PATH']}", MODELS_ROOT=str(root), SRC=str(src), LOG=str(tmp_path / "log"))
        return subprocess.run(["/bin/sh", "-c", templates.DOWNLOAD_SCRIPT], env=env, capture_output=True, text=True)

    d = root / p["hf_repo"] / SHA
    r = run(p)
    assert r.returncode == 0, r.stderr
    assert (d / "w.gguf").read_bytes() == b"weights" and (d / "mmproj-Q8_0.gguf").read_bytes() == b"projector"
    assert not list(d.glob("*.part"))
    r = run(p)                                                     # second run: both cached, no fetch
    assert r.returncode == 0 and r.stdout.count("ok (cached)") == 2
    assert (tmp_path / "log").read_text().count("\n") == 2
    (d / "mmproj-Q8_0.gguf").unlink()
    r = run({**p, "mmproj": {"file": "mmproj-Q8_0.gguf", "sha256": sha(b"other")}})
    assert r.returncode == 3 and "MISMATCH" in r.stdout
    assert (d / "mmproj-Q8_0.gguf.bad").exists() and not (d / "mmproj-Q8_0.gguf").exists()


async def test_lifecycle_gc_and_download_cover_mmproj(tmp_path):
    from lif.controller.lifecycle import Lifecycle, OpError, suite_for
    assert (suite_for("vision"), suite_for("general"), suite_for("embedding")) == ("vision", "core", "embedding")

    class K8s:
        enabled = True
        jobs = []

        async def create_job(self, ns, body):
            self.jobs.append(body)

    reg = Registry(str(tmp_path / "r.db"))
    reg.upsert("v", "Qwen/Qwen3-VL-4B-Instruct-GGUF", SHA, "vision", "CANDIDATE",
               profile=_vprof(mmproj={"file": "mmproj-Q8_0.gguf", "sha256": None}))
    life = Lifecycle(reg, K8s(), None, None)
    with pytest.raises(OpError, match="image projector"):
        life.download("v", "operator")
    reg.upsert("v", "Qwen/Qwen3-VL-4B-Instruct-GGUF", SHA, "vision", "REJECTED", profile=_vprof())
    reg.db.x("UPDATE models SET updated=? WHERE id='v'", (time.time() - 8 * 86400,))
    out = await life.gc("test")
    assert out["removed"] == 1
    cmd = K8s.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"]
    base = f"/models/Qwen/Qwen3-VL-4B-Instruct-GGUF/{SHA}/"
    assert base + "Qwen3VL-4B-Instruct-Q4_K_M.gguf" in cmd and base + "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf" in cmd


# ── evaluator: stdlib PNGs and the vision suite ──────────────────────────────

def _decode_png(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    i, w, h = 8, None, None
    idat = b""
    while i < len(data):
        (n,) = struct.unpack(">I", data[i:i + 4])
        kind, body = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        (crc,) = struct.unpack(">I", data[i + 8 + n:i + 12 + n])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert (depth, ctype) == (8, 2)
        elif kind == b"IDAT":
            idat += body
        i += 12 + n
    raw = zlib.decompress(idat)
    assert len(raw) == h * (1 + 3 * w)
    return w, h, raw


def _pixel(w, raw, x, y):
    o = y * (1 + 3 * w) + 1 + 3 * x
    return tuple(raw[o:o + 3])


def test_png_encoder_and_images():
    w, h, raw = _decode_png(evaluator.render_image({"kind": "solid", "color": "red"}))
    assert (w, h) == (256, 256) and _pixel(w, raw, 10, 200) == (220, 20, 20)
    w, h, raw = _decode_png(evaluator.render_image({"kind": "split", "left": "yellow", "right": "green"}))
    assert _pixel(w, raw, 5, 5) == (240, 210, 20) and _pixel(w, raw, 250, 5) == (20, 170, 40)
    w, h, raw = _decode_png(evaluator.render_image({"kind": "digit", "digit": 1, "size": 288}))
    ink = sum(_pixel(w, raw, x, y) == (0, 0, 0) for y in range(0, h, 4) for x in range(0, w, 4))
    assert 0.05 < ink / ((w // 4) * (h // 4)) < 0.4                     # a glyph, not blank, not solid
    w, h, raw = _decode_png(evaluator.render_image({"kind": "squares", "count": 3, "color": "blue", "size": 384}))
    row = [_pixel(w, raw, x, h // 2) == (25, 60, 220) for x in range(w)]
    assert sum(1 for a, b in zip([False] + row, row) if b and not a) == 3   # three separate squares


def test_vision_suite_loads_and_renders():
    s = evaluator.load_suite("vision")
    assert s["suite"] == "vision" and 4 <= len(s["items"]) <= 6
    for it in s["items"]:
        c = evaluator.item_content(it)
        assert c[0]["type"] == "image_url" and c[-1] == {"type": "text", "text": it["prompt"]}
        url = c[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        _decode_png(base64.b64decode(url.split(",", 1)[1]))
    assert evaluator.item_content({"prompt": "hi"}) == "hi"             # text suites unchanged
    by = {i["id"]: i for i in s["items"]}
    assert evaluator.check(by["vis-color-01"], "Red.")[0]
    assert not evaluator.check(by["vis-color-01"], "Blue")[0]
    assert evaluator.check(by["vis-digit-01"], "7")[0] and evaluator.check(by["vis-count-01"], "There are 3.")[0]


async def test_vision_run_suite_sends_images(monkeypatch):
    sent = []

    async def fake_one(client, url, model, prompt, max_tokens, extra):
        sent.append(prompt)
        it = next(i for i in evaluator.load_suite("vision")["items"] if evaluator.item_content(i) == prompt)
        return {"text": str(it["expect"]), "ttft": 0.1, "total": 0.5, "decode_tps": 20.0,
                "usage": {"completion_tokens": 2}}
    monkeypatch.setattr(evaluator, "_one", fake_one)
    results, summary = await evaluator.run_suite("http://x", suite="vision", concurrency=2)
    assert summary["quality"] == 1.0 and summary["structured_ok"] is None and summary["json_valid"] is None
    assert all(isinstance(p, list) and p[0]["type"] == "image_url" for p in sent)     # load pass too


def test_compare_without_incumbent_and_without_structured():
    good = {"quality": 0.83, "errors": 0, "structured_ok": None, "ttft_ms_p50": 900, "decode_tps_p50": 20,
            "throughput_tps": 30}
    assert evaluator.compare(good, None, 4000, None)["recommendation"] == "CANARY"
    bad = {**good, "quality": 0.17}
    r = evaluator.compare(bad, None, 4000, None)
    assert r["recommendation"] == "REJECT" and r["failed_checks"] == ["quality_floor"]
    # incumbent exists, neither has structured items: no structured check, no crash
    r = evaluator.compare({**good, "quality": 0.9}, good, 4000, 4000)
    assert "structured" not in r["checks"] and r["recommendation"] == "CANARY"
    # text suites keep their structured gate
    txt = {**good, "structured_ok": 0.5}
    assert "structured" in evaluator.compare(txt, None, 1, None)["failed_checks"]
