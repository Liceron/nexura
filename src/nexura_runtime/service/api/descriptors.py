# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""API endpoints for module discovery, versioned descriptors, and capability negotiation."""

from __future__ import annotations

import dataclasses
import functools
import inspect
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nexura_runtime import __version__ as CURRENT_SDK_VERSION
from nexura_runtime.modules.registry import (
    ModuleNotFoundError,
    VersionedModuleRegistry,
    default_registry,
)
from nexura_runtime.workflow import ModuleRegistry

router = APIRouter(tags=["descriptors"])


class PortDefinitionModel(BaseModel):
    """Named port metadata for module graph wiring."""

    name: str
    type: str = "any"
    description: str = ""
    required: bool = True


class ModuleDisplayModel(BaseModel):
    """Declarative UI metadata for canvas presentation."""

    label: str | None = None
    icon: str | None = None
    group: str | None = None
    examples: list[dict[str, Any]] = Field(default_factory=list)


class ModuleDescriptorResponse(BaseModel):
    """Declarative, versioned module descriptor exposing schemas and builder contracts."""

    module_id: str
    version: str
    kind: str
    title: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    named_ports: list[PortDefinitionModel] = Field(default_factory=list)
    supported_routes: list[str] = Field(default_factory=lambda: ["default"])
    required_credential_types: list[str] = Field(default_factory=list)
    capabilities: dict[str, bool] = Field(default_factory=dict)
    effect_policy: str = "pure"
    min_sdk_version: str = "0.3.0"
    verification_status: str = "unverified"
    is_available: bool = True
    missing_reason: str | None = None
    requires_approval: bool = False
    write_operations: list[str] = Field(default_factory=list)
    digest: str = ""
    display: ModuleDisplayModel | None = None
    #: Service-internal module: listed for existing workflows and tooling, but builders
    #: (Studio's module palette) do not offer it as a new node.
    internal: bool = False


class ModuleListResponse(BaseModel):
    """List response containing versioned descriptors."""

    items: list[ModuleDescriptorResponse]
    total: int
    modules: list[ModuleDescriptorResponse]  # Compatibility alias for legacy consumers


class ServiceCapabilitiesResponse(BaseModel):
    """Negotiation payload advertising available service features and profiles."""

    sdk_version: str
    active_profile: str
    supported_profiles: list[str]
    capabilities: dict[str, bool]
    supported_credential_types: list[str]
    supported_export_formats: list[str]
    provider_capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _build_descriptor_from_entry(
    entry: Any,
    approval_handlers: frozenset[str] | None = None,
) -> ModuleDescriptorResponse:
    """Safely convert a RegisteredModule entry into a sanitized JSON descriptor."""
    manifest = entry.manifest
    approval_required = False
    if approval_handlers:
        approval_required = (
            manifest.module_id in approval_handlers
            or getattr(manifest, "module_id", "") in approval_handlers
        )
    if hasattr(manifest, "requires_approval"):
        approval_required = approval_required or bool(manifest.requires_approval)
    write_ops = list(getattr(manifest, "write_operations", None) or [])
    always_write = bool(getattr(manifest, "always_write", False))
    if write_ops or always_write:
        approval_required = True

    named_ports = [
        PortDefinitionModel(
            name=p.name,
            type=p.type,
            description=p.description,
            required=p.required,
        )
        for p in getattr(manifest, "named_ports", [])
    ]

    display_obj = None
    if getattr(manifest, "display", None):
        disp = manifest.display
        display_obj = ModuleDisplayModel(
            label=getattr(disp, "label", None),
            icon=getattr(disp, "icon", None),
            group=getattr(disp, "group", None),
            examples=getattr(disp, "examples", []),
        )

    return ModuleDescriptorResponse(
        module_id=manifest.module_id,
        version=manifest.version,
        kind=manifest.kind,
        title=manifest.title,
        description=manifest.description,
        input_schema=getattr(manifest, "input_schema", {}),
        output_schema=getattr(manifest, "output_schema", {}),
        config_schema=getattr(manifest, "config_schema", {}),
        named_ports=named_ports,
        supported_routes=["default"],
        required_credential_types=list(getattr(manifest, "required_credential_types", [])),
        capabilities=dict(getattr(manifest, "capabilities", {})),
        effect_policy=getattr(manifest, "effect_policy", "pure"),
        min_sdk_version=getattr(manifest, "min_sdk_version", "0.3.0"),
        verification_status=getattr(manifest, "verification_status", "unverified"),
        is_available=bool(entry.is_available),
        missing_reason=entry.missing_reason,
        requires_approval=approval_required,
        write_operations=write_ops,
        digest=manifest.canonical_hash(),
        display=display_obj,
        internal=bool(getattr(manifest, "internal", False)),
    )


