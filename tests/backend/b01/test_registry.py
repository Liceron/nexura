"""Focused acceptance tests for B01: Versioned registry and standalone module CLI."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest
from nexura_runtime.contracts import (
    BaseModule,
    ExecutionContext,
    MissingDependencyError,
    ModuleManifest,
    ModuleResult,
)

from nexura_runtime.modules import (
    IncompatibleSDKVersionError,
    ModuleCollisionError,
    ModuleNotFoundError,
    VersionedModuleRegistry,
)
from nexura_runtime.modules.cli import main as cli_main


class DummyEchoModule(BaseModule):
    def __init__(
        self,
        version: str = "1.0.0",
        extra: str | None = None,
        min_sdk: str = "0.3.0",
    ) -> None:
        self._version = version
        self._extra = extra
        self._min_sdk = min_sdk

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            schema_version=1,
            module_id="nexura.test.echo",
            version=self._version,
            kind="transform",
            title="Test Echo",
            description="Test module for registry acceptance",
            dependency_extra=self._extra,
            min_sdk_version=self._min_sdk,
            input_schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
            },
        )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        return ModuleResult.success(outputs={"echo": f"{self._version}:{inputs['msg']}"})


def test_registry_rejects_collisions() -> None:
    """Duplicate registration with identical (module_id, version) raises ModuleCollisionError."""
    registry = VersionedModuleRegistry()
    mod1 = DummyEchoModule(version="1.0.0")
    mod2 = DummyEchoModule(version="1.0.0")

    registry.register(mod1)
    with pytest.raises(ModuleCollisionError):
        registry.register(mod2)


def test_registry_rejects_incompatible_sdk_version() -> None:
    """Module requiring higher min_sdk_version is rejected upon registration."""
    registry = VersionedModuleRegistry(sdk_version="0.3.0")
    mod_future = DummyEchoModule(version="1.0.0", min_sdk="0.9.0")

    with pytest.raises(IncompatibleSDKVersionError) as exc_info:
        registry.register(mod_future)
    assert "requires SDK version >= '0.9.0'" in exc_info.value.message


def test_absent_extras_are_inspectable_but_not_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Modules with missing extras can be inspected (describe/list) but fail on get_instance."""
    monkeypatch.setattr("nexura_runtime.modules.registry.importlib.util.find_spec", lambda _: None)
    registry = VersionedModuleRegistry()
    # A declared extra whose packages find_spec reports missing. (This used 'postgres', an
    # extra that moved out with the nexura-postgres package in P28 step 10 stage 4b.)
    mod = DummyEchoModule(version="1.0.0", extra="knowledge")
    registry.register(mod)

    # 1. Inspectable via describe()
    desc = registry.describe("nexura.test.echo", "1.0.0")
    assert desc["manifest"]["module_id"] == "nexura.test.echo"
    assert desc["is_available"] is False
    assert "Missing extra 'knowledge'" in (desc["missing_reason"] or "")

    # 2. Inspectable via list_modules()
    all_mods = registry.list_modules()
    assert any(m["module_id"] == "nexura.test.echo" and not m["is_available"] for m in all_mods)

    # 3. Execution raises MissingDependencyError
    with pytest.raises(MissingDependencyError):
        registry.get("nexura.test.echo", "1.0.0")


def test_two_installed_plugin_versions_resolve_explicitly() -> None:
    """Multiple versions of a module are indexed and resolved explicitly or by latest semver."""
    registry = VersionedModuleRegistry()
    mod1 = DummyEchoModule(version="1.0.0")
    mod2 = DummyEchoModule(version="1.2.0")
    mod3 = DummyEchoModule(version="1.1.5")

    registry.register(mod1)
    registry.register(mod2)
    registry.register(mod3)

    assert registry.get("nexura.test.echo", "1.0.0").manifest.version == "1.0.0"
    assert registry.get("nexura.test.echo", "1.1.5").manifest.version == "1.1.5"
    assert registry.get("nexura.test.echo", "1.2.0").manifest.version == "1.2.0"

    # Default without version returns highest semver
    assert registry.get("nexura.test.echo").manifest.version == "1.2.0"

    with pytest.raises(ModuleNotFoundError):
        registry.get("nexura.test.echo", "2.0.0")


def test_importing_core_does_not_import_heavyweight_libraries() -> None:
    """Importing core SDK contracts and registry does not import unneeded modules."""
    import nexura_runtime.contracts  # noqa: F401

    import nexura_runtime.modules  # noqa: F401

    # Ensure heavy external modules were not imported
    assert "asyncpg" not in sys.modules
    assert "anthropic" not in sys.modules
    assert "google.genai" not in sys.modules


@pytest.mark.asyncio
async def test_legacy_callable_registration_adapter() -> None:
    """Legacy callable handlers can be registered and invoked as BaseModule."""
    registry = VersionedModuleRegistry()

    def my_handler(state: dict[str, Any]) -> dict[str, Any]:
        return {"processed": state.get("val", 0) * 2}

    registry.register_legacy("legacy_calc", my_handler)

    assert registry.has("legacy_calc")
    assert registry.has("legacy.legacy_calc")

    mod = registry.get("legacy_calc")
    ctx = ExecutionContext(run_id="run_legacy", node_id="node_legacy")
    res = await mod.execute(ctx, {"val": 21}, {})
    assert res.outputs["processed"] == 42


def test_cli_exits_nonzero_for_bad_input() -> None:
    """CLI exits nonzero when provided invalid JSON inputs or unknown module."""
    out = io.StringIO()
    err = io.StringIO()

    with redirect_stdout(out), redirect_stderr(err):
        # Non-existent module
        code = cli_main(["run", "non.existent.module", "-i", "{}"])
    assert code != 0

    out2 = io.StringIO()
    err2 = io.StringIO()
    with redirect_stdout(out2), redirect_stderr(err2):
        # Malformed JSON
        code2 = cli_main(["run", "non.existent.module", "-i", "{bad json"])
    assert code2 != 0
