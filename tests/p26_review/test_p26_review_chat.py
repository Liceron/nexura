"""P26 review: a saved agent that keeps asking for a refused write tool in chat.

Each refusal resumes the run; when ``limits.max_turns`` is reached the playground answers
502 ``MODEL_PROVIDER_ERROR`` ("The model provider returned an error."), blaming the provider
for the agent's own turn limit and dropping ``refused_tool_calls``. Offline: scripted FakeModel.
"""

from __future__ import annotations

from typing import Any

from nexura_agents import FakeModel
from nexura_agents.types import ToolCall
from p26_review_helpers import API, WRITE_ID, WRITE_TOOL


def test_turn_limit_after_refusals_is_not_reported_as_provider_error(
    review_env: dict[str, Any],
) -> None:
    n = {"calls": 0}

    def insistent(messages: list[Any], tools: list[dict[str, Any]] | None) -> Any:
        n["calls"] += 1
        return [ToolCall(id=f"c{n['calls']}", name=WRITE_TOOL, arguments={"title": "x"})]

    c = review_env["make"]({"scripted": FakeModel(handler=insistent)})
    h = review_env["headers"]["admin"]
    aid = c.post(
        f"{API}/agents",
        headers=h,
        json={"name": "w", "model": "scripted", "tools": [WRITE_ID], "limits": {"max_turns": 2}},
    ).json()["agent_id"]
    session = c.post(f"{API}/sessions", headers=h, json={}).json()["session_id"]
    res = c.post(
        f"{API}/sessions/{session}/chat", headers=h, json={"message": "go", "agent_id": aid}
    )
    assert review_env["ticket"].created == []
    detail = res.json().get("detail") if res.status_code >= 400 else None
    assert res.status_code < 500 and (
        detail is None or detail.get("code") != "MODEL_PROVIDER_ERROR"
    ), (
        res.status_code,
        res.text[:200],
    )
