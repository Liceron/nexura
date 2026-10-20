"""B37 review regression (promoted from .autopilot/b37/repros).

B37 review repro: the AI workflow builder still offers the internal sandbox module.

B37 flags ``nexura.tools.sandbox.execute`` ``internal`` so builders do not offer it as a new
node until the P27 stage 2 sandbox exists. Studio's palette and composite pickers honour the
flag, but the chat-to-build assistant (``POST /api/v1/builder/draft``) lists every
registered module in the catalog it sends to the model, internal ones included.

Offline: the model is ``FakeModel`` (a scripted local fake); nothing is executed.
"""

from __future__ import annotations

import contextlib
import json

from nexura_agents.models.fake import FakeModel

from nexura_runtime.builder_assistant import DraftError, draft_from_description
from nexura_runtime.studio.builtin_modules import get_default_registry

MODULE_ID = "nexura.tools.sandbox.execute"


async def test_builder_catalog_does_not_offer_the_internal_sandbox_module() -> None:
    reply = json.dumps(
        {
            "explanation": "Runs the user's Python.",
            "spec": {
                "name": "RunPython",
                "entry_node": "run",
                "nodes": [
                    {
                        "id": "run",
                        "handler": MODULE_ID,
                        "module_id": MODULE_ID,
                        "input_mapping": {"code": "code"},
                        "transitions": {},
                        "default_transition": None,
                    }
                ],
                "terminal_nodes": [],
            },
        }
    )
    model = FakeModel([reply], repeat_last=True)
    with contextlib.suppress(DraftError):
        await draft_from_description(
            "run some python code to calculate a sum",
            model=model,
            registry=get_default_registry(),
            max_attempts=1,
        )
    assert model.calls, "the builder did not ask the model"
    system_prompt = model.calls[0]["messages"][0].content or ""
    assert MODULE_ID not in system_prompt, (
        "the builder assistant catalog offers the internal sandbox module to the model"
    )
