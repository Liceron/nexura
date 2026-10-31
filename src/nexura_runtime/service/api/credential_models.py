# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Chat models of a workspace: the operator's allowlist plus the workspace's own provider keys.

A workspace credential whose provider is ``openai``, ``anthropic``, ``gemini``/``google`` or
``sarvam`` names chat models ``cred:<credential_id>/<model_id>`` (bring your own key, P45).
The model is built with the credential's stored secret only (never an environment fallback)
and is billed to the key owner's provider account. Built models are cached per workspace,
credential, secret digest and model id, so a replaced or revoked key is never reused and
workspaces never share an instance.

:class:`WorkspaceChatModels` is what the chat, agent, guardrail, builder and red-team routes
use instead of ``app.state.chat_models``: the same interface, scoped to one workspace. When
``app.state.server_model_policy`` refuses the workspace (``model_access``), the operator's
configured models are hidden from it and refused on use; the offline fixture always stays.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections import OrderedDict
from typing import Any

from nexura_runtime.credentials.errors import CredentialAccessDeniedError, CredentialNotFoundError
from nexura_runtime.service.api.chat_models import (
    _STREAMING_PROVIDERS,
    DEFAULT_CHAT_MODEL,
    ChatModelRegistry,
)
from nexura_runtime.service.api.model_access import (
    ServerModelsNotAvailableError,
    server_models_allowed,
)

CREDENTIAL_MODEL_PREFIX = "cred:"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
#: The credential id rule of ``POST /credentials`` (and of the hosted edition's secret names).
CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
#: Suggested model ids per provider (listing only; any well-formed id resolves).
SUGGESTED_MODELS: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"),
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
    "gemini": ("gemini-3.5-flash-lite", "gemini-2.5-flash"),
    "google": ("gemini-3.5-flash-lite", "gemini-2.5-flash"),
    "sarvam": ("sarvam-m",),
}
MODEL_PROVIDERS = frozenset(SUGGESTED_MODELS)
#: Built models kept per app (least recently used first out).
MAX_CACHED_MODELS = 256

# Cache keys hold a keyed digest of the secret, never the secret; the key lives in memory only.
_DIGEST_KEY = secrets.token_bytes(32)


class ModelAccessError(Exception):
    """A model name the caller may not use; ``code`` and ``message`` are safe to return."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def parse_credential_model(name: object) -> tuple[str, str] | None:
    """``(credential_id, model_id)`` of a well-formed ``cred:<id>/<model>`` name, else None."""
    if not isinstance(name, str) or not name.startswith(CREDENTIAL_MODEL_PREFIX):
        return None
    credential_id, sep, model_id = name[len(CREDENTIAL_MODEL_PREFIX) :].partition("/")
    if not sep or not CREDENTIAL_ID_RE.match(credential_id) or not MODEL_ID_RE.match(model_id):
        return None
    return credential_id, model_id


def credential_model_name(credential_id: str, model_id: str) -> str:
    return f"{CREDENTIAL_MODEL_PREFIX}{credential_id}/{model_id}"


def _label(provider: str, credential_id: str, model_id: str) -> str:
    """Display label: model, provider and the credential's name; never key material."""
    return f"{model_id} ({provider}, your key '{credential_id}')"


def _visible(state: Any, workspace_id: str, credential_id: str) -> bool:
    """False when a legacy ownership record gives the id to another workspace."""
    repo = getattr(state, "workspace_repo", None)
    if repo is None:
        return True
    owner = repo.get_resource_workspace("credential", credential_id)
    return not owner or owner == workspace_id


class CredentialModelCache:
    """Built BYOK models of one app, keyed by workspace, credential, key digest and model."""

    def __init__(self, max_entries: int = MAX_CACHED_MODELS) -> None:
        self._max = max_entries
        self._models: OrderedDict[tuple[str, str, str, str, str], Any] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._models)

    def forget_workspace(self, workspace_id: str) -> int:
        """Drop every model built for ``workspace_id`` (workspace deletion); returns how many."""
        with self._lock:
            doomed = [key for key in self._models if key[0] == workspace_id]
            for key in doomed:
                del self._models[key]
            return len(doomed)

    def get_or_build(
        self,
        key: tuple[str, str, str, str, str],
        build: Any,
    ) -> Any:
        with self._lock:
            found = self._models.get(key)
            if found is not None:
                self._models.move_to_end(key)
                return found
        model = build()
        with self._lock:
            self._models[key] = model
            self._models.move_to_end(key)
            # Entries of an older digest of the same credential and model are dead (replaced).
            stale = [k for k in self._models if k[:2] == key[:2] and k[4] == key[4] and k != key]
            for old in stale:
                del self._models[old]
            while len(self._models) > self._max:
                self._models.popitem(last=False)
        return model


