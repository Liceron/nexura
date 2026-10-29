# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workspace agents API: declarative agents that Studio users build, save and run.

A saved agent is an :class:`~nexura_runtime.agents_spec.AgentSpec` (JSON, no code). Reads need
workspace membership; create and update need the ``edit`` permission; delete needs
``admin``. Resource-scoped API keys cannot reach these routes (deny-by-default in
``service/auth/resources.py``). Errors are ``{"code", "message"}`` (invalid specs add
``field_errors``).

Every name in a spec is checked against this app when the agent is saved and again each time
it runs: the model must be in the chat model allowlist, tools must be registered modules
that are not internal and not agent nodes, knowledge collections and skills must belong to
the workspace. Any tool whose module can write is forced to need approval.
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from typing import Any, NoReturn

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, status
from nexura_agents.agent import Agent
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.models.fake import FakeModel
from nexura_agents.module_tools import module_tool, tool_name
from nexura_agents.skills import SkillError
from nexura_agents.tools import Tool
from nexura_agents.types import Message, ToolCall, Usage

from nexura_runtime.agents_spec import (
    MAX_INSTRUCTIONS_CHARS,
    MAX_KNOWLEDGE_COLLECTIONS,
    MAX_SKILLS,
    MAX_SPEC_CHARS,
    MAX_TIMEOUT_SECONDS,
    MAX_TOOL_CALLS_CAP,
    MAX_TOOLS,
    MAX_TURNS_CAP,
    AgentSpec,
    AgentSpecError,
    parse_agent_spec,
    schema_example,
)
from nexura_runtime.agents_store import (
    MAX_AGENTS_PER_WORKSPACE,
    AgentRevisionConflictError,
    AgentStoreError,
)
from nexura_runtime.runtime.agents.saved import AGENT_NODE_MODULE_ID, BuiltAgent
from nexura_runtime.service.api.chat_models import ChatModelRegistry, ChatModelUnavailableError
from nexura_runtime.service.api.credentials import _enforce_perm, _get_caller_context
from nexura_runtime.service.api.guardrails import caller_is_admin, policy_view, preset_catalog
from nexura_runtime.service.api.knowledge import _workspace_collections
from nexura_runtime.service.api.sessions import (
    _knowledge_reply,
    knowledge_search_tool,
    looks_like_document_question,
)
from nexura_runtime.service.storage_protocols import AgentBackend

router = APIRouter(prefix="/agents", tags=["agents"])

OFFLINE_REPLY_PREFIX = "Offline fake reply from agent"


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    return {str(t.get("name") or (t.get("function") or {}).get("name") or "") for t in tools or []}


def offline_agent_model(spec: AgentSpec) -> FakeModel:
    """The offline fixture a saved agent runs on when its model is ``fake-deterministic``.

    Deterministic and keyless: it searches knowledge for document questions when the agent
    has collections, summarises a tool result, and otherwise echoes the input. With an
    ``output_schema`` it answers with a minimal JSON value matching the schema. It never
    calls the agent's other tools.
    """
    example = schema_example(spec.output_schema) if spec.output_schema is not None else None

    def handler(messages: list[Message], tools: list[dict[str, Any]] | None) -> Any:
        last = messages[-1] if messages else None
        content = str(last.content or "") if last is not None else ""
        if last is not None and last.role == "tool":
            text = _knowledge_reply(content, "en") or f"Tool result: {content[:500]}"
        else:
            user = content.strip()
            if "knowledge_search" in _tool_names(tools) and looks_like_document_question(user):
                return [
                    ToolCall(
                        id=f"call_{uuid.uuid4().hex[:8]}",
                        name="knowledge_search",
                        arguments={"query": user},
                    )
                ]
            text = f"{OFFLINE_REPLY_PREFIX} '{spec.name}': {user[:300]}"
        return json.dumps(example, ensure_ascii=False) if example is not None else text

    return FakeModel(
        handler=handler,
        default_usage=Usage(prompt_tokens=18, completion_tokens=32, total_tokens=50),
    )


