"""Automatic outcome labels (lif/decision/autolabel.py) replace the human review queue, so they must be
measurably right: each labeller is scored against its decision package's designed test cases."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lif.decision import autolabel

PKG = Path(__file__).resolve().parents[1] / "decision-packages/model-operations/model-advance/tests.jsonl"
CASES = [json.loads(line) for line in PKG.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("case", CASES, ids=[f"{c['slice']}:{c['note'][:40]}" for c in CASES])
def test_model_advance_labeller_matches_every_designed_case(case):
    lab = autolabel.label("model-advance", case["state"])
    assert lab is not None and lab.answer == case["expected"], (lab, case["note"])
    assert lab.labeller == "model-advance-rules-v1" and lab.why


@pytest.mark.parametrize("model_id,expected", [
    ("Qwen/Qwen3-VL-8B-Instruct-GGUF", "yes"),                                       # official release
    ("unsloth/Qwen3.5-4B-GGUF", "yes"),                                              # direct quantization
    ("Jackrong/Qwen3.5-9B-Gemini-3.1-Pro-Reasoning-Distill-GGUF", "no"),             # distill of another model
    ("TeichAI/Qwen3-14B-Claude-4.5-Opus-High-Reasoning-Distill-GGUF", "no"),
    ("brunopio/Qwen3.5-14B-A3B-Claude-4.6-Opus-Reasoning-Distilled-reap-Q4_K_M-GGUF", "no"),
])
def test_real_discovery_names(model_id, expected):
    cat = "vision" if "VL" in model_id or model_id.startswith("unsloth/Qwen3.5-4B") else "reasoning"
    state = {"model": {"id": model_id, "pipeline_tag": "image-text-to-text" if cat == "vision" else "text-generation"},
             "category": {"name": cat}, "current": {"model_id": "none"},
             "comparison": {"newer_than_current": True, "larger_than_current": False}}
    assert autolabel.label("model-advance", state).answer == expected


def test_same_model_as_production_and_unknown_decisions():
    state = {"model": {"id": "unsloth/Qwen3-4B-Instruct-2507-GGUF", "pipeline_tag": "text-generation"},
             "category": {"name": "general"}, "current": {"model_id": "unsloth/Qwen3-4B-Instruct-2507"},
             "comparison": {"newer_than_current": True, "larger_than_current": False}}
    assert autolabel.label("model-advance", state).answer == "no"
    assert autolabel.label("model-advance", {"model": {"id": "x/y"}, "category": {"name": "nope"}}) is None
    assert autolabel.label("no-such-decision", {}) is None          # no labeller → no label, never a person