@router.get("/modules", response_model=ModuleListResponse)
async def list_modules(
    request: Request,
    kind: str | None = Query(default=None, description="Filter by module kind"),
    query: str | None = Query(default=None, description="Search query string"),
    available_only: bool = Query(default=False, description="Filter to installed modules"),
) -> ModuleListResponse:
    """List registered versioned modules with typed descriptors and capability schemas."""
    app_registry: ModuleRegistry | None = getattr(request.app.state, "registry", None)
    v_reg: VersionedModuleRegistry = (
        app_registry.versioned_registry
        if app_registry and app_registry.versioned_registry
        else default_registry
    )
    approval_handlers: frozenset[str] = getattr(
        request.app.state, "approval_required_handlers", frozenset()
    )

    descriptors: list[ModuleDescriptorResponse] = []
    seen_keys: set[str] = set()

    caller_ws = getattr(request.state, "workspace_id", "default")
    ws_repo = getattr(request.app.state, "workspace_repo", None)

    # 1. Inspect versioned modules
    for item in v_reg.list_modules():
        mod_id = item["module_id"]
        version = item["version"]

        # Scope integration tools to owning workspace
        if ws_repo is not None:
            owner_ws = ws_repo.get_resource_workspace("integration_tool", mod_id)
            if owner_ws is not None and owner_ws != caller_ws:
                continue

        key = f"{mod_id}@{version}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        entry = v_reg.get_entry(mod_id, version)
        desc = _build_descriptor_from_entry(entry, approval_handlers)

        # Apply filters
        if kind and desc.kind != kind:
            continue
        if available_only and not desc.is_available:
            continue
        if query:
            q_lower = query.lower()
            if (
                q_lower not in desc.module_id.lower()
                and q_lower not in desc.title.lower()
                and q_lower not in desc.description.lower()
            ):
                continue

        descriptors.append(desc)

    # 2. Inspect synthetic/legacy handlers registered in app_registry
    if app_registry and hasattr(app_registry, "_handlers"):
        for name, fn in app_registry._handlers.items():
            if ws_repo is not None:
                owner_ws = ws_repo.get_resource_workspace("integration_tool", name)
                if owner_ws is not None and owner_ws != caller_ws:
                    continue
            fn_ws = getattr(fn, "_workspace_id", None)
            if fn_ws and fn_ws != caller_ws:
                continue
            if name in seen_keys:
                continue
            seen_keys.add(name)

            doc = inspect.getdoc(fn) or "Trusted Python workflow handler."
            is_req_appr = name in approval_handlers
            desc = ModuleDescriptorResponse(
                module_id=name,
                version="0.1.0",
                kind="legacy_handler",
                title=name.replace("_", " ").title(),
                description=doc.strip().split("\n")[0],
                input_schema={},
                output_schema={},
                config_schema={},
                named_ports=[],
                supported_routes=["default"],
                required_credential_types=[],
                capabilities={"network": False, "streaming": False},
                effect_policy="idempotent_write",
                min_sdk_version="0.3.0",
                verification_status="mock_verified",
                is_available=True,
                missing_reason=None,
                requires_approval=is_req_appr,
                digest="",
                display=ModuleDisplayModel(label=name.replace("_", " ").title(), group="Legacy"),
            )
            if kind and desc.kind != kind:
                continue
            if query:
                q_lower = query.lower()
                if (
                    q_lower not in desc.module_id.lower()
                    and q_lower not in desc.title.lower()
                    and q_lower not in desc.description.lower()
                ):
                    continue
            descriptors.append(desc)

    descriptors.sort(key=lambda d: d.module_id)
    return ModuleListResponse(
        items=descriptors,
        total=len(descriptors),
        modules=descriptors,
    )