class AgentRuntime:
    """Checks saved specs against one app and builds runnable agents from them."""

    def __init__(self, app: FastAPI, store: AgentBackend) -> None:
        self._app = app
        self.store = store
        # Knowledge helpers read ``.app.state``; outside a request this stands in for one.
        self._holder = SimpleNamespace(app=app)

    @property
    def _state(self) -> Any:
        return self._app.state

    # ------------------------------------------------------------------ tools

    def _module(self, workspace_id: str, module_id: str) -> BaseModule | None:
        """The module behind ``module_id`` if this workspace may use it, else None."""
        state = self._state
        ws_repo = getattr(state, "workspace_repo", None)
        if ws_repo is not None:
            owner = ws_repo.get_resource_workspace("integration_tool", module_id)
            if owner is not None and owner != workspace_id:
                return None
        registry = state.registry
        try:
            if registry.has_module(module_id):
                found = registry.get_module(module_id)
            elif registry.has_handler(module_id):
                found = registry.get(module_id)
            else:
                return None
        except Exception:
            return None
        if not isinstance(found, BaseModule):
            return None  # plain Python handlers have no schema to offer a model
        owner_ws = getattr(found, "_workspace_id", None)
        if owner_ws is not None and owner_ws != workspace_id:
            return None
        return found

    def _available(self, module_id: str) -> bool:
        versioned = getattr(self._state.registry, "versioned_registry", None)
        if versioned is None or not versioned.has(module_id, None):
            return True
        try:
            return bool(versioned.get_entry(module_id, None).is_available)
        except Exception:
            return False

    def requires_approval(self, module: BaseModule) -> bool:
        """True for any module that can write (or that the server gates): calls need approval."""
        manifest = module.manifest
        gated: frozenset[str] = getattr(self._state, "approval_required_handlers", frozenset())
        return bool(
            manifest.write_operations
            or manifest.always_write
            or getattr(manifest, "requires_approval", False)
            or manifest.module_id in gated
            or manifest.effect_policy not in ("pure", "read")
        )

    def _tool_problem(self, workspace_id: str, module_id: str) -> str | None:
        module = self._module(workspace_id, module_id)
        if module is None:
            return f"Unknown tool module '{module_id}'."
        manifest = module.manifest
        if getattr(manifest, "internal", False):
            return f"'{module_id}' is a service-internal module and cannot be a tool."
        if manifest.kind == "agent":
            return f"'{module_id}' is an agent node; agents cannot be tools of an agent."
        # An agent's model comes only from its ``model`` field (the operator's allowlist). A
        # model adapter or any module that calls a provider with the server's API key would
        # let the model pick an unlisted (paid) model or send the key to a URL it chooses.
        if manifest.kind == "model_adapter" or "api_key" in (
            getattr(manifest, "required_credential_types", None) or []
        ):
            return (
                f"'{module_id}' calls a model provider with a server API key and cannot be a "
                "tool; choose the agent's model in its 'model' field instead."
            )
        if not self._available(module_id):
            return f"'{module_id}' is not available on this server."
        return None

    def tool_policies(self, workspace_id: str, spec: AgentSpec) -> list[dict[str, Any]]:
        """Per tool: the name the model sees and whether its calls need approval."""
        out = []
        for module_id in spec.tools:
            module = self._module(workspace_id, module_id)
            out.append(
                {
                    "module_id": module_id,
                    "tool_name": tool_name(module_id),
                    "available": module is not None
                    and self._tool_problem(workspace_id, module_id) is None,
                    "requires_approval": True if module is None else self.requires_approval(module),
                }
            )
        return out

    def approval_tools(self, workspace_id: str, module_ids: list[str]) -> list[str]:
        """The module ids among ``module_ids`` whose calls need approval (unknown ones do)."""
        out = []
        for module_id in module_ids:
            module = self._module(workspace_id, module_id)
            if module is None or self.requires_approval(module):
                out.append(module_id)
        return out

    def tool_options(self, workspace_id: str) -> list[dict[str, Any]]:
        """Modules this workspace can attach as tools, with their approval policy."""
        registry = self._state.registry
        ids: list[str] = []
        versioned = getattr(registry, "versioned_registry", None)
        if versioned is not None:
            ids += [entry["module_id"] for entry in versioned.list_modules()]
        handlers = getattr(registry, "_handlers", {})
        ids += [name for name, h in handlers.items() if hasattr(h, "manifest")]
        out = []
        for module_id in sorted(dict.fromkeys(ids)):
            if self._tool_problem(workspace_id, module_id) is not None:
                continue
            module = self._module(workspace_id, module_id)
            assert module is not None
            manifest = module.manifest
            out.append(
                {
                    "module_id": module_id,
                    "tool_name": tool_name(module_id),
                    "title": manifest.title,
                    "description": manifest.description,
                    "kind": manifest.kind,
                    "requires_approval": self.requires_approval(module),
                    "effect_policy": manifest.effect_policy,
                    "effect_class": _effect_class(manifest.effect_policy),
                }
            )
        return out

    # ----------------------------------------------------------------- checks

    def _models(self) -> ChatModelRegistry:
        registry: ChatModelRegistry = self._state.chat_models
        return registry

    def _skill_names(self, workspace_id: str) -> set[str] | None:
        skills = getattr(self._state, "skills_store", None)
        if skills is None:
            return None
        return {item["name"] for item in skills.list_skills(workspace_id)}

    def check(self, workspace_id: str, spec: AgentSpec) -> None:
        """Refuse names that do not exist for this workspace (raises AgentSpecError)."""
        errors: list[dict[str, str]] = []
        models = self._models()
        if spec.model not in models:
            errors.append(
                {
                    "field": "model",
                    "message": f"Unknown model '{spec.model}'. Allowed models: "
                    f"{', '.join(models.names)}.",
                }
            )
        for index, module_id in enumerate(spec.tools):
            problem = self._tool_problem(workspace_id, module_id)
            if problem:
                errors.append({"field": f"tools.{index}", "message": problem})
        ws_repo = getattr(self._state, "workspace_repo", None)
        collections = set(_workspace_collections(ws_repo, workspace_id)) if ws_repo else {"default"}
        for index, collection_id in enumerate(spec.knowledge):
            if collection_id not in collections:
                errors.append(
                    {
                        "field": f"knowledge.{index}",
                        "message": f"Unknown knowledge collection '{collection_id}'.",
                    }
                )
        if spec.skills:
            names = self._skill_names(workspace_id)
            for index, skill in enumerate(spec.skills):
                if names is None or skill not in names:
                    errors.append(
                        {"field": f"skills.{index}", "message": f"Unknown skill '{skill}'."}
                    )
        # P39 G6: presets this server can build (judge models from the allowlist).
        from nexura_runtime.service.api.guardrails import check_agent_guardrails

        errors.extend(
            check_agent_guardrails(
                spec.guardrails,
                models,
                spec.model,
                policy=getattr(self._state, "guardrail_policy", None),
                agent_name=spec.name,
            )
        )
        if errors:
            shown = "; ".join(e["message"] for e in errors[:5])
            code = (
                "MODEL_NOT_AVAILABLE"
                if [e["field"] for e in errors] == ["model"]
                else "AGENT_INVALID"
            )
            raise AgentSpecError(f"Invalid agent: {shown}", errors, code=code)

    # ------------------------------------------------------------------ build

    def resolve_model(self, spec: AgentSpec) -> Any | None:
        """The configured model instance, or None for the offline fixture."""
        models = self._models()
        if spec.model not in models:
            raise AgentSpecError(
                f"Model '{spec.model}' is not available on this server.",
                [{"field": "model", "message": f"Unknown model '{spec.model}'."}],
                code="MODEL_NOT_AVAILABLE",
            )
        if models.is_fake(spec.model):
            return None
        try:
            return models.resolve(spec.model)
        except ChatModelUnavailableError as exc:
            raise AgentSpecError(str(exc), code="MODEL_UNAVAILABLE") from None

    def build_agent(
        self, workspace_id: str, agent_id: str, context: ExecutionContext | None
    ) -> BuiltAgent:
        """Build ``agent_id`` of ``workspace_id`` (other workspaces' ids are not found)."""
        spec, revision = self.store.get_spec(workspace_id, agent_id)
        return self.build_from_spec(workspace_id, agent_id, revision, spec, context)

    def build_from_spec(
        self,
        workspace_id: str,
        agent_id: str,
        revision: int,
        spec: AgentSpec,
        context: ExecutionContext | None,
        *,
        replace_approval_tools: Any = None,
    ) -> BuiltAgent:
        """An ``Agent`` for ``spec``; ``replace_approval_tools(name)`` swaps write tools."""
        self.check(workspace_id, spec)
        model = self.resolve_model(spec)
        tools: list[Tool] = []
        gated: list[str] = []
        for module_id in spec.tools:
            module = self._module(workspace_id, module_id)
            assert module is not None  # check() refused missing modules
            name = tool_name(module_id)
            needs = self.requires_approval(module)
            if needs:
                gated.append(name)
            if needs and replace_approval_tools is not None:
                tools.append(replace_approval_tools(name))
                continue
            tools.append(
                module_tool(
                    module,
                    name=name,
                    # A write can never run without the approval flow.
                    requires_approval=bool(needs),
                    context=context,
                )
            )
        if spec.knowledge:
            tools.append(knowledge_search_tool(self._holder, workspace_id, spec.knowledge))
        instructions = spec.instructions or f"You are the '{spec.name}' agent."
        from nexura_runtime.service.api.guardrails import build_agent_guardrails

        guards = build_agent_guardrails(spec.guardrails, self._models(), spec.model)
        if spec.skills:
            skills = getattr(self._state, "skills_store", None)
            if skills is None:
                raise AgentSpecError("This agent lists skills but no skill store is connected.")
            try:
                library = skills.library_for(workspace_id, spec.skills, spec.skills_label)
            except SkillError as exc:
                raise AgentSpecError(str(exc), code="SKILL_NOT_FOUND") from None
            instructions = f"{instructions}\n\n{library.catalog(label=spec.skills_label)}"
            tools.append(
                library.loader_tool(
                    label=spec.skills_label, available_tools=[t.name for t in tools]
                )
            )
        if guards.tool_input or guards.tool_output:
            # Preset tool checks go on every tool of this agent (built here, never shared), so
            # they also apply when the agent runs in a workflow node.
            for item in tools:
                item.input_guardrails = (*item.input_guardrails, *guards.tool_input)
                item.output_guardrails = (*item.output_guardrails, *guards.tool_output)
        agent: Agent[Any] = Agent(
            name=spec.name,
            instructions=instructions,
            model=model if model is not None else offline_agent_model(spec),
            tools=tools,
            max_turns=spec.limits.max_turns,
            max_tool_calls=spec.limits.max_tool_calls,
            timeout=spec.limits.timeout_seconds,
            output_type=copy.deepcopy(spec.output_schema),
            description=spec.description,
            input_guardrails=list(guards.input),
            output_guardrails=list(guards.output),
            stream_guard=guards.stream_guard,
        )
        return BuiltAgent(
            agent=agent,
            agent_id=agent_id,
            revision=revision,
            name=spec.name,
            approval_tools=tuple(gated),
            timeout=spec.limits.timeout_seconds,
        )


