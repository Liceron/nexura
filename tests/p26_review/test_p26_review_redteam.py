"""P26 review: red-team with ``agent_id`` drives the agent's configured (paid) model.

docs/evals.md: the Studio red-team uses the offline fake unless the operator sets
``app.state.redteam_model``; "Clients cannot choose the model", and the CLI refuses a live
model without ``--allow-live-model`` "because the pack sends many requests and live models cost
money". With ``agent_id``, the editor-chosen spec model is used instead. Offline: the
"configured" model is a local counting FakeModel registered as a non-fake chat model.
"""

from __future__ import annotations

from typing import Any

from nexura_agents import FakeModel
from p26_review_helpers import API


def test_redteam_of_saved_agent_does_not_call_a_live_model(review_env: dict[str, Any]) -> None:
    calls = {"n": 0}

    def paid(messages: list[Any], tools: list[dict[str, Any]] | None) -> str:
        calls["n"] += 1
        return "I can't help with that."

    c = review_env["make"]({"paid": FakeModel(handler=paid)})
    h = review_env["headers"]["editor"]
    aid = c.post(f"{API}/agents", headers=h, json={"name": "a", "model": "paid"}).json()["agent_id"]
    # No operator redteam_model is set: the red-team must stay offline (or refuse).
    res = c.post(f"{API}/evaluations/studio/redteam", headers=h, json={"agent_id": aid})
    assert calls["n"] == 0, f"red-team made {calls['n']} calls to the configured model"
    assert res.status_code != 200 or res.json()["model"]["offline"] is True, res.json()["model"]
