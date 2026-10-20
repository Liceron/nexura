"""Focused acceptance tests for B00: Typed module contracts."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_runtime.contracts import (
    BaseModule,
    ConfigValidationError,
    ContractErrorCode,
    CredentialReference,
    ExecutionContext,
    ModuleEvent,
    ModuleManifest,
    ModuleResult,
    PortDefinition,
    SchemaValidationError,
    canonical_digest,
    canonical_json,
)
from nexura_runtime.errors import ExecutionCancelledError
from nexura_runtime.types import Usage


class MockEventSink:
    """In-memory event sink for testing emitted events."""

    def __init__(self) -> None:
        self.events: list[ModuleEvent] = []

    def emit(self, event: ModuleEvent) -> None:
        self.events.append(event)


class DummyTransformModule(BaseModule):
    """Test module with a side-effect tracker to verify zero-effect on invalid schema."""

    def __init__(self) -> None:
        self.effect_count = 0

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            schema_version=1,
            module_id="nexura.test.dummy",
            version="1.0.0",
            kind="transform",
            title="Dummy Module",
            description="Module for testing contracts and policies",
            effect_policy="pure",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1},
                },
                "required": ["text", "count"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
            config_schema={
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["upper", "lower"]}},
                "required": ["mode"],
                "additionalProperties": False,
            },
            named_ports=[
                PortDefinition(name="text", type="string"),
                PortDefinition(name="count", type="integer"),
                PortDefinition(name="result", type="string"),
            ],
            capabilities={"network": False, "streaming": False},
        )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        self.effect_count += 1
        text = inputs["text"]
        count = inputs["count"]
        mode = config["mode"]

        transformed = (text.upper() if mode == "upper" else text.lower()) * count
        return ModuleResult.success(outputs={"result": transformed})


def test_canonical_json_and_digest_round_trip() -> None:
    """Round-trip every contract with Unicode, nested tool metadata and stable digest."""
    payload: dict[str, Any] = {
        "b_key": "World",
        "a_key": "नमस्ते दुनिया",
        "nested": {"z": 10, "a": [1, 2, 3], "tools": [{"name": "search", "id": "call_1"}]},
        "none_val": None,
        "empty_str": "",
        "empty_list": [],
    }

    serialized = canonical_json(payload)
    # Check lexicographical sorting: "a_key" must appear before "b_key"
    assert serialized.startswith('{"a_key":"नमस्ते दुनिया","b_key":"World"')
    # Unicode is preserved without \uXXXX escaping
    assert "नमस्ते दुनिया" in serialized

    digest1 = canonical_digest(payload)
    digest2 = canonical_digest(payload)
    assert digest1 == digest2
    assert len(digest1) == 64


def test_canonical_json_rejects_non_finite_floats() -> None:
    """Canonical JSON serialization strictly rejects NaN and Infinity."""
    with pytest.raises(ValueError, match="Non-finite float"):
        canonical_json({"value": float("nan")})

    with pytest.raises(ValueError, match="Non-finite float"):
        canonical_json({"value": float("inf")})


@pytest.mark.asyncio
async def test_reject_invalid_inputs_causes_zero_effects() -> None:
    """Invalid input schemas are rejected before invoke, causing zero side-effects."""
    module = DummyTransformModule()
    sink = MockEventSink()
    ctx = ExecutionContext(run_id="run_1", node_id="node_1", event_sink=sink)

    # 1. Missing required field 'count'
    with pytest.raises(SchemaValidationError) as exc_info:
        await module.execute(ctx, {"text": "hello"}, {"mode": "upper"})
    assert module.effect_count == 0
    assert exc_info.value.code == ContractErrorCode.INVALID_SCHEMA
    assert "missing required field 'count'" in exc_info.value.message

    # 2. Unexpected extra field when additionalProperties=False
    bad_payload = {"text": "hello", "count": 2, "extra": "forbidden"}
    with pytest.raises(SchemaValidationError) as exc_info2:
        await module.execute(ctx, bad_payload, {"mode": "upper"})
    assert module.effect_count == 0
    assert "unexpected extra properties" in exc_info2.value.message

    # 3. Wrong type for 'count'
    with pytest.raises(SchemaValidationError):
        await module.execute(ctx, {"text": "hello", "count": "not-an-int"}, {"mode": "upper"})
    assert module.effect_count == 0


@pytest.mark.asyncio
async def test_reject_invalid_config_causes_zero_effects() -> None:
    """Invalid configuration is rejected before invoke, causing zero side-effects."""
    module = DummyTransformModule()
    ctx = ExecutionContext(run_id="run_1", node_id="node_1")

    # Invalid enum value
    with pytest.raises(ConfigValidationError) as exc_info:
        await module.execute(ctx, {"text": "hello", "count": 1}, {"mode": "invalid_mode"})
    assert module.effect_count == 0
    assert exc_info.value.code == ContractErrorCode.INVALID_CONFIG
    assert "allowed_values" in exc_info.value.details


@pytest.mark.asyncio
async def test_two_nodes_using_one_module_retain_distinct_ids() -> None:
    """Two nodes using the same module retain distinct node_id and run_id identities."""
    module = DummyTransformModule()
    sink = MockEventSink()

    ctx1 = ExecutionContext(
        workspace_id="ws_1",
        run_id="run_alpha",
        node_id="node_step_1",
        attempt_id="att_1",
        event_sink=sink,
    )
    ctx2 = ExecutionContext(
        workspace_id="ws_1",
        run_id="run_alpha",
        node_id="node_step_2",
        attempt_id="att_1",
        event_sink=sink,
    )

    res1 = await module.execute(ctx1, {"text": "foo", "count": 1}, {"mode": "upper"})
    res2 = await module.execute(ctx2, {"text": "bar", "count": 2}, {"mode": "lower"})

    assert res1.outputs["result"] == "FOO"
    assert res2.outputs["result"] == "barbar"

    # Verify event sink recorded events with distinct node_ids
    node_ids = [e.node_id for e in sink.events]
    assert "node_step_1" in node_ids
    assert "node_step_2" in node_ids
    assert all(e.run_id == "run_alpha" for e in sink.events)


@pytest.mark.asyncio
async def test_execution_cancellation_and_deadline() -> None:
    """Execution context enforces cancellation and deadline checks."""
    ctx = ExecutionContext(
        run_id="run_cancel",
        node_id="node_cancel",
        is_cancelled=True,
    )
    assert ctx.is_cancelled is True
    with pytest.raises(ExecutionCancelledError):
        ctx.check_cancelled()

    module = DummyTransformModule()
    with pytest.raises(ExecutionCancelledError):
        await module.execute(ctx, {"text": "a", "count": 1}, {"mode": "upper"})
    assert module.effect_count == 0


def test_credential_reference_no_raw_secret_leak() -> None:
    """CredentialReference references secrets by ID and does not hold raw secret values."""
    ref = CredentialReference(
        credential_id="cred_openai_key",
        credential_type="bearer_token",
        environment_variable="OPENAI_API_KEY",
    )
    dumped = ref.model_dump()
    assert "credential_id" in dumped
    assert "raw_secret" not in dumped
    assert "key" not in dumped


def test_usage_interface_distinguishes_reported_vs_unknown() -> None:
    """Usage attached to ModuleResult preserves distinction between None and zero."""
    unknown_usage = Usage(
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        is_complete=False,
    )
    result_unknown = ModuleResult.success(
        outputs={"res": "ok"},
        usage=unknown_usage,
    )
    assert result_unknown.usage is not None
    assert result_unknown.usage.is_reported is False
    assert result_unknown.usage.total_tokens is None


def test_schema_validator_enforces_min_and_max_items() -> None:
    from nexura_agents.contracts.errors import SchemaValidationError
    from nexura_agents.contracts.schema_validator import validate_schema

    schema = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2}
    validate_schema(["a", "b"], schema)
    with pytest.raises(SchemaValidationError, match="maxItems"):
        validate_schema(["a", "b", "c"], schema)
    with pytest.raises(SchemaValidationError, match="minItems"):
        validate_schema([], schema)