@router.get("/modules/{module_id}", response_model=ModuleDescriptorResponse)
async def get_module_descriptor(
    request: Request,
    module_id: str,
    version: str | None = Query(default=None, description="Specific semantic version"),
) -> ModuleDescriptorResponse:
    """Retrieve the authoritative descriptor for a specific module."""
    app_registry: ModuleRegistry | None = getattr(request.app.state, "registry", None)
    v_reg: VersionedModuleRegistry = (
        app_registry.versioned_registry
        if app_registry and app_registry.versioned_registry
        else default_registry
    )
    approval_handlers: frozenset[str] = getattr(
        request.app.state, "approval_required_handlers", frozenset()
    )

    caller_ws = getattr(request.state, "workspace_id", "default")
    ws_repo = getattr(request.app.state, "workspace_repo", None)
    if ws_repo is not None:
        owner_ws = ws_repo.get_resource_workspace("integration_tool", module_id)
        if owner_ws is not None and owner_ws != caller_ws:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "CROSS_WORKSPACE_DENIED",
                    "message": (
                        f"Integration module '{module_id}' belongs to workspace '{owner_ws}', not "
                        f"'{caller_ws}'."
                    ),
                },
            )

    try:
        entry = v_reg.get_entry(module_id, version)
        return _build_descriptor_from_entry(entry, approval_handlers)
    except ModuleNotFoundError:
        pass

    # Check legacy handlers in app_registry
    if app_registry and hasattr(app_registry, "_handlers") and module_id in app_registry._handlers:
        fn = app_registry._handlers[module_id]
        doc = inspect.getdoc(fn) or "Trusted Python workflow handler."
        is_req_appr = module_id in approval_handlers
        return ModuleDescriptorResponse(
            module_id=module_id,
            version="0.1.0",
            kind="legacy_handler",
            title=module_id.replace("_", " ").title(),
            description=doc.strip().split("\n")[0],
            input_schema={},
            output_schema={},
            config_schema={},
            named_ports=[],
            supported_routes=["default"],
            required_credential_types=[],
            capabilities={"network": False, "streaming": False},
            effect_policy="idempotent_write",
            min_sdk_version="0.3.0",
            verification_status="mock_verified",
            is_available=True,
            missing_reason=None,
            requires_approval=is_req_appr,
            digest="",
            display=ModuleDisplayModel(label=module_id.replace("_", " ").title(), group="Legacy"),
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "MODULE_NOT_FOUND", "message": f"Module '{module_id}' not found."},
    )


#: Capability flags of the service itself; extensions may add flags but not redefine these.
BUILTIN_CAPABILITIES: dict[str, bool] = {
    "modules": True,
    "validation": True,
    "export": True,
    "downloadable_export": True,
    "live_events": True,
    "run_diagnostics": True,
    "diagnostic_trace_download": True,
    "vector_storage": True,
    "sandbox_execution": True,
    "evaluations": True,
    "triggers": True,
}


@functools.cache
def _provider_capabilities() -> dict[str, dict[str, Any]]:
    """Default capabilities declared by each built-in adapter (read once, no network)."""
    from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider
    from nexura_agents.providers.fake import StreamingFakeProvider
    from nexura_agents.providers.gemini.provider import GeminiNativeProvider
    from nexura_agents.providers.openai.provider import OpenAIProvider
    from nexura_agents.providers.sarvam.provider import SarvamProvider

    adapters = {
        "openai": OpenAIProvider("gpt-4o"),
        "anthropic": AnthropicNativeProvider(),
        "gemini": GeminiNativeProvider(),
        "sarvam": SarvamProvider(),
        "fake_model_final_only": StreamingFakeProvider.final_only(),
    }
    return {
        name: {
            **dataclasses.asdict(adapter.capabilities),
            "final_only": not adapter.capabilities.can_stream,
        }
        for name, adapter in adapters.items()
    }


@router.get("/capabilities", response_model=ServiceCapabilitiesResponse)
async def get_capabilities(request: Request) -> ServiceCapabilitiesResponse:
    """Discover service-wide capability support, active auth profile, and credential types."""
    profile_mgr = getattr(request.app.state, "profile_manager", None)
    active_profile = profile_mgr.profile if profile_mgr else "local-owner"
    host = getattr(request.app.state, "extensions", None)
    extension_capabilities = host.capabilities if host is not None else {}
    # The OSS provider supports one local owner. An installed authenticating provider
    # supplies the shared profile; its presence, rather than a profile name, is evidence. The
    # open-source operator token (remote single-operator mode) authenticates the local owner
    # only, so it does not add the shared profile.
    provider = getattr(request.app.state, "auth_provider", None)
    profiles = ["local-owner"]
    if (
        provider is not None
        and provider.is_authenticating
        and getattr(provider, "single_operator", False) is not True
    ):
        profiles.append("authenticated-shared-service")

    return ServiceCapabilitiesResponse(
        sdk_version=CURRENT_SDK_VERSION,
        active_profile=active_profile,
        supported_profiles=profiles,
        capabilities={**BUILTIN_CAPABILITIES, **extension_capabilities},
        supported_credential_types=[
            "openai_api_key",
            "anthropic_api_key",
            "google_api_key",
            "sarvam_api_key",
            "generic_api_token",
        ],
        supported_export_formats=["bundle", "archive", "json"],
        provider_capabilities=_provider_capabilities(),
    )