def _cache(state: Any) -> CredentialModelCache:
    cache = getattr(state, "credential_model_cache", None)
    if cache is None:
        cache = CredentialModelCache()
        state.credential_model_cache = cache
    return cache


def _store(state: Any) -> Any:
    return getattr(state, "credential_store", None)


def model_credentials(state: Any, workspace_id: str) -> list[Any]:
    """Active credentials of the workspace whose provider can serve chat models."""
    store = _store(state)
    if store is None or not workspace_id:
        return []
    return [
        meta
        for meta in store.list_credentials(workspace_id)
        if meta.status == "active"
        and meta.provider in MODEL_PROVIDERS
        and _visible(state, workspace_id, meta.credential_id)
    ]


def credential_provider(state: Any, workspace_id: str, credential_id: str) -> str | None:
    """The model provider of the workspace's credential, or None (missing, not a model
    provider, or owned by another workspace)."""
    store = _store(state)
    if store is None or not workspace_id or not _visible(state, workspace_id, credential_id):
        return None
    try:
        meta = store.get_metadata(workspace_id, credential_id)
    except CredentialNotFoundError:
        return None
    return meta.provider if meta.provider in MODEL_PROVIDERS else None


def describe_credential_models(state: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Listing entries for the suggested models of each usable credential."""
    out: list[dict[str, Any]] = []
    for meta in model_credentials(state, workspace_id):
        for model_id in SUGGESTED_MODELS[meta.provider]:
            out.append(
                {
                    "name": credential_model_name(meta.credential_id, model_id),
                    "provider": meta.provider,
                    "label": _label(meta.provider, meta.credential_id, model_id),
                    "offline": False,
                    "streaming": meta.provider in _STREAMING_PROVIDERS,
                    "default": False,
                    "credential_id": meta.credential_id,
                }
            )
    return out


def resolve_credential_model(state: Any, workspace_id: str, name: str) -> Any:
    """Build (or reuse) the model ``name`` with the workspace's stored key.

    Raises :class:`ModelAccessError`; messages name the credential, never its secret.
    """
    parsed = parse_credential_model(name)
    if parsed is None:
        raise ModelAccessError(
            "MODEL_NOT_AVAILABLE",
            f"'{name}' is not a valid model name; use cred:<credential>/<model id>.",
        )
    credential_id, model_id = parsed
    store = _store(state)
    missing = ModelAccessError(
        "CREDENTIAL_NOT_FOUND",
        f"Credential '{credential_id}' was not found in this workspace.",
    )
    if store is None or not workspace_id or not _visible(state, workspace_id, credential_id):
        raise missing
    try:
        meta = store.get_metadata(workspace_id, credential_id)
        # The stored secret only: ``resolve`` never reads an environment fallback.
        secret = store.resolve(workspace_id, credential_id).reveal()
    except CredentialNotFoundError:
        raise missing from None
    except CredentialAccessDeniedError:
        raise ModelAccessError(
            "CREDENTIAL_UNUSABLE", f"Credential '{credential_id}' has been revoked."
        ) from None
    if meta.status != "active":
        raise ModelAccessError(
            "CREDENTIAL_UNUSABLE", f"Credential '{credential_id}' is not active."
        )
    if meta.provider not in MODEL_PROVIDERS:
        raise ModelAccessError(
            "CREDENTIAL_NOT_A_MODEL_PROVIDER",
            f"Credential '{credential_id}' is for '{meta.provider}', which is not a model "
            "provider (openai, anthropic, gemini or sarvam).",
        )
    if not secret.strip():
        # An empty key would make the provider fall back to the server environment.
        raise ModelAccessError(
            "CREDENTIAL_UNUSABLE", f"Credential '{credential_id}' has an empty value."
        )
    digest = hmac.new(
        _DIGEST_KEY, f"{meta.provider}\0{secret}".encode(), hashlib.sha256
    ).hexdigest()
    provider = meta.provider

    def build() -> Any:
        # Looked up at call time so tests can replace the builder's factory with a fake.
        from nexura_runtime.service.api import builder

        try:
            return builder.build_model_from_credential(provider, model_id, secret)
        except Exception as exc:
            raise ModelAccessError(
                "MODEL_UNAVAILABLE",
                f"Could not set up the {provider} model '{model_id}' ({type(exc).__name__}).",
                503,
            ) from None

    return _cache(state).get_or_build(
        (workspace_id, credential_id, digest, provider, model_id), build
    )


class WorkspaceChatModels:
    """``ChatModelRegistry``'s interface for one workspace (see the module docstring)."""

    def __init__(self, state: Any, workspace_id: str | None) -> None:
        self._state = state
        self._registry: ChatModelRegistry = (
            getattr(state, "chat_models", None) or ChatModelRegistry()
        )
        self.workspace_id = workspace_id or ""
        self.server_allowed = server_models_allowed(state, workspace_id)

    def _server(self, name: str) -> bool:
        return name in self._registry and not self._registry.is_fake(name)

    def refused(self, name: object) -> bool:
        """True for an operator model this workspace may not use."""
        return isinstance(name, str) and not self.server_allowed and self._server(name)

    @property
    def names(self) -> list[str]:
        """Operator names this workspace may use (the fixture first); credential models are
        listed by :meth:`describe`."""
        return [n for n in self._registry.names if not self._server(n) or self.server_allowed]

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        if name in self._registry:
            return self._registry.is_fake(name) or self.server_allowed
        parsed = parse_credential_model(name)
        return parsed is not None and (
            credential_provider(self._state, self.workspace_id, parsed[0]) is not None
        )

    def is_fake(self, name: str) -> bool:
        return name == DEFAULT_CHAT_MODEL

    def is_credential_model(self, name: str) -> bool:
        return name not in self._registry and parse_credential_model(name) is not None

    def provider(self, name: str) -> str:
        if name in self._registry:
            return self._registry.provider(name)
        parsed = parse_credential_model(name)
        found = credential_provider(self._state, self.workspace_id, parsed[0]) if parsed else None
        return found or "unknown"

    def display(self, name: str) -> str:
        if name in self._registry:
            return self._registry.display(name)
        parsed = parse_credential_model(name)
        if parsed is None:
            return name
        return _label(self.provider(name), parsed[0], parsed[1])

    def resolve(self, name: str) -> Any:
        """The model instance. Raises ``ServerModelsNotAvailableError`` for a refused operator
        model, ``ModelAccessError`` for a credential model that cannot be used, and
        ``ChatModelUnavailableError`` as the operator registry does."""
        if self.refused(name):
            raise ServerModelsNotAvailableError()
        if name in self._registry:
            return self._registry.resolve(name)
        return resolve_credential_model(self._state, self.workspace_id, name)

    def describe(self) -> list[dict[str, Any]]:
        allowed = set(self.names)
        listed = [e for e in self._registry.describe() if e["name"] in allowed]
        return listed + describe_credential_models(self._state, self.workspace_id)


def workspace_chat_models(state: Any, workspace_id: str | None) -> WorkspaceChatModels:
    return WorkspaceChatModels(state, workspace_id)


def _agent_ids(spec: Any) -> list[str]:
    """Saved agents named by the agent nodes of ``spec`` (sub-workflows included)."""
    from nexura_runtime.runtime.agents.saved import AGENT_NODE_MODULE_ID

    found: list[str] = []
    for node in spec.nodes.values():
        if AGENT_NODE_MODULE_ID in (node.module_id, node.handler):
            agent_id = (node.config or {}).get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                found.append(agent_id)
        if node.subgraph is not None:
            found += _agent_ids(node.subgraph)
    return found


def unpriced_credential_models(state: Any, workspace_id: str, spec: Any) -> list[str]:
    """``cred:`` models (agent models and judge presets) that the saved agents of ``spec``'s
    agent nodes run on and that the server's price list does not know.

    Pricing rule (P45): a credential model is billed to its key owner's provider account. A
    monetary cap (a publication's daily limit) can only be enforced for priced models, and a
    capped run refuses an unpriced model call; publishing routes use this to refuse such a cap
    up front (``SPEND_CAP_NEEDS_PRICED_MODEL``) instead of failing visitors' chats. Uncapped
    runs (the playground, agent runs, publications without a cap) need no price.
    """
    runtime = getattr(state, "agent_runtime", None)
    table = getattr(state, "pricing_table", None)
    if runtime is None:
        return []
    names: list[str] = []
    for agent_id in dict.fromkeys(_agent_ids(spec)):
        try:
            agent_spec, _ = runtime.store.get_spec(workspace_id, agent_id)
        except Exception:
            continue  # a missing agent fails the run on its own, with its own error
        names.append(agent_spec.model)
        guardrails = getattr(agent_spec, "guardrails", None)
        for ref in getattr(guardrails, "presets", None) or []:
            if ref.preset == "judge":
                names.append(ref.model or agent_spec.model)
    out: list[str] = []
    for name in dict.fromkeys(names):
        parsed = parse_credential_model(name)
        if parsed is None:
            continue
        if table is None or table.get_pricing(parsed[1]) is None:
            out.append(name)
    return out


__all__ = [
    "CREDENTIAL_MODEL_PREFIX",
    "MODEL_PROVIDERS",
    "SUGGESTED_MODELS",
    "CredentialModelCache",
    "ModelAccessError",
    "WorkspaceChatModels",
    "credential_model_name",
    "unpriced_credential_models",
    "describe_credential_models",
    "model_credentials",
    "parse_credential_model",
    "resolve_credential_model",
    "workspace_chat_models",
]
