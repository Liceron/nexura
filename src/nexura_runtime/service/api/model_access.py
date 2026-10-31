# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Which workspaces may use the server's own model keys (P45 phase 1).

``app.state.server_model_policy`` is ``None`` (the default: every workspace may use the
server's environment keys, the open-source behaviour) or a callable ``(workspace_id) -> bool``.
The hosted edition sets it from ``NEXURA_OPERATOR_WORKSPACES`` so that a self-serve workspace
has no route to an operator key: the operator's chat models, workflow nodes that call a
provider with an environment key, environment-key embeddings and credential environment
fallbacks are refused for it, and it brings its own provider credential instead.

Fail closed: with a policy set, an unknown (``None`` or empty) workspace, a policy that raises
and any answer other than ``True`` all mean "not allowed".
"""

from __future__ import annotations

from typing import Any

SERVER_MODELS_NOT_AVAILABLE = "SERVER_MODELS_NOT_AVAILABLE"
SERVER_MODELS_MESSAGE = (
    "This workspace cannot use the server's models. Add your own provider key in "
    "Settings → AI keys and choose one of its models."
)


class ServerModelsNotAvailableError(Exception):
    """The caller's workspace may not use a model or node that spends a server key."""

    code = SERVER_MODELS_NOT_AVAILABLE

    def __init__(self, message: str = SERVER_MODELS_MESSAGE) -> None:
        super().__init__(message)
        self.message = message


def server_models_allowed(state: Any, workspace_id: str | None) -> bool:
    """True when ``workspace_id`` may use the server's environment model keys."""
    policy = getattr(state, "server_model_policy", None)
    if policy is None:
        return True
    if not isinstance(workspace_id, str) or not workspace_id:
        return False
    try:
        return policy(workspace_id) is True
    except Exception:
        return False


def uses_server_keys(module: Any) -> bool:
    """True for a workflow module that calls a model provider with a server key.

    The provider adapters (``kind="model_adapter"``) and every module that declares an
    ``api_key`` credential read their key from the server environment (``OPENAI_API_KEY``,
    ``SARVAM_API_KEY``, ...). The keyless fixture adapter ``nexura.models.provider`` is the
    one exception.
    """
    manifest = getattr(module, "manifest", None)
    if manifest is None:
        return False
    if "api_key" in (getattr(manifest, "required_credential_types", None) or []):
        return True
    return (
        getattr(manifest, "kind", None) == "model_adapter"
        and getattr(manifest, "module_id", None) not in KEYLESS_MODEL_MODULES
    )


#: Model adapters that never read a key (the offline streaming fixture).
KEYLESS_MODEL_MODULES = frozenset({"nexura.models.provider"})


def _lookup(registry: Any, name: str, version: str | None) -> Any:
    try:
        if registry.has_module(name, version):
            return registry.get_module(name, version)
        if registry.has_handler(name):
            return registry.get(name)
    except Exception:
        return None
    return None


def server_key_nodes(spec: Any, registry: Any) -> list[str]:
    """Ids of the nodes of ``spec`` (sub-workflows and parallel branches included) whose
    module calls a provider with a server key."""
    found: list[str] = []
    for node_id, node in spec.nodes.items():
        names: list[tuple[str, str | None]] = []
        if node.module_id:
            names.append((node.module_id, node.module_version))
        if node.handler:
            names.append((node.handler, None))
        names += [(name, None) for name in node.parallel]
        if any(uses_server_keys(_lookup(registry, n, v)) for n, v in names):
            found.append(str(node_id))
        if node.subgraph is not None:
            found += [f"{node_id}/{inner}" for inner in server_key_nodes(node.subgraph, registry)]
    return found


def check_workflow_models(state: Any, workspace_id: str | None, spec: Any) -> None:
    """Refuse (``ServerModelsNotAvailableError``) a workflow with server-key nodes in a
    workspace that may not use the server's keys. Used at save and at every run start."""
    if server_models_allowed(state, workspace_id):
        return
    nodes = server_key_nodes(spec, state.registry)
    if nodes:
        raise ServerModelsNotAvailableError(
            f"Node(s) {', '.join(nodes[:5])} call a model provider with the server's key, "
            "which this workspace cannot use. Use an agent node whose agent runs on your own "
            "provider key (Settings → AI keys) instead."
        )


def workflow_module_gate(state: Any, workspace_id: str | None) -> Any:
    """The ``WorkflowEngine(module_gate=...)`` of a run in ``workspace_id``: ``None`` when the
    workspace may use server keys, else a check that refuses server-key modules at the moment
    a step resolves its handler (the last line behind the start-time check)."""
    if server_models_allowed(state, workspace_id):
        return None

    def gate(name: str, module: Any) -> None:
        if uses_server_keys(module):
            raise ServerModelsNotAvailableError(
                f"'{name}' calls a model provider with the server's key, which this workspace "
                "cannot use."
            )

    return gate


__all__ = [
    "KEYLESS_MODEL_MODULES",
    "SERVER_MODELS_MESSAGE",
    "SERVER_MODELS_NOT_AVAILABLE",
    "ServerModelsNotAvailableError",
    "check_workflow_models",
    "server_key_nodes",
    "server_models_allowed",
    "uses_server_keys",
    "workflow_module_gate",
]
