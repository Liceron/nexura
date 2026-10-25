# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Immutable, versioned module registry with entrypoint discovery."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import re
from collections.abc import Callable
from typing import Any

try:
    from packaging.version import Version
except ImportError:

    class Version:  # type: ignore[no-redef]
        def __init__(self, v: str) -> None:
            self._parts = [
                int(p) if p.isdigit() else str(p) for p in v.replace("-", ".").split(".")
            ]

        def __lt__(self, other: Any) -> bool:
            return self._parts < getattr(other, "_parts", [])

        def __gt__(self, other: Any) -> bool:
            return self._parts > getattr(other, "_parts", [])

        def __ge__(self, other: Any) -> bool:
            return self._parts >= getattr(other, "_parts", [])

        def __le__(self, other: Any) -> bool:
            return self._parts <= getattr(other, "_parts", [])

        def __eq__(self, other: Any) -> bool:
            return self._parts == getattr(other, "_parts", [])


from nexura_agents.contracts import (
    BaseModule,
    ContractError,
    ContractErrorCode,
    ExecutionContext,
    MissingDependencyError,
    ModuleManifest,
    ModuleResult,
)

from nexura_runtime import __version__ as CURRENT_SDK_VERSION

_DISTRIBUTION_NAME = "nexura-runtime"
_EXTRA_MARKER = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _declared_extras() -> dict[str, list[str]] | None:
    """Map each declared package extra to its requirement distribution names."""
    try:
        requires = importlib.metadata.requires(_DISTRIBUTION_NAME) or []
    except importlib.metadata.PackageNotFoundError:
        return None
    extras: dict[str, list[str]] = {}
    for requirement in requires:
        marker = _EXTRA_MARKER.search(requirement)
        name = _REQUIREMENT_NAME.match(requirement)
        if marker and name:
            extras.setdefault(marker.group(1), []).append(name.group(1))
    return extras


def _missing_extra_reason(extra: str) -> str | None:
    """Explain why an optional extra is unusable, or None when it is installed."""
    extras = _declared_extras()
    if extras is None:
        return f"Cannot verify extra '{extra}': {_DISTRIBUTION_NAME} metadata is not installed"
    if extra not in extras:
        return f"Unknown optional dependency extra '{extra}'; it is not declared by this SDK"
    # Declared extras use distributions whose import name is the normalized dist name.
    missing = [
        dist
        for dist in extras[extra]
        if importlib.util.find_spec(dist.lower().replace("-", "_").replace(".", "_")) is None
    ]
    if missing:
        return f"Missing extra '{extra}': {', '.join(missing)} not installed"
    return None


class ModuleCollisionError(ContractError):
    """Raised when registering a module with an existing (module_id, version)."""

    def __init__(self, module_id: str, version: str) -> None:
        super().__init__(
            f"Module collision: '{module_id}' version '{version}' is already registered.",
            code=ContractErrorCode.INVALID_CONFIG,
            details={"module_id": module_id, "version": version},
        )


class IncompatibleSDKVersionError(ContractError):
    """Raised when a module requires an SDK version newer than the runtime environment."""

    def __init__(self, module_id: str, min_sdk_version: str, current_sdk_version: str) -> None:
        super().__init__(
            f"Module '{module_id}' requires SDK version >= '{min_sdk_version}', "
            f"but current SDK is '{current_sdk_version}'.",
            code=ContractErrorCode.INVALID_CONFIG,
            details={
                "module_id": module_id,
                "min_sdk_version": min_sdk_version,
                "current_sdk_version": current_sdk_version,
            },
        )


class ModuleNotFoundError(ContractError):
    """Raised when a requested module or specific version cannot be found."""

    def __init__(self, module_id: str, version: str | None = None) -> None:
        msg = (
            f"Module '{module_id}' version '{version}' not found."
            if version
            else f"Module '{module_id}' not found."
        )
        super().__init__(
            msg,
            code=ContractErrorCode.INVALID_CONFIG,
            details={"module_id": module_id, "version": version},
        )