def _effect_class(policy: str | None) -> str | None:
    """The durable effect class of a module's effect policy (``pure``, ``idempotent``,
    ``non_idempotent``), or None for an unknown spelling."""
    from nexura_agents.contracts.execution import effect_class_from_policy

    try:
        return str(effect_class_from_policy(policy).value)
    except ValueError:
        return None


# ------------------------------------------------------------------ routes


def runtime_for(request: Request) -> AgentRuntime:
    """The app's agent runtime."""
    runtime: AgentRuntime = request.app.state.agent_runtime
    return runtime


def _fail(exc: AgentSpecError | AgentStoreError) -> NoReturn:
    if isinstance(exc, AgentSpecError):
        detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.field_errors:
            detail["field_errors"] = exc.field_errors
        raise HTTPException(status_code=422, detail=detail) from exc
    detail = {"code": exc.code, "message": exc.message}
    if isinstance(exc, AgentRevisionConflictError):
        detail["current_revision"] = exc.current_revision
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _view(runtime: AgentRuntime, workspace_id: str, record: dict[str, Any]) -> dict[str, Any]:
    spec = AgentSpec.model_validate(record["spec"])
    return {**record, "tools": runtime.tool_policies(workspace_id, spec)}


def _limits() -> dict[str, Any]:
    return {
        "max_agents_per_workspace": MAX_AGENTS_PER_WORKSPACE,
        "max_spec_chars": MAX_SPEC_CHARS,
        "max_instructions_chars": MAX_INSTRUCTIONS_CHARS,
        "max_tools": MAX_TOOLS,
        "max_knowledge_collections": MAX_KNOWLEDGE_COLLECTIONS,
        "max_skills": MAX_SKILLS,
        "max_turns": MAX_TURNS_CAP,
        "max_tool_calls": MAX_TOOL_CALLS_CAP,
        "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
    }


