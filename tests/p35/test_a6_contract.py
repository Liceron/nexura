"""Offline value-type checks for A6 S1; no executor or service behavior changes."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from dataclasses import asdict
from typing import Any, get_args

import pytest
from nexura_agents import contracts
from nexura_agents.contracts import (
    Applied,
    DefinitionMismatchError,
    EffectClass,
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    NotApplied,
    ReconcileResult,
    StepIntent,
    StepKey,
    StepOutcome,
    Unknown,
    default_idempotency_key,
    derive_effect_id,
    effect_class_from_idempotent,
    effect_class_from_policy,
    execution,
)
from nexura_agents.contracts.manifest import EffectPolicy
from nexura_agents.errors import NexuraError


def _intent(**overrides: Any) -> StepIntent:
    values: dict[str, Any] = {
        "step_key": StepKey.agent_tool(2, "pay"),
        "payload_digest": "payload-sha256",
        "idempotency_key": default_idempotency_key(
            ExecutionId("workspace", "run"), StepKey.agent_tool(2, "pay")
        ),
    }
    return StepIntent(**(values | overrides))


def test_key_derivation_known_vector_and_process_stability() -> None:
    execution_id = ExecutionId("workspace", "run")
    step_key = StepKey.agent_tool(2, "pay")
    # sha256 of the exact UTF-8 bytes b'["workspace","run","turn:2/call:pay",0]'.
    expected = "f9b40fd6313134998d212d50583ad87df75de95951c405f4e09d487f40bdcdfa"
    assert derive_effect_id(execution_id, step_key) == expected
    assert default_idempotency_key(execution_id, step_key) == "nx-" + expected[:32]
    code = (
        "from nexura_agents.contracts import ExecutionId, derive_effect_id; "
        "print(derive_effect_id(ExecutionId('workspace', 'run'), 'turn:2/call:pay'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONHASHSEED": "123"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == expected


def test_identity_components_and_tuple_boundaries_are_distinct() -> None:
    identities = [
        (ExecutionId("workspace", "run"), "turn:2/call:pay", 0),
        (ExecutionId("workspace2", "run"), "turn:2/call:pay", 0),
        (ExecutionId("workspace", "run2"), "turn:2/call:pay", 0),
        (ExecutionId("workspace", "run"), "turn:3/call:pay", 0),
        (ExecutionId("workspace", "run"), "turn:2/call:other", 0),
        (ExecutionId("workspace", "run"), "turn:2/call:pay", 1),
        (ExecutionId("ab", "c"), "node#1", 0),
        (ExecutionId("a", "bc"), "node#1", 0),
        (ExecutionId("a:b", "c"), "node#1", 0),
        (ExecutionId("a", "b:c"), "node#1", 0),
        (ExecutionId("工作区", 'r\n"\\'), "node#1", 0),
        (ExecutionId("workspace", "run"), "node#1", 0),
        (ExecutionId("workspace", "run"), "node#2", 0),
    ]
    assert len({derive_effect_id(*identity) for identity in identities}) == len(identities)
    assert len({default_idempotency_key(*identity) for identity in identities}) == len(identities)
    for identity in identities:
        assert len(derive_effect_id(*identity)) == 64
        assert len(default_idempotency_key(*identity)) == 35
    # Literal Unicode is UTF-8 encoded rather than JSON's default ASCII escaping.
    assert derive_effect_id(ExecutionId("工作区", "run"), "node#1") == (
        "8e51aae1da78efd5de6d004bce2e09ed000d34120dd36780e128b286dc9976c0"
    )


@pytest.mark.parametrize(
    ("key", "spelling"),
    [
        (StepKey.agent_tool(0, "id/with:#[]"), "turn:0/call:id/with:#[]"),
        (StepKey.agent_model(3), "turn:3/model"),
        (StepKey.workflow("node", 0), "node#0"),
        (StepKey.workflow("node", 4, attempt=1), "node#4"),
        (StepKey.workflow("node", 4, attempt=2), "node#4.2"),
        (StepKey.workflow("node", 4, item=0), "node#4[0]"),
        (StepKey.workflow("node", 4, attempt=2, item=10), "node#4.2[10]"),
        (StepKey.workflow("parent/child#nested", 4), "parent/child#nested#4"),
    ],
)
def test_step_key_round_trips(key: StepKey, spelling: str) -> None:
    assert str(key) == spelling
    assert StepKey.parse(str(key)) == key
    assert StepKey.parse(json.loads(json.dumps(key))) == key


@pytest.mark.parametrize(
    "spelling",
    [
        "",
        "turn:-1/model",
        "turn:01/model",
        "turn:1/call:",
        "node",
        "#1",
        "node#-1",
        "node#1.0",
        "node#1[-1]",
        "node#1[[0]]",
        "node#1.2[0]junk",
    ],
)
def test_malformed_keys_are_rejected(spelling: str) -> None:
    with pytest.raises(ValueError):
        StepKey.parse(spelling)


@pytest.mark.parametrize("counter", [-1, True, 1.5, "1"])
def test_invalid_counters_are_rejected(counter: Any) -> None:
    with pytest.raises(ValueError):
        StepKey.agent_model(counter)
    with pytest.raises(ValueError):
        StepKey.agent_tool(counter, "call")
    with pytest.raises(ValueError):
        StepKey.workflow("node", counter)
    with pytest.raises(ValueError):
        StepKey.workflow("node", 1, attempt=counter)
    with pytest.raises(ValueError):
        StepKey.workflow("node", 1, item=counter)
    with pytest.raises(ValueError):
        derive_effect_id(ExecutionId("w", "r"), "node#1", counter)


def test_empty_identifiers_and_zero_attempt_are_rejected() -> None:
    for workspace_id, run_id in [("", "run"), ("workspace", "")]:
        with pytest.raises(ValueError):
            ExecutionId(workspace_id, run_id)
    with pytest.raises(ValueError):
        StepKey.agent_tool(1, "")
    with pytest.raises(ValueError):
        StepKey.workflow("", 1)
    with pytest.raises(ValueError):
        StepKey.workflow("node", 1, attempt=0)


def test_all_legacy_policy_spellings_and_defaults() -> None:
    expected = {
        "pure": EffectClass.PURE,
        "read": EffectClass.PURE,
        "idempotent_write": EffectClass.IDEMPOTENT,
        "non_idempotent_write": EffectClass.NON_IDEMPOTENT,
    }
    assert set(get_args(EffectPolicy)) == set(expected)
    for spelling, effect_class in expected.items():
        assert effect_class_from_policy(spelling) is effect_class
    assert effect_class_from_idempotent(True) is EffectClass.IDEMPOTENT
    assert effect_class_from_idempotent(False) is EffectClass.NON_IDEMPOTENT
    assert effect_class_from_idempotent() is EffectClass.NON_IDEMPOTENT
    assert effect_class_from_policy() is EffectClass.NON_IDEMPOTENT
    assert effect_class_from_policy(None) is EffectClass.NON_IDEMPOTENT
    assert _intent().effect_class is EffectClass.NON_IDEMPOTENT
    assert _intent().status == "dispatched"
    assert EffectRecord(ExecutionId("w", "r"), _intent()).status is EffectStatus.PENDING
    first, second = _intent(), _intent()
    first.reservation_ids.append("reservation")
    assert second.reservation_ids == []


@pytest.mark.parametrize("spelling", ["", "write", "PURE", "idempotent", "non-idempotent"])
def test_unknown_policy_is_rejected(spelling: str) -> None:
    with pytest.raises(ValueError, match="Unknown EffectPolicy"):
        effect_class_from_policy(spelling)


@pytest.mark.parametrize("value", [None, "true", 1])
def test_boolean_alias_does_not_accept_truthy_values(value: Any) -> None:
    with pytest.raises(TypeError, match="bool"):
        effect_class_from_idempotent(value)


def test_identity_intent_and_outcome_json_round_trips() -> None:
    identity = ExecutionId("工作区", "opaque/run")
    assert ExecutionId(**json.loads(json.dumps(asdict(identity)))) == identity
    for effect_class in EffectClass:
        intent = _intent(
            effect_class=effect_class, approval_id="approved", reservation_ids=["r1", "r2"]
        )
        restored = StepIntent(**json.loads(json.dumps(asdict(intent))))
        assert restored == intent
        assert restored.effect_class is effect_class
        assert isinstance(restored.step_key, StepKey)
    for status in ("completed", "failed"):
        values: dict[str, Any] = {
            "step_key": StepKey.agent_model(0),
            "status": status,
            "output": {"text": "你好", "items": [1, None, True]},
        }
        outcome = StepOutcome(**values)
        restored_outcome = StepOutcome(**json.loads(json.dumps(asdict(outcome))))
        assert restored_outcome == outcome
        assert isinstance(restored_outcome.step_key, StepKey)


@pytest.mark.parametrize(
    ("status", "resolution"),
    [
        (EffectStatus.PENDING, None),
        (EffectStatus.CONFIRMED, None),
        (EffectStatus.INDETERMINATE, None),
    ]
    + [(EffectStatus.RESOLVED, resolution) for resolution in EffectResolution],
)
def test_effect_record_json_round_trip(
    status: EffectStatus, resolution: EffectResolution | None
) -> None:
    record = EffectRecord(
        ExecutionId("workspace", "run"),
        _intent(),
        effect_index=2,
        status=status,
        resolution=resolution,
        result={"receipt": 42},
        resolved_by="operator" if resolution is not None else None,
    )
    restored = EffectRecord(**json.loads(json.dumps(asdict(record))))
    assert restored == record
    assert restored.effect_id == derive_effect_id(record.execution_id, record.intent.step_key, 2)
    assert restored.status is status
    assert restored.resolution is resolution
    assert isinstance(restored.execution_id, ExecutionId)
    assert isinstance(restored.intent, StepIntent)
    assert isinstance(restored.intent.step_key, StepKey)
    assert restored.intent.effect_class is EffectClass.NON_IDEMPOTENT


@pytest.mark.parametrize(
    "result", [Applied({"receipt": [1, None]}), Applied(None), NotApplied(), Unknown()]
)
def test_reconcile_result_json_round_trip(result: ReconcileResult) -> None:
    assert ReconcileResult.from_dict(json.loads(json.dumps(result.to_dict()))) == result
    assert type(ReconcileResult.from_dict(result.to_dict())) is type(result)


def test_invalid_statuses_and_resolutions_are_rejected() -> None:
    with pytest.raises(ValueError):
        _intent(status="pending")
    with pytest.raises(ValueError):
        _intent(effect_class="read")
    invalid_outcome: dict[str, Any] = {"step_key": "node#1", "status": "paused"}
    with pytest.raises(ValueError):
        StepOutcome(**invalid_outcome)
    for status, resolution in [
        ("resolved", None),
        ("pending", "confirmed"),
        ("reconciled", "rolled_back"),
        ("resolved", "voided"),
    ]:
        invalid_effect: dict[str, Any] = {
            "execution_id": ExecutionId("w", "r"),
            "intent": _intent(),
            "status": status,
            "resolution": resolution,
        }
        with pytest.raises(ValueError):
            EffectRecord(**invalid_effect)
    for data in [{}, {"status": "failed"}, {"status": "Applied"}]:
        with pytest.raises(ValueError):
            ReconcileResult.from_dict(data)
    for variant in (Applied, NotApplied, Unknown):
        invalid_result: dict[str, Any] = {
            "status": "bad", **({"result": None} if variant is Applied else {})
        }
        with pytest.raises(ValueError):
            variant(**invalid_result)


@pytest.mark.parametrize("error_type", [LeaseLostError, DefinitionMismatchError])
def test_errors_use_the_existing_sdk_base(error_type: type[NexuraError]) -> None:
    cause = RuntimeError("store refused")
    error = error_type("contract refused", original_error=cause)
    assert isinstance(error, NexuraError)
    assert error.message == "contract refused"
    assert error.__cause__ is cause


def test_execution_names_are_exported_and_documented() -> None:
    public = {
        name
        for name, value in vars(execution).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == execution.__name__
    }
    assert len(public) == 18
    for name in public:
        assert name in contracts.__all__
        assert getattr(contracts, name) is getattr(execution, name)
        assert inspect.getdoc(getattr(execution, name))


def test_contract_import_does_not_load_the_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from nexura_agents.contracts import EffectClass; "
            "assert not any(m == 'nexura_runtime' or m.startswith('nexura_runtime.') "
            "for m in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
