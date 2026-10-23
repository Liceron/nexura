# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G3: the verdict type, the category taxonomy and classifier label mapping."""

from __future__ import annotations

import pytest
from nexura_guardrails import (
    CATEGORIES,
    LABEL_MAPS,
    Verdict,
    map_label,
    policy_for,
    validate_categories,
)


def test_every_mapped_label_targets_a_known_category() -> None:
    for source, table in LABEL_MAPS.items():
        for label, ids in table.items():
            assert ids, (source, label)
            assert set(ids) <= set(CATEGORIES), (source, label, ids)


def test_llama_guard_hazards_s1_to_s14_are_all_mapped() -> None:
    assert set(LABEL_MAPS["llama_guard"]) == {f"S{i}" for i in range(1, 15)}
    assert map_label("llama_guard", "S1") == ("violence",)
    assert map_label("llama_guard", "s10") == ("hate",)  # case-insensitive
    assert map_label("llama_guard", " S11 ") == ("self_harm",)
    assert map_label("llama_guard", "S2") == ("crime", "fraud")
    assert map_label("llama_guard", "S13") == ("elections",)


@pytest.mark.parametrize(
    ("source", "label", "expected"),
    [
        ("shieldgemma", "dangerous", ("violence", "weapons", "self_harm")),
        ("shieldgemma", "Hate", ("hate",)),
        ("granite_guardian", "social_bias", ("hate",)),
        ("granite_guardian", "function_call", ("function_call_error",)),
        ("granite_guardian", "jailbreak", ("jailbreak",)),
        ("nemotron", "S7", ("child_safety",)),
        ("nemotron", "S16", ("fraud",)),
        ("nemotron", "S19", ("misinformation",)),
        ("nemotron", "S23", ("other",)),
        ("openai_moderation", "self-harm/instructions", ("self_harm",)),
        ("openai_moderation", "sexual/minors", ("child_safety",)),
        ("openai_moderation", "illicit/violent", ("weapons",)),
    ],
)
def test_label_mapping(source: str, label: str, expected: tuple[str, ...]) -> None:
    assert map_label(source, label) == expected


def test_unmapped_labels_go_to_other_and_unknown_sources_fail() -> None:
    assert map_label("llama_guard", "S99") == ("other",)
    assert map_label("nemotron", "S5") == ("other",)
    with pytest.raises(ValueError):
        map_label("unknown_guard", "S1")


def test_general_categories_are_approved() -> None:
    general = [c for c in CATEGORIES.values() if c.region == "global"]
    assert all(c.review_status == "approved" and c.parent is None for c in general)
    assert {"prompt_injection", "jailbreak", "other"} <= {c.id for c in general}


def test_validate_and_policy_for() -> None:
    assert validate_categories(["fraud", "fraud", "other"]) == ("fraud", "other")
    assert validate_categories("fraud") == ("fraud",)
    with pytest.raises(ValueError):
        validate_categories(["nope"])
    text = policy_for(["hate", "fraud"])
    assert "- hate:" in text and "- fraud:" in text and "Allow everything else." in text


def test_verdict_validation_and_info() -> None:
    verdict = Verdict("block", categories=["fraud"], score=0.5, reason="r", check="c")  # type: ignore[arg-type]
    assert verdict.categories == ("fraud",) and verdict.blocks
    info = verdict.info()
    assert set(info) >= {
        "check",
        "backend",
        "categories",
        "score",
        "reason",
        "latency_ms",
        "cached",
    }
    assert not Verdict("allow").blocks and Verdict("error").blocks and Verdict("review").blocks
    with pytest.raises(ValueError):
        Verdict("maybe")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Verdict("allow", score=1.5)
    with pytest.raises(ValueError):
        Verdict("allow", score=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Verdict("rewrite")
    with pytest.raises(ValueError):
        Verdict("allow", replacement="x")
