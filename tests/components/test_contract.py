# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Every component is executable, discoverable and bounded, with valid example schemas."""

import asyncio

import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import ConfigValidationError, SchemaValidationError

from nexura_runtime.modules import default_registry
from nexura_runtime.modules.dataops import COMPONENT_CLASSES as DATA
from nexura_runtime.modules.logic import COMPONENT_CLASSES as LOGIC
from nexura_runtime.modules.textops import COMPONENT_CLASSES as TEXT
from nexura_runtime.modules.utils import COMPONENT_CLASSES as UTILS
from nexura_runtime.service.api.descriptors import _build_descriptor_from_entry

CLASSES = (*TEXT, *DATA, *LOGIC, *UTILS)


@pytest.mark.parametrize("cls", CLASSES)
def test_registration_descriptor_examples_and_bounds(cls, run_component):
    manifest = cls.manifest
    assert len(CLASSES) == 30
    assert len({(c.manifest.module_id, c.manifest.version) for c in CLASSES}) == 30
    assert isinstance(default_registry.get(manifest.module_id, manifest.version), cls)
    entry = default_registry.get_entry(manifest.module_id, manifest.version)
    descriptor = _build_descriptor_from_entry(entry)
    assert descriptor.module_id == manifest.module_id
    assert descriptor.is_available and descriptor.kind == "data_transform"
    assert descriptor.input_schema == manifest.input_schema
    assert descriptor.output_schema == manifest.output_schema
    assert descriptor.display and descriptor.display.examples
    assert manifest.effect_policy == "read" and manifest.capabilities["network"] is False
    assert manifest.timeout_seconds == 2 and manifest.output_limit_bytes == 262144
    example = manifest.display.examples[0]
    manifest.validate_inputs(example)
    manifest.validate_outputs(run_component(manifest.module_id, **example))
    context = ExecutionContext(run_id="r", node_id="n")
    with pytest.raises(SchemaValidationError):
        asyncio.run(cls().invoke(context, {**example, "unknown": True}))
    with pytest.raises(ConfigValidationError):
        asyncio.run(cls().invoke(context, example, {"unknown": True}))
    with pytest.raises(SchemaValidationError):
        asyncio.run(cls().invoke(context, {**example, "oversized": "x" * 131073}))


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 1 << 5000, {1: "bad"}, (1, 2), "\ud800"]
)
def test_non_json_inputs_rejected(run_component, value):
    with pytest.raises(SchemaValidationError):
        run_component("nexura.data.json_stringify", value=value)


def test_deep_cyclic_and_multibyte_limits(run_component):
    value = {}
    value["cycle"] = value
    with pytest.raises(SchemaValidationError, match="depth"):
        run_component("nexura.data.json_stringify", value=value)
    with pytest.raises(SchemaValidationError, match="bytes"):
        run_component("nexura.text.count", text="😀" * 40000)
    with pytest.raises(SchemaValidationError, match="1024"):
        run_component("nexura.data.dedupe", items=[0] * 1025)
