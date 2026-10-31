# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline direct component runner with repeatability and mutation checks."""

import asyncio
import copy
from typing import Any

import pytest
from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules import default_registry


@pytest.fixture
def run_component() -> Any:
    def run(module_id: str, **inputs: Any) -> dict[str, Any]:
        module = default_registry.get(module_id, "1.0.0")
        before = copy.deepcopy(inputs)
        context = ExecutionContext(run_id="offline-components", node_id="component")
        first = asyncio.run(module.execute(context, inputs, {}))
        second = asyncio.run(module.execute(context, copy.deepcopy(inputs), {}))
        assert first.status == "success"
        assert first.model_dump() == second.model_dump()
        assert inputs == before
        return first.outputs

    return run
