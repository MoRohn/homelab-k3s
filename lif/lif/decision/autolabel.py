"""Automatic outcome labels: measurable answers instead of a human review queue.

Shadow observation (`POST /de/observe`) used to send every disagreement, plus a sample of agreements, to a
person, only to collect the "correct answer" for calibration. Nothing waited on those reviews. Where a
decision's criteria can be checked in code, a labeller here computes that answer from the same state and
records it as the outcome (`source="auto:<labeller>"`). The labeller is versioned, deterministic and
measured against the human answers it replaces (tests/test_autolabel.py).

A decision without a labeller gets no label (it is left out of calibration); it never falls back to a
person. Critical or irreversible decisions keep their human/advisory route in the cascade: they are never
automated, and nothing here touches them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from lif.models.discovery import _TOY, CATEGORIES


# Not "an official release or a direct quantization of one": distills of other models, merges, roleplay,
# uncensored/abliterated variants, fine-tunes, tests and adapters (model-advance/v1 criteria).
_VARIANT = re.compile(r"(?i)(distill|claude|opus|gemini|gpt-?\d|(?:^|[/_-])rp(?:$|[_-])|roleplay|storytell|uncensor|"
                      r"abliterat|merge|fine-?tune|-reap\b|tiny-random|\btest\b|dummy|debug|lora\b|adapter)")


def _base(model_id: str) -> str:
    """'unsloth/Qwen3-4B-Instruct-2507-GGUF' → 'qwen3-4b-instruct-2507' (repo name without format suffix)."""
    name = model_id.split("/")[-1].lower()
    return re.sub(r"[-_](gguf|q\d.*|i\d-gguf|i\d)$", "", name)


@dataclass(frozen=True)
class Label:
    answer: str
    labeller: str            # e.g. "model-advance-rules-v1": the source recorded with the outcome
    why: str                 # the measurable reason, for the audit trail


def _get(state: Any, path: str) -> Any:
    cur = state
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
    return cur


def model_advance_v1(state: Any) -> Label | None:
    """model-advance/v1's criteria, checked in code:
    1. the model is built for the category's task (pipeline tag, and name for coding/embedding/…);
    2. it is not a toy, test, merge, adapter or uncensored/abliterated variant;
    3. it is newer or clearly larger than the production model, or there is none.
    All three → "yes" (worth downloading and benchmarking), otherwise "no"."""
    L = "model-advance-rules-v1"
    mid, cat = str(_get(state, "model.id") or ""), str(_get(state, "category.name") or "")
    spec = CATEGORIES.get(cat)
    if not mid or spec is None:
        return None                                   # not enough to judge: no label
    tag = _get(state, "model.pipeline_tag")
    if _TOY.search(mid) or _VARIANT.search(mid):
        return Label("no", L, "not an official release: distill, merge, roleplay, uncensored, fine-tune or test")
    name_ok = not spec.get("name_re") or re.search(spec["name_re"], mid) or tag in spec.get("name_re_bypass_tags", ())
    if not name_ok or tag not in spec["tasks"]:
        return Label("no", L, f"not built for {cat} (pipeline tag {tag!r})")
    current = str(_get(state, "current.model_id") or "none")
    if current != "none" and _base(mid) == _base(current):
        return Label("no", L, "the same model as production")
    better = current == "none" or bool(_get(state, "comparison.newer_than_current")) \
        or bool(_get(state, "comparison.larger_than_current"))
    if not better:
        return Label("no", L, "neither newer nor larger than the production model")
    return Label("yes", L, "fits the category, is a real release, and is newer or larger than production")


LABELLERS: dict[str, Callable[[Any], Label | None]] = {
    "model-advance": model_advance_v1,
}


def label(decision_name: str, state: Any) -> Label | None:
    fn = LABELLERS.get(decision_name)
    return fn(state) if fn else None
