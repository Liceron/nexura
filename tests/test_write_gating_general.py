"""B23: one write-gating mechanism for every module with external side effects."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import ContractError
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.module_tools import module_tool
from nexura_agents.openapi.module import import_openapi_operations

from nexura_runtime.integrations.channels.module import EmailChannelModule, WhatsAppChannelModule
from nexura_runtime.integrations.http.module import HttpRequestModule
from nexura_runtime.integrations.sql.modules import SqlExecuteModule
from nexura_runtime.service.api.integrations import PETSTORE_FIXTURE_SPEC
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

OPERATOR = ExecutionContext(run_id="r", node_id="n", approval_granted=True)
PLAIN = ExecutionContext(run_id="r", node_id="n")


def _manifest(**kw: Any) -> ModuleManifest:
    return ModuleManifest(
        module_id="x.y", version="1.0.0", kind="integration", title="t", description="d", **kw
    )


def test_manifest_classifies_by_named_key_and_always_write() -> None:
    by_method = _manifest(write_operations=["POST"], write_operation_key="method")
    assert by_method.write_gate_inputs({"method": "POST"})
    assert not by_method.write_gate_inputs({"method": "GET"})
    assert by_method.write_gate_inputs({})  # unknown is gated conservatively
    assert _manifest(always_write=True).write_gate_inputs({})
    assert not _manifest().write_gate_inputs({"operation": "anything"})


async def test_http_post_needs_approval_and_get_does_not() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={})

    mod = HttpRequestModule(transport=httpx.MockTransport(handler))
    cfg = {"allowed_hosts": ["api.example.com"]}
    with pytest.raises(ContractError):
        await mod.invoke(PLAIN, {"method": "POST", "url": "https://api.example.com/x"}, cfg)
    assert seen == []
    await mod.invoke(OPERATOR, {"method": "POST", "url": "https://api.example.com/x"}, cfg)
    await mod.invoke(PLAIN, {"method": "GET", "url": "https://api.example.com/x"}, cfg)
    assert seen == ["POST", "GET"]


async def test_channels_ignore_the_approved_input() -> None:
    email = EmailChannelModule()
    wa = WhatsAppChannelModule()
    for mod, op in ((email, "send_email"), (wa, "send_message")):
        res = await mod.invoke(PLAIN, {"operation": op, "approved": True, "dry_run": True}, {})
        assert res.status == "failure" and res.error_code == "APPROVAL_REQUIRED"
        ok = await mod.invoke(
            OPERATOR,
            {
                "operation": op,
                "approved": False,
                "dry_run": True,
                "is_service_window_active": True,
                "consent_verified": True,
            },
            {},
        )
        assert ok.status == "success", ok.error_message


async def test_sql_execute_needs_approval() -> None:
    with pytest.raises(ContractError):
        await SqlExecuteModule().invoke(
            PLAIN, {"connection_id": "c", "statement": "DELETE FROM t"}, {}
        )


def test_descriptor_fields_agree_for_gated_modules() -> None:
    mods = [HttpRequestModule(), EmailChannelModule(), WhatsAppChannelModule(), SqlExecuteModule()]
    for m in mods:
        mf = m.manifest
        assert mf.effect_policy == "non_idempotent_write"
        assert mf.always_write or mf.write_operations, mf.module_id


async def test_openapi_delete_pauses_and_get_runs() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        return httpx.Response(200, json={})

    mods = import_openapi_operations(
        PETSTORE_FIXTURE_SPEC, prefix="api", transport=httpx.MockTransport(handler)
    )
    delete = next(m for m in mods if m.op_spec.method.upper() == "DELETE")
    get = next(m for m in mods if m.op_spec.method.upper() == "GET")
    assert delete.manifest.always_write and not get.manifest.always_write
    reg = ModuleRegistry()
    reg.register("pet", delete)
    spec = WorkflowSpec(
        name="o",
        entry_node="d",
        nodes={"d": WorkflowNode(id="d", handler="pet", input_mapping={"petId": "pid"})},
    )
    eng = WorkflowEngine(spec, reg, store=MemoryWorkflowStore())
    paused = await eng.run({"pid": "1"})
    assert paused.status == "paused" and sent == []
    assert paused.pending_approval is not None
    done = await eng.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    assert done.status == "completed" and sent == ["DELETE"]


async def test_agent_tool_call_of_a_write_module_requires_approval() -> None:
    tool = module_tool(SqlExecuteModule())
    needs = tool.requires_approval
    assert callable(needs)
    assert needs(None, {"connection_id": "c", "statement": "DELETE FROM t"})  # type: ignore[arg-type]


def test_mcp_calculator_is_pure() -> None:
    from nexura_runtime.integrations.mcp_defaults import register_default_mcp_tools

    class Reg:
        def register(self, *a: Any, **k: Any) -> None:
            self.mod = a[0] if a else None

    reg = Reg()
    register_default_mcp_tools(reg)
    mod = reg.mod
    assert mod.manifest.effect_policy == "pure"


async def test_parallel_write_approval_lists_its_branches() -> None:
    """One approval covers every branch of a parallel step, so the approver is shown them."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        return httpx.Response(200, json={})

    mods = import_openapi_operations(
        PETSTORE_FIXTURE_SPEC, prefix="api", transport=httpx.MockTransport(handler)
    )
    delete = next(m for m in mods if m.op_spec.method.upper() == "DELETE")
    get = next(m for m in mods if m.op_spec.method.upper() == "GET")
    reg = ModuleRegistry()
    reg.register("del", delete)
    reg.register("read", get)
    spec = WorkflowSpec(
        name="p",
        entry_node="both",
        nodes={
            "both": WorkflowNode(
                id="both",
                handler="",
                parallel=["del", "read"],
                reducers={"status_code": "append", "body": "append", "headers": "append"},
            )
        },
    )
    eng = WorkflowEngine(spec, reg, store=MemoryWorkflowStore())
    paused = await eng.run({"petId": "1"})
    assert paused.status == "paused" and sent == []
    assert paused.pending_approval is not None
    details = paused.pending_approval.details
    assert details is not None and details["parallel"] is True
    assert details["branch_count"] == 2 and details["write_branch_count"] == 1
    assert {b["handler"]: b["can_write"] for b in details["branches"]} == {
        "del": True,
        "read": False,
    }
    assert details["inputs_digest"]