@router.get("")
async def list_agents(request: Request) -> dict[str, Any]:
    """The workspace's saved agents (summaries), by name, with the tools that need approval."""
    workspace_id, _, _ = _get_caller_context(request)
    runtime = runtime_for(request)
    items = runtime.store.list_agents(workspace_id)
    for item in items:
        item["approval_tools"] = runtime.approval_tools(workspace_id, item["tools"])
    return {"items": items, "total": len(items), "limits": _limits()}


@router.get("/options")
async def agent_options(request: Request) -> dict[str, Any]:
    """What a spec may name in this workspace: models, tools, collections, skills, limits."""
    workspace_id, _, _ = _get_caller_context(request)
    runtime = runtime_for(request)
    ws_repo = getattr(request.app.state, "workspace_repo", None)
    skills = runtime._skill_names(workspace_id)
    return {
        "models": runtime._models().describe(),
        "tools": runtime.tool_options(workspace_id),
        "knowledge_collections": (
            _workspace_collections(ws_repo, workspace_id) if ws_repo else ["default"]
        ),
        "skills": sorted(skills or ()),
        "node_module_id": AGENT_NODE_MODULE_ID,
        "limits": _limits(),
        # P39 G6: presets a spec may name, and the operator's enforced policy (read-only).
        "guardrails": {
            **preset_catalog(),
            "policy": policy_view(
                getattr(request.app.state, "guardrail_policy", None),
                admin=caller_is_admin(request),
            ),
        },
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Save a new agent (revision 1) after checking every name against this workspace."""
    workspace_id, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")
    runtime = runtime_for(request)
    try:
        spec = parse_agent_spec(payload)
        runtime.check(workspace_id, spec)
        record = runtime.store.create(workspace_id, spec, actor=principal.principal_id)
    except (AgentSpecError, AgentStoreError) as exc:
        _fail(exc)
    return _view(runtime, workspace_id, record)


@router.get("/{agent_id}")
async def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """One agent with its spec, revision and per-tool approval policy."""
    workspace_id, _, _ = _get_caller_context(request)
    runtime = runtime_for(request)
    try:
        record = runtime.store.get(workspace_id, agent_id)
    except AgentStoreError as exc:
        _fail(exc)
    return _view(runtime, workspace_id, record)


@router.put("/{agent_id}")
async def update_agent(
    request: Request, agent_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Replace the spec. The body is the spec plus ``expected_revision`` (409 if stale)."""
    workspace_id, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")
    body = dict(payload)
    expected = body.pop("expected_revision", None)
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "EXPECTED_REVISION_REQUIRED",
                "message": "Send expected_revision: the revision you loaded (an integer >= 1).",
            },
        )
    runtime = runtime_for(request)
    try:
        runtime.store.get(workspace_id, agent_id)  # 404 before validation errors
        spec = parse_agent_spec(body)
        runtime.check(workspace_id, spec)
        record = runtime.store.update(
            workspace_id,
            agent_id,
            spec,
            expected_revision=expected,
            actor=principal.principal_id,
        )
    except (AgentSpecError, AgentStoreError) as exc:
        _fail(exc)
    return _view(runtime, workspace_id, record)


