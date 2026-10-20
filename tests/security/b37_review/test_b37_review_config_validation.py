"""B37 review regression (promoted from .autopilot/b37/repros).

B37 review repro: operator-only node config passes validation, then fails every run.

``SandboxExecutionModule.invoke`` refuses node config keys such as ``runsc_path``,
``backend`` or ``timeout_seconds`` with ``INVALID_CONFIG``. Its manifest ``config_schema`` still
accepts any key, so ``WorkflowSpec.validate`` and ``POST /api/v1/workflows/validate`` report
the workflow valid. The run then pauses for a human approval and fails only after the human
approves. Validation should reject the node up front.

Offline: ``MockDisposableWorker`` only; nothing is executed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nexura_agents.errors import WorkflowValidationError

from nexura_runtime.integrations.sandbox import MockDisposableWorker, SandboxExecutionModule
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

MODULE_ID = "nexura.tools.sandbox.execute"


@pytest.mark.parametrize(
    "config",
    [
        {"runsc_path": "/tmp/evil-runsc"},
        {"backend": "trusted_subprocess"},
        {"timeout_seconds": 300},
    ],
)
def test_sdk_validation_rejects_operator_only_node_config(config: dict[str, object]) -> None:
    reg = ModuleRegistry()
    reg.register("sandbox", SandboxExecutionModule(worker=MockDisposableWorker()))
    spec = WorkflowSpec(
        name="b37-review",
        entry_node="run",
        nodes={
            "run": WorkflowNode(
                id="run", handler="sandbox", config=config, input_mapping={"code": "src"}
            )
        },
    )
    with pytest.raises(WorkflowValidationError):
        spec.validate(reg)


async def test_api_validate_reports_operator_only_node_config_invalid(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.studio.builtin_modules import get_default_registry
    from nexura_runtime.studio.store import StudioStore

    db_file = tmp_path / "b37-review.sqlite3"
    app = create_api_app(
        registry=get_default_registry(),
        store=StudioStore(db_file),
        db_path=db_file,
        profile="local-owner",
    )
    node = {
        "id": "run",
        "handler": MODULE_ID,
        "module_id": MODULE_ID,
        "module_version": "1.0.0",
        "config": {"runsc_path": "/tmp/evil-runsc", "rootfs_path": "/tmp/rootfs"},
        "input_mapping": {"code": "code"},
        "output_mapping": {},
        "transitions": {},
        "default_transition": None,
        "requires_approval": False,
    }
    spec = {"name": "SavedSandbox", "entry_node": "run", "spec_version": 1, "nodes": {"run": node}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/workflows/validate", json={"spec": spec})
    assert res.status_code == 200
    assert res.json()["valid"] is False, "operator-only sandbox config validated as valid"