class RegisteredModule:
    """Internal container for a registered module and its metadata."""

    def __init__(
        self,
        manifest: ModuleManifest,
        target: BaseModule | type[BaseModule] | Callable[[], BaseModule] | None,
        *,
        missing_package: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.target = target
        #: Distribution that provides this module when it is not installed (see
        #: ``VersionedModuleRegistry.register_unavailable``); ``target`` is then None.
        self.missing_package = missing_package
        self._instance: BaseModule | None = target if isinstance(target, BaseModule) else None

    @property
    def is_available(self) -> bool:
        """Check if required extras/dependencies are installed."""
        return self.missing_reason is None

    @property
    def missing_reason(self) -> str | None:
        """Return failure reason if optional dependencies are missing, else None."""
        if self.missing_package is not None:
            return (
                f"Module '{self.manifest.module_id}' is provided by the "
                f"'{self.missing_package}' package, which is not installed. "
                f"Install it with: pip install {self.missing_package}"
            )
        extra = self.manifest.dependency_extra
        if not extra:
            return None
        return _missing_extra_reason(extra)

    def get_instance(self) -> BaseModule:
        """Return the executable module instance.

        Raises:
            MissingDependencyError: If an optional extra is required but not installed.
        """
        if self.missing_package is not None:
            raise MissingDependencyError(
                dependency_name=self.missing_package, message=self.missing_reason
            )
        if not self.is_available:
            raise MissingDependencyError(
                dependency_name=self.manifest.dependency_extra or "unknown",
                extra_name=self.manifest.dependency_extra,
                message=(
                    f"Module '{self.manifest.module_id}' requires extra "
                    f"'{self.manifest.dependency_extra}' which is not installed."
                ),
            )
        if self._instance is None and (inspect.isclass(self.target) or callable(self.target)):
            self._instance = self.target()
        if self._instance is None:
            raise RuntimeError(f"Could not instantiate module '{self.manifest.module_id}'")
        return self._instance


class LegacyModuleAdapter(BaseModule):
    """Wraps a legacy callable handler into a typed BaseModule."""

    def __init__(self, name: str, handler: Callable[..., Any]) -> None:
        self._name = name
        self._handler = handler
        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=f"legacy.{name}",
            version="0.1.0",
            kind="legacy_handler",
            title=f"Legacy Handler: {name}",
            description="Compatibility adapter for legacy workflow callable handlers.",
            # A legacy callable declares nothing about its effects: non_idempotent (A6).
            effect_policy="non_idempotent_write",
            capabilities={"network": False, "streaming": False},
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        if inspect.iscoroutinefunction(self._handler):
            res = await self._handler(inputs)
        else:
            res = self._handler(inputs)
        outputs = res if isinstance(res, dict) else {"result": res}
        return ModuleResult.success(outputs=outputs)


class VersionedModuleRegistry:
    """Registry maintaining immutable (module_id, version) module registrations."""

    def __init__(self, sdk_version: str = CURRENT_SDK_VERSION) -> None:
        self.sdk_version = sdk_version
        self._modules: dict[tuple[str, str], RegisteredModule] = {}

    def register(
        self,
        module: BaseModule | type[BaseModule] | Callable[[], BaseModule],
        manifest: ModuleManifest | None = None,
    ) -> None:
        """Register a module immutably by (module_id, version).

        Raises:
            ModuleCollisionError: If already registered.
            IncompatibleSDKVersionError: If module requires newer SDK.
        """
        if manifest is None:
            if isinstance(module, BaseModule):
                manifest = module.manifest
            elif inspect.isclass(module) and hasattr(module, "manifest"):
                manifest = module.manifest
                if callable(manifest):
                    manifest = manifest()
            else:
                raise ValueError(
                    "A ModuleManifest is required when registering a factory or class."
                )

        # Check SDK compatibility
        if Version(manifest.min_sdk_version) > Version(self.sdk_version):
            raise IncompatibleSDKVersionError(
                manifest.module_id,
                manifest.min_sdk_version,
                self.sdk_version,
            )

        key = (manifest.module_id, manifest.version)
        if key in self._modules:
            raise ModuleCollisionError(manifest.module_id, manifest.version)

        self._modules[key] = RegisteredModule(manifest=manifest, target=module)

    def register_unavailable(self, manifest: ModuleManifest, package: str) -> None:
        """Register ``manifest`` as provided by the distribution ``package``, not installed.

        The module id stays known (listed, described and validated with a clear reason that
        names ``pip install <package>``), but it cannot run: ``get`` raises
        ``MissingDependencyError``. Installing the package registers the real module instead.
        """
        key = (manifest.module_id, manifest.version)
        if key in self._modules:
            raise ModuleCollisionError(manifest.module_id, manifest.version)
        self._modules[key] = RegisteredModule(manifest, None, missing_package=package)

    def register_legacy(self, name: str, handler: Callable[..., Any]) -> None:
        """Register a legacy callable handler via compatibility adapter."""
        adapter = LegacyModuleAdapter(name, handler)
        # Register both under legacy.name and plain name for full backwards compatibility
        key_legacy = (f"legacy.{name}", "0.1.0")
        if key_legacy not in self._modules:
            self._modules[key_legacy] = RegisteredModule(adapter.manifest, adapter)

        manifest_plain = adapter.manifest.model_copy(update={"module_id": name})
        key_plain = (name, "0.1.0")
        if key_plain not in self._modules:
            self._modules[key_plain] = RegisteredModule(manifest_plain, adapter)

    def get(self, module_id: str, version: str | None = None) -> BaseModule:
        """Retrieve executable module instance.

        If version is omitted, returns the highest semantic version registered.
        """
        registered = self._resolve_registered(module_id, version)
        return registered.get_instance()

    def get_entry(self, module_id: str, version: str | None = None) -> RegisteredModule:
        """Retrieve registered module entry containing manifest and metadata."""
        return self._resolve_registered(module_id, version)

    def get_module(self, module_id: str, version: str | None = None) -> RegisteredModule:
        """Alias for get_entry."""
        return self._resolve_registered(module_id, version)

    def has(self, module_id: str, version: str | None = None) -> bool:
        """True if the module is registered (regardless of dependency availability)."""
        try:
            self._resolve_registered(module_id, version)
            return True
        except ModuleNotFoundError:
            return False

    def describe(self, module_id: str, version: str | None = None) -> dict[str, Any]:
        """Inspect module metadata and dependency status without executing it."""
        registered = self._resolve_registered(module_id, version)
        return {
            "manifest": registered.manifest.model_dump(mode="json"),
            "digest": registered.manifest.digest,
            "is_available": registered.is_available,
            "missing_reason": registered.missing_reason,
        }

    def list_modules(self) -> list[dict[str, Any]]:
        """List all registered modules sorted by module_id and version."""
        results: list[dict[str, Any]] = []
        for (module_id, version), reg in sorted(
            self._modules.items(),
            key=lambda item: (item[0][0], Version(item[0][1])),
        ):
            results.append(
                {
                    "module_id": module_id,
                    "version": version,
                    "title": reg.manifest.title,
                    "kind": reg.manifest.kind,
                    "effect_policy": reg.manifest.effect_policy,
                    "digest": reg.manifest.digest,
                    "is_available": reg.is_available,
                    "missing_reason": reg.missing_reason,
                }
            )
        return results

    def discover_entry_points(
        self,
        group: str = "nexura.modules",
        allowlist: list[str] | None = None,
    ) -> int:
        """Discover and register modules declared via package entry points."""
        selected_eps = importlib.metadata.entry_points(group=group)

        count = 0
        for ep in selected_eps:
            if allowlist is not None and ep.name not in allowlist:
                continue
            loaded = ep.load()
            is_module_cls = inspect.isclass(loaded) and issubclass(loaded, BaseModule)
            if isinstance(loaded, BaseModule) or is_module_cls:
                self.register(loaded)
                count += 1
        return count

    def _resolve_registered(self, module_id: str, version: str | None) -> RegisteredModule:
        if version is not None:
            key = (module_id, version)
            if key not in self._modules:
                raise ModuleNotFoundError(module_id, version)
            return self._modules[key]

        # Version is None: find all matching versions
        matches = [(v, reg) for (mid, v), reg in self._modules.items() if mid == module_id]
        if not matches:
            raise ModuleNotFoundError(module_id, None)

        # Sort by Version descending
        matches.sort(key=lambda item: Version(item[0]), reverse=True)
        return matches[0][1]


# Global shared registry instance
default_registry: VersionedModuleRegistry = VersionedModuleRegistry()


def _register_agent_sdk_modules(registry: VersionedModuleRegistry) -> None:
    """Expose the agent SDK's provider and budget modules to workflows and Studio."""
    from nexura_agents.providers.anthropic.module import AnthropicProviderModule
    from nexura_agents.providers.gemini.module import GeminiProviderModule
    from nexura_agents.providers.module import ModelProviderModule
    from nexura_agents.providers.openai.module import OpenAIProviderModule
    from nexura_agents.providers.sarvam.module import SarvamProviderModule
    from nexura_agents.usage.module import UsageBudgetModule

    for module_id, target in (
        ("nexura.models.provider", ModelProviderModule),
        ("nexura.models.openai", OpenAIProviderModule),
        ("nexura.models.gemini", GeminiProviderModule),
        ("nexura.models.anthropic", AnthropicProviderModule()),
        ("nexura.models.sarvam", SarvamProviderModule()),
        (None, UsageBudgetModule()),
    ):
        key = module_id or target.manifest.module_id
        if not registry.has(key):
            registry.register(target)
    # P39 G6: the guardrails.check node, when nexura-guardrails is installed (optional).
    try:
        from nexura_guardrails.runtime.module import GuardrailCheckModule
    except ImportError:
        return
    check_module = GuardrailCheckModule()
    if not registry.has(check_module.manifest.module_id):
        registry.register(check_module)


_register_agent_sdk_modules(default_registry)