@router.delete("/{agent_id}")
async def delete_agent(
    request: Request,
    agent_id: str,
    expected_revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Delete an agent (admin). Workflows that name it fail when they next run it."""
    workspace_id, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "admin")
    try:
        runtime_for(request).store.delete(
            workspace_id, agent_id, expected_revision=expected_revision
        )
    except AgentStoreError as exc:
        _fail(exc)
    return {"deleted": True, "agent_id": agent_id}


def mount_agents(app: FastAPI, api_v1: APIRouter) -> None:
    """Create the agent store on the service database, bind agent nodes and mount routes."""
    store = app.state.agent_store
    runtime = AgentRuntime(app, store)
    app.state.agent_store = store
    app.state.agent_runtime = runtime
    registry = app.state.registry
    for handler in list(getattr(registry, "_handlers", {}).values()):
        if hasattr(handler, "bind_agent_source") and getattr(handler, "agent_source", None) is None:
            handler.bind_agent_source(runtime)
    # The shared nexura.agents.agent module is process-wide: bind an app-local copy instead,
    # so each app reads its own store.
    versioned = getattr(registry, "versioned_registry", None)
    if versioned is not None and hasattr(registry, "override_module"):
        for entry in versioned.list_modules():
            if not entry["is_available"]:
                continue
            module = versioned.get(entry["module_id"], entry["version"])
            if hasattr(module, "bind_agent_source") and module.agent_source is None:
                local = copy.copy(module)
                local.bind_agent_source(runtime)
                registry.override_module(local)
    api_v1.include_router(router)


__all__ = ["AgentRuntime", "mount_agents", "offline_agent_model", "router", "runtime_for"]
