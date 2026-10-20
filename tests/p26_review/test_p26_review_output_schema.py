"""P26 review: output_schema abuse against the offline fixture (``schema_example``).

A tiny, valid ``output_schema`` makes ``offline_agent_model`` build a huge value on every
build (chat, eval, workflow run), synchronously on the event loop: a self-referencing ``$ref``
fans out k^4 (k=20: ~41 MB in 1.3 s from a 1.5 KB schema; k=40: ~1.3 GB / 47 s from a 3 KB
schema), and ``minLength`` is copied verbatim. Separately, the fixture caps ``minItems`` at 5,
so a valid schema with ``minItems: 7`` fails every offline run, contrary to the docs claim that
the fixture "answers with a minimal value matching the schema".
"""

from __future__ import annotations

import json
from typing import Any

from p26_review_helpers import API, run, workflow

from nexura_runtime.agents_spec import AgentSpec, schema_example


def _fanout(k: int) -> dict[str, Any]:
    node = {
        "type": "object",
        "properties": {f"p{i}": {"$ref": "#/$defs/n"} for i in range(k)},
        "required": [f"p{i}" for i in range(k)],
    }
    return {"$defs": {"n": node}, **node}


def test_recursive_ref_schema_is_refused_or_bounded(review_env: dict[str, Any]) -> None:
    c = review_env["make"]({})
    schema = _fanout(20)
    assert len(json.dumps(schema)) < 2_000
    res = c.post(
        f"{API}/agents",
        headers=review_env["headers"]["editor"],
        json={"name": "fan", "output_schema": schema},
    )
    if res.status_code == 422:
        return
    spec = AgentSpec.model_validate(res.json()["spec"])
    assert spec.output_schema is not None
    size = len(json.dumps(schema_example(spec.output_schema)))
    assert size <= 64_000, f"offline fixture answer is {size} characters"


def test_huge_min_length_is_refused_or_bounded(review_env: dict[str, Any]) -> None:
    c = review_env["make"]({})
    h = review_env["headers"]["editor"]
    res = c.post(
        f"{API}/agents",
        headers=h,
        json={"name": "big", "output_schema": {"type": "string", "minLength": 10_000_000}},
    )
    if res.status_code == 422:
        return
    session = c.post(f"{API}/sessions", headers=h, json={}).json()["session_id"]
    chat = c.post(
        f"{API}/sessions/{session}/chat",
        headers=h,
        json={"message": "x", "agent_id": res.json()["agent_id"]},
    )
    # A 60-byte schema; the same agent in a workflow run blocks for ~25 s (2M chars: ~5 s).
    assert len(chat.content) <= 1_000_000, (
        f"one offline chat turn returned {len(chat.content)} bytes"
    )


def test_offline_fixture_matches_min_items_above_five(review_env: dict[str, Any]) -> None:
    c = review_env["make"]({})
    h = review_env["headers"]["admin"]
    schema = {"type": "array", "items": {"type": "string"}, "minItems": 7}
    res = c.post(f"{API}/agents", headers=h, json={"name": "seven", "output_schema": schema})
    assert res.status_code == 201, res.text  # the schema is accepted at save
    out = run(c, h, workflow(c, h, {"agent_id": res.json()["agent_id"]}), {"input": "x"})
    assert out["status"] == "completed", out.get("error")
    assert len(out["current_state"]["output"]) == 7
