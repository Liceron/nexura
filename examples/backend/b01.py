"""Standalone executable example for B01: Versioned registry and module CLI.

Demonstrates:
- Immutable (module_id, version) registration.
- Multi-version resolution.
- Schema inspection via describe().
- Invocation via Python API and standalone CLI.
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from typing import Any

from nexura_runtime.contracts import (
    BaseModule,
    ExecutionContext,
    ModuleManifest,
    ModuleResult,
    PortDefinition,
)

from nexura_runtime.modules import VersionedModuleRegistry
from nexura_runtime.modules.cli import main as cli_main


class EchoV1(BaseModule):
    """Simple echo module version 1.0.0."""

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            schema_version=1,
            module_id="nexura.demo.echo",
            version="1.0.0",
            kind="transform",
            title="Echo Module V1",
            description="Returns input text directly.",
            effect_policy="pure",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
                "additionalProperties": False,
            },
            config_schema={"type": "object", "additionalProperties": False},
            named_ports=[
                PortDefinition(name="text", type="string"),
                PortDefinition(name="echo", type="string"),
            ],
            capabilities={"network": False, "streaming": False},
        )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        return ModuleResult.success(outputs={"echo": f"v1:{inputs['text']}"})


class EchoV2(BaseModule):
    """Updated echo module version 1.1.0."""

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            schema_version=1,
            module_id="nexura.demo.echo",
            version="1.1.0",
            kind="transform",
            title="Echo Module V2",
            description="Returns prefixed input text.",
            effect_policy="pure",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
                "additionalProperties": False,
            },
            config_schema={"type": "object", "additionalProperties": False},
            capabilities={"network": False, "streaming": False},
        )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        return ModuleResult.success(outputs={"echo": f"v2:{inputs['text']}"})


async def run_demo() -> None:
    registry = VersionedModuleRegistry(sdk_version="0.3.0")

    # 1. Register multiple versions of the same module
    mod_v1 = EchoV1()
    mod_v2 = EchoV2()
    registry.register(mod_v1)
    registry.register(mod_v2)

    print("Registered modules:")
    for item in registry.list_modules():
        print(f" - {item['module_id']} v{item['version']} [{item['kind']}]")

    # 2. Resolve specific versions
    resolved_v1 = registry.get("nexura.demo.echo", "1.0.0")
    resolved_v2 = registry.get("nexura.demo.echo", "1.1.0")
    latest = registry.get("nexura.demo.echo")  # defaults to latest (1.1.0)

    assert resolved_v1.manifest.version == "1.0.0"
    assert resolved_v2.manifest.version == "1.1.0"
    assert latest.manifest.version == "1.1.0"

    # 3. Direct invocation through Python API
    ctx = ExecutionContext(run_id="run_b01_demo", node_id="node_demo_1")
    res1 = await resolved_v1.execute(ctx, {"text": "hello"}, {})
    res2 = await resolved_v2.execute(ctx, {"text": "hello"}, {})

    print(f"V1 Output: {res1.outputs}")
    print(f"V2 Output: {res2.outputs}")
    assert res1.outputs["echo"] == "v1:hello"
    assert res2.outputs["echo"] == "v2:hello"

    # 4. Describe module
    desc = registry.describe("nexura.demo.echo", "1.0.0")
    print(f"V1 Manifest Digest: {desc['digest']}")
    assert desc["is_available"] is True

    # 5. Invoke via CLI runner on default registry
    from nexura_runtime.modules import default_registry

    if not default_registry.has("nexura.demo.echo", "1.0.0"):
        default_registry.register(mod_v1)

    f = io.StringIO()
    with redirect_stdout(f):
        exit_code = cli_main(["run", "nexura.demo.echo", "-i", '{"text":"cli_test"}'])
    cli_out = f.getvalue().strip()
    print(f"CLI exit code: {exit_code}, Output: {cli_out}")

    assert exit_code == 0
    parsed = json.loads(cli_out)
    assert parsed["status"] == "success"
    assert parsed["outputs"]["echo"] == "v1:cli_test"
    print("B01 standalone example executed and verified successfully.")


if __name__ == "__main__":
    asyncio.run(run_demo())
