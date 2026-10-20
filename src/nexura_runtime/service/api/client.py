# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Asynchronous and synchronous Python HTTP client for the Nexura Service API."""

from __future__ import annotations

import builtins
import json
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any, Self, cast

import httpx


class NexuraApiError(Exception):
    """Exception raised when an API request returns an error response."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"[{status_code}] {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


#: Header and error codes of the local server's per-session token (see ``local_session``).
SESSION_HEADER = "X-Nexura-Session"
_SESSION_TOKEN_CODES = frozenset({"SESSION_TOKEN_REQUIRED", "SESSION_TOKEN_INVALID"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _session_token_refusal(response: httpx.Response) -> bool:
    """True when the server refused the request for a missing or stale session token."""
    if response.status_code != 403:
        return False
    try:
        detail = response.json().get("detail")
    except Exception:
        return False
    return isinstance(detail, dict) and detail.get("code") in _SESSION_TOKEN_CODES


def _safe_quote(val: Any) -> str:
    """URL-encode a path parameter strictly with no safe characters."""
    return urllib.parse.quote(str(val), safe="")


class WorkflowsClient:
    """Client for workflow specification CRUD, import, and export operations."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(
        self, limit: int = 20, offset: int = 0, query: str | None = None
    ) -> dict[str, Any]:
        """List workflows with pagination and optional search filter."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if query is not None:
            params["query"] = query
        return await self._root._request("GET", "/workflows", params=params)

    async def create(
        self,
        spec: dict[str, Any],
        workflow_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Create and persist a new workflow specification."""
        payload: dict[str, Any] = {"spec": spec}
        if workflow_id:
            payload["workflow_id"] = workflow_id
        if name:
            payload["name"] = name
        return await self._root._request("POST", "/workflows", json_data=payload)

    async def get(self, workflow_id: str) -> dict[str, Any]:
        """Retrieve a workflow by ID."""
        return await self._root._request("GET", f"/workflows/{_safe_quote(workflow_id)}")

    async def update(
        self, workflow_id: str, spec: dict[str, Any], expected_revision: int
    ) -> dict[str, Any]:
        """Update a workflow specification with CAS revision check."""
        payload = {"expected_revision": expected_revision, "spec": spec}
        return await self._root._request(
            "PUT", f"/workflows/{_safe_quote(workflow_id)}", json_data=payload
        )

    async def delete(self, workflow_id: str) -> dict[str, Any]:
        """Delete a workflow specification."""
        return await self._root._request("DELETE", f"/workflows/{_safe_quote(workflow_id)}")

    async def export(self, workflow_id: str, target_dir: str | None = None) -> dict[str, Any]:
        """Export workflow specification to a portable standalone bundle."""
        payload: dict[str, Any] = {}
        if target_dir:
            payload["target_dir"] = target_dir
        path = f"/workflows/{_safe_quote(workflow_id)}/export"
        return await self._root._request("POST", path, json_data=payload)

    async def validate(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Validate a workflow specification graph against registered schemas and policies."""
        return await self._root._request("POST", "/workflows/validate", json_data={"spec": spec})

    async def download_export(self, workflow_id: str) -> bytes:
        """Download portable workflow bundle as raw zip bytes."""
        client = await self._root._get_client()
        url = f"{self._root.base_url}/api/v1/workflows/{_safe_quote(workflow_id)}/export/download"
        headers = self._root._get_headers()
        res = await client.get(url, headers=headers)
        if res.status_code >= 400:
            raise NexuraApiError(res.status_code, "DOWNLOAD_ERROR", res.text)
        return res.content

    async def export_python(self, workflow_id: str) -> dict[str, Any]:
        """Export workflow specification as readable Python source."""
        return await self._root._request(
            "GET", f"/workflows/{_safe_quote(workflow_id)}/export/python"
        )

    async def import_python(
        self, source: str, name: str | None = None, workflow_id: str | None = None
    ) -> dict[str, Any]:
        """Import a workflow generated from Python source."""
        payload: dict[str, Any] = {"source": source}
        if name is not None:
            payload["name"] = name
        if workflow_id is not None:
            payload["workflow_id"] = workflow_id
        return await self._root._request("POST", "/workflows/import/python", json_data=payload)

    async def import_bundle(
        self,
        archive_base64: str | None = None,
        spec: dict[str, Any] | None = None,
        spec_json: str | None = None,
        name: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Import a standalone workflow bundle or specification into caller's workspace."""
        payload: dict[str, Any] = {}
        if archive_base64 is not None:
            payload["archive_base64"] = archive_base64
        if spec is not None:
            payload["spec"] = spec
        if spec_json is not None:
            payload["spec_json"] = spec_json
        if name is not None:
            payload["name"] = name
        if workflow_id is not None:
            payload["workflow_id"] = workflow_id
        return await self._root._request("POST", "/workflows/import", json_data=payload)


class RunsClient:
    """Client for run execution, cancellation, approvals, history, forks, and SSE streaming."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(
        self,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List execution runs with pagination."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        return await self._root._request("GET", "/runs", params=params)

    async def start(
        self,
        workflow_id: str | None = None,
        spec: dict[str, Any] | None = None,
        initial_state: dict[str, Any] | None = None,
        budget_config: dict[str, Any] | None = None,
        async_execution: bool = False,
    ) -> dict[str, Any]:
        """Start a new workflow execution run."""
        payload: dict[str, Any] = {
            "initial_state": initial_state or {},
            "async_execution": async_execution,
        }
        if workflow_id:
            payload["workflow_id"] = workflow_id
        if spec:
            payload["spec"] = spec
        if budget_config:
            payload["budget_config"] = budget_config
        return await self._root._request("POST", "/runs", json_data=payload)

    async def get(self, run_id: str) -> dict[str, Any]:
        """Retrieve run snapshot and events."""
        return await self._root._request("GET", f"/runs/{_safe_quote(run_id)}")

    async def cancel(self, run_id: str, reason: str | None = None) -> dict[str, Any]:
        """Cancel an active execution run."""
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = reason
        return await self._root._request(
            "POST", f"/runs/{_safe_quote(run_id)}/cancel", json_data=payload
        )

    async def decide(
        self,
        run_id: str,
        approval_id: str,
        decision: str = "approve",
        state_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a decision for a pending approval gate."""
        payload: dict[str, Any] = {
            "approval_id": approval_id,
            "decision": decision,
        }
        if state_update is not None:
            payload["state_update"] = state_update
        return await self._root._request(
            "POST", f"/runs/{_safe_quote(run_id)}/decide", json_data=payload
        )

    async def diagnostics(self, run_id: str, include_content: bool = False) -> dict[str, Any]:
        """Retrieve run diagnostics report."""
        params = {"include_content": "true" if include_content else "false"}
        return await self._root._request(
            "GET", f"/runs/{_safe_quote(run_id)}/diagnostics", params=params
        )

    async def trace(self, run_id: str, include_content: bool = False) -> dict[str, Any]:
        """Retrieve hierarchical span trace tree for a run."""
        params = {"include_content": "true" if include_content else "false"}
        return await self._root._request("GET", f"/runs/{_safe_quote(run_id)}/trace", params=params)

    async def provide_input(self, run_id: str, interrupt_id: str, value: Any) -> dict[str, Any]:
        """Answer an interrupt question so a run in waiting_for_input continues."""
        payload = {"interrupt_id": interrupt_id, "value": value}
        return await self._root._request(
            "POST", f"/runs/{_safe_quote(run_id)}/input", json_data=payload
        )

    async def history(self, run_id: str) -> dict[str, Any]:
        """Retrieve all checkpoints of a run, oldest first, with state redacted."""
        return await self._root._request("GET", f"/runs/{_safe_quote(run_id)}/history")

    async def subruns(self, run_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List latest checkpoints of all child runs created by sub-workflow steps."""
        params = {"limit": limit, "offset": offset}
        return await self._root._request(
            "GET", f"/runs/{_safe_quote(run_id)}/subruns", params=params
        )

    async def subrun(self, run_id: str, child_key: str) -> dict[str, Any]:
        """Retrieve detail, steps, and history for one child subrun."""
        return await self._root._request(
            "GET", f"/runs/{_safe_quote(run_id)}/subruns/{_safe_quote(child_key)}"
        )

    async def fork(
        self,
        run_id: str,
        checkpoint_index: int = 0,
        state_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a new run from a past checkpoint of this run, optionally changing its state."""
        payload: dict[str, Any] = {"checkpoint_index": checkpoint_index}
        if state_update is not None:
            payload["state_update"] = state_update
        return await self._root._request(
            "POST", f"/runs/{_safe_quote(run_id)}/fork", json_data=payload
        )

    async def repro_bundle(self, run_id: str) -> dict[str, Any]:
        """Download the repro bundle captured when this run failed (opt-in capture).

        Replay it offline with ``nexura-runtime repro run FILE``. A 404 means no bundle was
        captured for the run.
        """
        return await self._root._request("GET", f"/runs/{_safe_quote(run_id)}/repro")

    async def stream_events(
        self, run_id: str, cursor: int = 0, live: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream execution lifecycle events over Server-Sent Events (SSE)."""
        client = await self._root._get_client()
        url = f"{self._root.base_url}/api/v1/runs/{_safe_quote(run_id)}/events"
        headers = self._root._get_headers()
        params: dict[str, Any] = {"cursor": cursor}
        if live:
            params["live"] = "true"

        async with client.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise NexuraApiError(response.status_code, "STREAM_ERROR", body.decode("utf-8"))

            current_event: dict[str, Any] = {}
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    if current_event:
                        yield current_event
                        current_event = {}
                    continue

                if line.startswith("id:"):
                    seq_str = line[len("id:") :].strip()
                    current_event["seq"] = int(seq_str) if seq_str.isdigit() else seq_str
                elif line.startswith("event:"):
                    current_event["type"] = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_str = line[len("data:") :].strip()
                    try:
                        current_event["data"] = json.loads(data_str)
                    except json.JSONDecodeError:
                        current_event["data"] = data_str

            if current_event:
                yield current_event


class ModulesClient:
    """Client for querying registered versioned module descriptors."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(
        self,
        kind: str | None = None,
        query: str | None = None,
        available_only: bool = False,
    ) -> dict[str, Any]:
        """List registered modules and their typed contracts."""
        params: dict[str, Any] = {}
        if kind:
            params["kind"] = kind
        if query:
            params["query"] = query
        if available_only:
            params["available_only"] = "true"
        return await self._root._request("GET", "/modules", params=params)

    async def get(self, module_id: str, version: str | None = None) -> dict[str, Any]:
        """Retrieve descriptor for a specific module."""
        params: dict[str, Any] = {}
        if version:
            params["version"] = version
        return await self._root._request("GET", f"/modules/{_safe_quote(module_id)}", params=params)


class CapabilitiesClient:
    """Client for discovering service-wide capabilities and supported features."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def get(self) -> dict[str, Any]:
        """Retrieve service capabilities report."""
        return await self._root._request("GET", "/capabilities")


class WorkspacesClient:
    """Client for workspace management and policies."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        params = {"limit": limit, "offset": offset}
        return await self._root._request("GET", "/workspaces", params=params)

    async def create(
        self, workspace_id: str, name: str, description: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"workspace_id": workspace_id, "name": name}
        if description:
            payload["description"] = description
        return await self._root._request("POST", "/workspaces", json_data=payload)

    async def get(self, workspace_id: str | None = None) -> dict[str, Any]:
        ws = workspace_id or self._root.workspace_id
        return await self._root._request("GET", f"/workspaces/{_safe_quote(ws)}")

    async def update_policy(
        self, require_promotion_approval: bool, workspace_id: str | None = None
    ) -> dict[str, Any]:
        ws = workspace_id or self._root.workspace_id
        return await self._root._request(
            "PATCH",
            f"/workspaces/{_safe_quote(ws)}",
            json_data={"require_promotion_approval": require_promotion_approval},
        )


class SessionsClient:
    """Client for chat sessions and LLM turn interactions."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        params = {"limit": limit, "offset": offset}
        return await self._root._request("GET", "/sessions", params=params)

    async def create(
        self, session_id: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if session_id:
            payload["session_id"] = session_id
        if title:
            payload["title"] = title
        return await self._root._request("POST", "/sessions", json_data=payload)

    async def get(self, session_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/sessions/{_safe_quote(session_id)}")

    async def delete(self, session_id: str) -> dict[str, Any]:
        return await self._root._request("DELETE", f"/sessions/{_safe_quote(session_id)}")

    async def models(self) -> builtins.list[dict[str, Any]]:
        res = await self._root._request("GET", "/chat/models")
        return cast(
            builtins.list[dict[str, Any]],
            res.get("data", res) if isinstance(res, dict) else res,
        )

    async def chat(
        self,
        session_id: str,
        message: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"message": message}
        if model:
            payload["model"] = model
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        return await self._root._request(
            "POST", f"/sessions/{_safe_quote(session_id)}/chat", json_data=payload
        )

    async def chat_stream(
        self,
        session_id: str,
        message: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        client = await self._root._get_client()
        url = f"{self._root.base_url}/api/v1/sessions/{_safe_quote(session_id)}/chat/stream"
        headers = self._root._get_headers()
        headers["Accept"] = "text/event-stream"
        payload: dict[str, Any] = {"message": message}
        if model:
            payload["model"] = model
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature

        for attempt in (0, 1):
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    if attempt == 0 and await self._root._renew_session_token(response):
                        headers = {**headers, **self._root._get_headers()}
                        headers["Accept"] = "text/event-stream"
                        continue
                    raise NexuraApiError(response.status_code, "STREAM_ERROR", body.decode("utf-8"))
                async for event in self._read_sse(response):
                    yield event
                return

    @staticmethod
    async def _read_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        """Parse a ``text/event-stream`` body into ``{id, event, data}`` dicts."""
        current_event: dict[str, Any] = {}
        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                if current_event:
                    yield current_event
                    current_event = {}
                continue

            if line.startswith("id:"):
                current_event["id"] = line[len("id:") :].strip()
            elif line.startswith("event:"):
                current_event["event"] = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:") :].strip()
                try:
                    current_event["data"] = json.loads(data_str)
                except json.JSONDecodeError:
                    current_event["data"] = data_str

        if current_event:
            yield current_event


class KnowledgeClient:
    """Client for RAG collections, document ingestion, retrieval, and limits."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list_collections(self) -> dict[str, Any]:
        return await self._root._request("GET", "/knowledge/collections")

    async def create_collection(
        self, collection_id: str, name: str, description: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"collection_id": collection_id, "name": name}
        if description:
            payload["description"] = description
        return await self._root._request("POST", "/knowledge/collections", json_data=payload)

    async def limits(self) -> dict[str, Any]:
        return await self._root._request("GET", "/knowledge/limits")

    async def status(self, collection_id: str = "default") -> dict[str, Any]:
        """Configured and persisted embedding lineage of a collection (no provider calls)."""
        return await self._root._request(
            "GET", "/knowledge/status", params={"collection_id": collection_id}
        )

    async def reindex(self, collection_id: str) -> dict[str, Any]:
        """Rebuild the collection with the configured embedder (needs edit permission)."""
        return await self._root._request(
            "POST", f"/knowledge/collections/{_safe_quote(collection_id)}/reindex"
        )

    async def list_documents(
        self, collection_id: str = "default", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        params = {"collection_id": collection_id, "limit": limit, "offset": offset}
        return await self._root._request("GET", "/knowledge/documents", params=params)

    async def upload_document(
        self,
        filename: str,
        content: str,
        collection_id: str = "default",
        mime_type: str = "text/plain",
        content_encoding: str = "text",
        source_uri: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "filename": filename,
            "content": content,
            "collection_id": collection_id,
            "mime_type": mime_type,
            "content_encoding": content_encoding,
        }
        if source_uri:
            payload["source_uri"] = source_uri
        if metadata:
            payload["metadata"] = metadata
        return await self._root._request("POST", "/knowledge/documents/upload", json_data=payload)

    async def get_document(self, document_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/knowledge/documents/{_safe_quote(document_id)}")

    async def get_chunk(self, document_id: str, chunk_index: int) -> dict[str, Any]:
        return await self._root._request(
            "GET",
            f"/knowledge/documents/{_safe_quote(document_id)}/chunks/{_safe_quote(chunk_index)}",
        )

    async def delete_document(self, document_id: str) -> dict[str, Any]:
        return await self._root._request(
            "DELETE", f"/knowledge/documents/{_safe_quote(document_id)}"
        )

    async def retrieve(
        self,
        query: str,
        collection_id: str = "default",
        top_k: int = 5,
        dense_weight: float = 0.7,
        lexical_weight: float = 0.3,
        min_score_threshold: float = 0.0,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "collection_id": collection_id,
            "top_k": top_k,
            "dense_weight": dense_weight,
            "lexical_weight": lexical_weight,
            "min_score_threshold": min_score_threshold,
        }
        return await self._root._request("POST", "/knowledge/retrieve", json_data=payload)

    async def answer(
        self, query: str, collection_id: str = "default", language: str = "en"
    ) -> dict[str, Any]:
        payload = {"query": query, "collection_id": collection_id, "language": language}
        return await self._root._request("POST", "/knowledge/answer", json_data=payload)


class CredentialsClient:
    """Client for workspace credentials and validation."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        params = {"limit": limit, "offset": offset}
        return await self._root._request("GET", "/credentials", params=params)

    async def create(
        self,
        credential_id: str,
        credential_type: str,
        secret: str,
        provider: str = "custom",
        description: str = "",
        environment_variable: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "credential_id": credential_id,
            "credential_type": credential_type,
            "secret": secret,
            "provider": provider,
            "description": description,
        }
        if environment_variable:
            payload["environment_variable"] = environment_variable
        return await self._root._request("POST", "/credentials", json_data=payload)

    async def get(self, credential_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/credentials/{_safe_quote(credential_id)}")

    async def delete(self, credential_id: str) -> dict[str, Any]:
        return await self._root._request("DELETE", f"/credentials/{_safe_quote(credential_id)}")

    async def verify(self, credential_id: str) -> dict[str, Any]:
        return await self._root._request(
            "POST", f"/credentials/{_safe_quote(credential_id)}/verify"
        )

    async def revoke(self, credential_id: str) -> dict[str, Any]:
        return await self._root._request(
            "POST", f"/credentials/{_safe_quote(credential_id)}/revoke"
        )


class IntegrationsClient:
    """Client for external tool discovery and testing (MCP, OpenAPI)."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def get_status(self) -> dict[str, Any]:
        return await self._root._request("GET", "/integrations/status")

    async def discover_mcp(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        fixture_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if command:
            payload["command"] = command
        if args:
            payload["args"] = args
        if env:
            payload["env"] = env
        if fixture_id:
            payload["fixture_id"] = fixture_id
        return await self._root._request("POST", "/integrations/mcp/discover", json_data=payload)

    async def test_run_mcp(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        fixture_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool_name": tool_name, "arguments": arguments}
        if command:
            payload["command"] = command
        if args:
            payload["args"] = args
        if env:
            payload["env"] = env
        if fixture_id:
            payload["fixture_id"] = fixture_id
        return await self._root._request("POST", "/integrations/mcp/test-run", json_data=payload)

    async def discover_openapi(
        self,
        spec_content: str,
        base_url: str | None = None,
        auth_type: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"spec_content": spec_content}
        if base_url:
            payload["base_url"] = base_url
        if auth_type:
            payload["auth_type"] = auth_type
        return await self._root._request(
            "POST", "/integrations/openapi/discover", json_data=payload
        )

    async def test_run_openapi(
        self,
        spec_content: str,
        operation_id: str,
        parameters: dict[str, Any] | None = None,
        body: Any = None,
        credential_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "spec_content": spec_content,
            "operation_id": operation_id,
        }
        if parameters:
            payload["parameters"] = parameters
        if body is not None:
            payload["body"] = body
        if credential_id:
            payload["credential_id"] = credential_id
        return await self._root._request(
            "POST", "/integrations/openapi/test-run", json_data=payload
        )

    async def register(
        self,
        integration_type: str,
        name: str,
        configuration: dict[str, Any],
        tool_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "integration_type": integration_type,
            "name": name,
            "configuration": configuration,
        }
        if tool_ids:
            payload["tool_ids"] = tool_ids
        return await self._root._request("POST", "/integrations/register", json_data=payload)


class StudioEvaluationsClient:
    """Client for Studio evaluations, test cases from runs, comparisons, and red-teaming."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list_targets(self) -> dict[str, Any]:
        return await self._root._request("GET", "/evaluations/studio/targets")

    async def list_datasets(self, workflow_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        return await self._root._request("GET", "/evaluations/studio/datasets", params=params)

    async def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        return await self._root._request(
            "GET", f"/evaluations/studio/datasets/{_safe_quote(dataset_id)}"
        )

    async def put_dataset(
        self,
        dataset_id: str,
        cases: list[Any] | None = None,
        name: str | None = None,
        description: str | None = None,
        expected_revision: int | None = None,
        format: str | None = None,
        format_version: int | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if cases is not None:
            payload["cases"] = cases
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        if format is not None:
            payload["format"] = format
        if format_version is not None:
            payload["format_version"] = format_version
        if workflow_id is not None:
            payload["workflow_id"] = workflow_id
        return await self._root._request(
            "PUT",
            f"/evaluations/studio/datasets/{_safe_quote(dataset_id)}",
            json_data=payload,
        )

    async def delete_dataset(self, dataset_id: str) -> dict[str, Any]:
        return await self._root._request(
            "DELETE", f"/evaluations/studio/datasets/{_safe_quote(dataset_id)}"
        )

    async def export_dataset(self, dataset_id: str) -> dict[str, Any]:
        return await self._root._request(
            "GET", f"/evaluations/studio/datasets/{_safe_quote(dataset_id)}/export"
        )

    async def import_openai(
        self,
        content: str,
        dataset_id: str,
        input_field: str | None = None,
        expected_field: str | None = None,
        name_field: str | None = None,
        testing_criteria: Any = None,
        name: str | None = None,
        skip_invalid: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": content,
            "dataset_id": dataset_id,
            "skip_invalid": skip_invalid,
        }
        if input_field:
            payload["input_field"] = input_field
        if expected_field:
            payload["expected_field"] = expected_field
        if name_field:
            payload["name_field"] = name_field
        if testing_criteria is not None:
            payload["testing_criteria"] = testing_criteria
        if name:
            payload["name"] = name
        return await self._root._request(
            "POST", "/evaluations/studio/datasets/import-openai", json_data=payload
        )

    async def add_case_from_run(
        self,
        dataset_id: str,
        run_id: str,
        case_name: str | None = None,
        output_key: str | None = None,
        dataset_name: str | None = None,
        expected_revision: int | None = None,
        record_path: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"run_id": run_id, "record_path": record_path}
        if case_name:
            payload["case_name"] = case_name
        if output_key:
            payload["output_key"] = output_key
        if dataset_name:
            payload["dataset_name"] = dataset_name
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        return await self._root._request(
            "POST",
            f"/evaluations/studio/datasets/{_safe_quote(dataset_id)}/cases/from-run",
            json_data=payload,
        )

    async def create_run(
        self,
        dataset_id: str,
        target: dict[str, Any],
        scorers: list[dict[str, Any]] | None = None,
        repeats: int = 1,
        label: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset_id": dataset_id,
            "target": target,
            "repeats": repeats,
            "label": label,
        }
        if scorers is not None:
            payload["scorers"] = scorers
        return await self._root._request("POST", "/evaluations/studio/runs", json_data=payload)

    async def list_runs(self, dataset_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if dataset_id:
            params["dataset_id"] = dataset_id
        return await self._root._request("GET", "/evaluations/studio/runs", params=params)

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/evaluations/studio/runs/{_safe_quote(run_id)}")

    async def delete_run(self, run_id: str) -> dict[str, Any]:
        return await self._root._request(
            "DELETE", f"/evaluations/studio/runs/{_safe_quote(run_id)}"
        )

    async def compare(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        tolerance: float = 0.0,
        min_pass_rate: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "tolerance": tolerance,
        }
        if min_pass_rate is not None:
            payload["min_pass_rate"] = min_pass_rate
        return await self._root._request("POST", "/evaluations/studio/compare", json_data=payload)

    async def redteam(
        self,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        write_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if workflow_id:
            payload["workflow_id"] = workflow_id
        if agent_id:
            payload["agent_id"] = agent_id
        if write_tools is not None:
            payload["write_tools"] = write_tools
        return await self._root._request("POST", "/evaluations/studio/redteam", json_data=payload)


class EvaluationsClient:
    """Client for evaluation datasets, test runs, comparisons, and Studio test engines."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root
        self.studio = StudioEvaluationsClient(root)

    async def list_datasets(self) -> dict[str, Any]:
        return await self._root._request("GET", "/evaluations/datasets")

    async def create_dataset(
        self,
        dataset_id: str,
        name: str,
        examples: list[dict[str, Any]],
        description: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset_id": dataset_id,
            "name": name,
            "examples": examples,
        }
        if description:
            payload["description"] = description
        return await self._root._request("POST", "/evaluations/datasets", json_data=payload)

    async def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/evaluations/datasets/{_safe_quote(dataset_id)}")

    async def delete_dataset(self, dataset_id: str) -> dict[str, Any]:
        return await self._root._request(
            "DELETE", f"/evaluations/datasets/{_safe_quote(dataset_id)}"
        )

    async def run_evaluation(
        self,
        dataset_id: str,
        target_workflow_id: str | None = None,
        graders: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"dataset_id": dataset_id}
        if target_workflow_id:
            payload["target_workflow_id"] = target_workflow_id
        if graders:
            payload["graders"] = graders
        return await self._root._request("POST", "/evaluations/run", json_data=payload)

    async def list_runs(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        params = {"limit": limit, "offset": offset}
        return await self._root._request("GET", "/evaluations/runs", params=params)

    async def get_run(self, eval_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/evaluations/runs/{_safe_quote(eval_id)}")

    async def compare_runs(self, run_id_a: str, run_id_b: str) -> dict[str, Any]:
        payload = {"run_id_a": run_id_a, "run_id_b": run_id_b}
        return await self._root._request("POST", "/evaluations/compare", json_data=payload)


class TriggersClient:
    """Client for cron schedules, webhooks, durable execution jobs, and channel outbox."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list_schedules(self) -> dict[str, Any]:
        return await self._root._request("GET", "/triggers/schedules")

    async def create_schedule(
        self,
        workflow_id: str,
        cron_expression: str,
        schedule_id: str | None = None,
        name: str | None = None,
        initial_state: dict[str, Any] | None = None,
        paused: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "cron_expression": cron_expression,
            "paused": paused,
        }
        if schedule_id:
            payload["schedule_id"] = schedule_id
        if name:
            payload["name"] = name
        if initial_state:
            payload["initial_state"] = initial_state
        return await self._root._request("POST", "/triggers/schedules", json_data=payload)

    async def pause_schedule(self, schedule_id: str) -> dict[str, Any]:
        return await self._root._request(
            "POST", f"/triggers/schedules/{_safe_quote(schedule_id)}/pause"
        )

    async def resume_schedule(self, schedule_id: str) -> dict[str, Any]:
        return await self._root._request(
            "POST", f"/triggers/schedules/{_safe_quote(schedule_id)}/resume"
        )

    async def delete_schedule(self, schedule_id: str) -> dict[str, Any]:
        return await self._root._request(
            "DELETE", f"/triggers/schedules/{_safe_quote(schedule_id)}"
        )

    async def list_webhooks(self) -> dict[str, Any]:
        return await self._root._request("GET", "/triggers/webhooks")

    async def register_webhook(
        self,
        workflow_id: str,
        endpoint_id: str | None = None,
        secret: str | None = None,
        name: str | None = None,
        input_mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"workflow_id": workflow_id}
        if endpoint_id:
            payload["endpoint_id"] = endpoint_id
        if secret:
            payload["secret"] = secret
        if name:
            payload["name"] = name
        if input_mapping:
            payload["input_mapping"] = input_mapping
        return await self._root._request("POST", "/triggers/webhooks", json_data=payload)

    async def delete_webhook(self, endpoint_id: str) -> dict[str, Any]:
        return await self._root._request("DELETE", f"/triggers/webhooks/{_safe_quote(endpoint_id)}")

    async def test_webhook(
        self,
        endpoint_id: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if payload is not None:
            body["payload"] = payload
        if headers is not None:
            body["headers"] = headers
        return await self._root._request(
            "POST", f"/triggers/webhooks/{_safe_quote(endpoint_id)}/test", json_data=body
        )

    async def list_jobs(self, limit: int = 50) -> dict[str, Any]:
        return await self._root._request("GET", "/triggers/jobs", params={"limit": limit})

    async def list_outbox(self, channel: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if channel:
            params["channel"] = channel
        return await self._root._request("GET", "/triggers/outbox", params=params)

    async def test_dispatch_outbox(
        self,
        channel: str,
        recipient: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel,
            "recipient": recipient,
            "content": content,
        }
        if metadata:
            payload["metadata"] = metadata
        return await self._root._request(
            "POST", "/triggers/outbox/test-dispatch", json_data=payload
        )


class AgentsClient:
    """Client for saved workspace agents."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(self) -> dict[str, Any]:
        return await self._root._request("GET", "/agents")

    async def options(self) -> dict[str, Any]:
        return await self._root._request("GET", "/agents/options")

    async def get(self, agent_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/agents/{_safe_quote(agent_id)}")

    async def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._root._request("POST", "/agents", json_data=spec)

    async def update(
        self, agent_id: str, spec: dict[str, Any], expected_revision: int
    ) -> dict[str, Any]:
        payload = {**spec, "expected_revision": expected_revision}
        return await self._root._request(
            "PUT", f"/agents/{_safe_quote(agent_id)}", json_data=payload
        )

    async def delete(self, agent_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if expected_revision is not None:
            params["expected_revision"] = expected_revision
        return await self._root._request(
            "DELETE", f"/agents/{_safe_quote(agent_id)}", params=params
        )


class SkillsClient:
    """Client for workspace skills instructions and versions."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(self, label: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if label:
            params["label"] = label
        return await self._root._request("GET", "/skills", params=params)

    async def create(
        self,
        markdown: str,
        resources: dict[str, str] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"markdown": markdown}
        if resources:
            payload["resources"] = resources
        if label:
            payload["label"] = label
        return await self._root._request("POST", "/skills", json_data=payload)

    async def get(
        self,
        name: str,
        version: int | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if version is not None:
            params["version"] = version
        if label:
            params["label"] = label
        return await self._root._request("GET", f"/skills/{_safe_quote(name)}", params=params)

    async def versions(self, name: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/skills/{_safe_quote(name)}/versions")

    async def set_label(self, name: str, label: str, version: int) -> dict[str, Any]:
        return await self._root._request(
            "PUT",
            f"/skills/{_safe_quote(name)}/labels/{_safe_quote(label)}",
            json_data={"version": version},
        )

    async def remove(self, name: str, version: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if version is not None:
            params["version"] = version
        return await self._root._request("DELETE", f"/skills/{_safe_quote(name)}", params=params)


class TemplatesClient:
    """Client for starter workflow template gallery."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def list(self, category: str | None = None, tag: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
        return await self._root._request("GET", "/templates", params=params)

    async def get(self, template_id: str) -> dict[str, Any]:
        return await self._root._request("GET", f"/templates/{_safe_quote(template_id)}")

    async def instantiate(
        self,
        template_id: str,
        name: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if workflow_id:
            payload["workflow_id"] = workflow_id
        return await self._root._request(
            "POST",
            f"/templates/{_safe_quote(template_id)}/instantiate",
            json_data=payload,
        )


class BuilderClient:
    """Client for chat-to-build workflow drafting."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def draft(
        self,
        description: str,
        credential: str | None = None,
        model: str | None = None,
        model_ref: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "description": description,
            "max_attempts": max_attempts,
        }
        if credential:
            payload["credential"] = credential
        if model:
            payload["model"] = model
        if model_ref:
            payload["model_ref"] = model_ref
        return await self._root._request("POST", "/builder/draft", json_data=payload)


class LocalSessionClient:
    """The local server's per-session token (``GET /api/v1/local-session``)."""

    def __init__(self, root: NexuraClient) -> None:
        self._root = root

    async def token(self) -> str:
        """Read the session token from the server and use it for later requests."""
        return await self._root.fetch_session_token()


class NexuraClient:
    """Official asynchronous Python SDK client for the Nexura Service API.

    Against a local server (``nexura-runtime serve``), state-changing requests need the
    per-session token in ``X-Nexura-Session``. Pass ``session_token`` (for example
    ``app.state.session_token`` of an embedded app), or leave ``auto_session_token`` on: when the
    server answers 403 ``SESSION_TOKEN_REQUIRED`` / ``SESSION_TOKEN_INVALID`` (a restart changes
    the token), the client reads it from ``GET /api/v1/local-session`` and retries once.

    It covers the routes the open-source service serves (``COVERED_ROUTES``). Clients for the
    closed edition's hosted routes (organisations, publishing, the hosted secrets admin API and
    workspace members) are not part of this package; the closed edition ships them as an add-on
    that subclasses this client.
    """

    COVERED_ROUTES: dict[tuple[str, str], tuple[str, str]] = {
        # Workflows
        ("GET", "/api/v1/workflows"): ("workflows", "list"),
        ("POST", "/api/v1/workflows"): ("workflows", "create"),
        ("POST", "/api/v1/workflows/validate"): ("workflows", "validate"),
        ("GET", "/api/v1/workflows/{workflow_id}"): ("workflows", "get"),
        ("PUT", "/api/v1/workflows/{workflow_id}"): ("workflows", "update"),
        ("DELETE", "/api/v1/workflows/{workflow_id}"): ("workflows", "delete"),
        ("POST", "/api/v1/workflows/{workflow_id}/export"): ("workflows", "export"),
        ("GET", "/api/v1/workflows/{workflow_id}/export/download"): (
            "workflows",
            "download_export",
        ),
        ("GET", "/api/v1/workflows/{workflow_id}/export/python"): ("workflows", "export_python"),
        ("POST", "/api/v1/workflows/import/python"): ("workflows", "import_python"),
        ("POST", "/api/v1/workflows/import"): ("workflows", "import_bundle"),
        # Runs
        ("GET", "/api/v1/runs"): ("runs", "list"),
        ("POST", "/api/v1/runs"): ("runs", "start"),
        ("GET", "/api/v1/runs/{run_id}"): ("runs", "get"),
        ("POST", "/api/v1/runs/{run_id}/cancel"): ("runs", "cancel"),
        ("POST", "/api/v1/runs/{run_id}/decide"): ("runs", "decide"),
        ("GET", "/api/v1/runs/{run_id}/diagnostics"): ("runs", "diagnostics"),
        ("GET", "/api/v1/runs/{run_id}/trace"): ("runs", "trace"),
        ("POST", "/api/v1/runs/{run_id}/input"): ("runs", "provide_input"),
        ("GET", "/api/v1/runs/{run_id}/history"): ("runs", "history"),
        ("GET", "/api/v1/runs/{run_id}/subruns"): ("runs", "subruns"),
        ("GET", "/api/v1/runs/{run_id}/subruns/{child_key}"): ("runs", "subrun"),
        ("POST", "/api/v1/runs/{run_id}/fork"): ("runs", "fork"),
        ("GET", "/api/v1/runs/{run_id}/events"): ("runs", "stream_events"),
        ("GET", "/api/v1/runs/{run_id}/repro"): ("runs", "repro_bundle"),
        # Descriptors & Capabilities
        ("GET", "/api/v1/modules"): ("modules", "list"),
        ("GET", "/api/v1/modules/{module_id}"): ("modules", "get"),
        ("GET", "/api/v1/capabilities"): ("capabilities", "get"),
        # Workspaces (the closed edition serves the member routes, EDITIONS_PLAN step 7)
        ("GET", "/api/v1/workspaces"): ("workspaces", "list"),
        ("POST", "/api/v1/workspaces"): ("workspaces", "create"),
        ("GET", "/api/v1/workspaces/{workspace_id}"): ("workspaces", "get"),
        ("PATCH", "/api/v1/workspaces/{workspace_id}"): ("workspaces", "update_policy"),
        # Sessions & Chat
        ("GET", "/api/v1/sessions"): ("sessions", "list"),
        ("POST", "/api/v1/sessions"): ("sessions", "create"),
        ("GET", "/api/v1/sessions/{session_id}"): ("sessions", "get"),
        ("DELETE", "/api/v1/sessions/{session_id}"): ("sessions", "delete"),
        ("POST", "/api/v1/sessions/{session_id}/chat"): ("sessions", "chat"),
        ("POST", "/api/v1/sessions/{session_id}/chat/stream"): ("sessions", "chat_stream"),
        ("GET", "/api/v1/chat/models"): ("sessions", "models"),
        # Knowledge
        ("GET", "/api/v1/knowledge/collections"): ("knowledge", "list_collections"),
        ("POST", "/api/v1/knowledge/collections"): ("knowledge", "create_collection"),
        ("GET", "/api/v1/knowledge/limits"): ("knowledge", "limits"),
        ("GET", "/api/v1/knowledge/status"): ("knowledge", "status"),
        ("POST", "/api/v1/knowledge/collections/{collection_id}/reindex"): (
            "knowledge",
            "reindex",
        ),
        ("GET", "/api/v1/knowledge/documents"): ("knowledge", "list_documents"),
        ("POST", "/api/v1/knowledge/documents/upload"): ("knowledge", "upload_document"),
        ("GET", "/api/v1/knowledge/documents/{document_id}"): ("knowledge", "get_document"),
        ("GET", "/api/v1/knowledge/documents/{document_id}/chunks/{chunk_index}"): (
            "knowledge",
            "get_chunk",
        ),
        ("DELETE", "/api/v1/knowledge/documents/{document_id}"): ("knowledge", "delete_document"),
        ("POST", "/api/v1/knowledge/retrieve"): ("knowledge", "retrieve"),
        ("POST", "/api/v1/knowledge/answer"): ("knowledge", "answer"),
        # Credentials
        ("GET", "/api/v1/credentials"): ("credentials", "list"),
        ("POST", "/api/v1/credentials"): ("credentials", "create"),
        ("GET", "/api/v1/credentials/{credential_id}"): ("credentials", "get"),
        ("DELETE", "/api/v1/credentials/{credential_id}"): ("credentials", "delete"),
        ("POST", "/api/v1/credentials/{credential_id}/verify"): ("credentials", "verify"),
        ("POST", "/api/v1/credentials/{credential_id}/revoke"): ("credentials", "revoke"),
        # Integrations
        ("GET", "/api/v1/integrations/status"): ("integrations", "get_status"),
        ("POST", "/api/v1/integrations/mcp/discover"): ("integrations", "discover_mcp"),
        ("POST", "/api/v1/integrations/mcp/test-run"): ("integrations", "test_run_mcp"),
        ("POST", "/api/v1/integrations/openapi/discover"): ("integrations", "discover_openapi"),
        ("POST", "/api/v1/integrations/openapi/test-run"): ("integrations", "test_run_openapi"),
        ("POST", "/api/v1/integrations/register"): ("integrations", "register"),
        # Evaluations (Core & Studio)
        ("GET", "/api/v1/evaluations/datasets"): ("evaluations", "list_datasets"),
        ("POST", "/api/v1/evaluations/datasets"): ("evaluations", "create_dataset"),
        ("GET", "/api/v1/evaluations/datasets/{dataset_id}"): ("evaluations", "get_dataset"),
        ("DELETE", "/api/v1/evaluations/datasets/{dataset_id}"): ("evaluations", "delete_dataset"),
        ("POST", "/api/v1/evaluations/run"): ("evaluations", "run_evaluation"),
        ("GET", "/api/v1/evaluations/runs"): ("evaluations", "list_runs"),
        ("GET", "/api/v1/evaluations/runs/{eval_id}"): ("evaluations", "get_run"),
        ("POST", "/api/v1/evaluations/compare"): ("evaluations", "compare_runs"),
        ("GET", "/api/v1/evaluations/studio/targets"): ("evaluations", "studio.list_targets"),
        ("GET", "/api/v1/evaluations/studio/datasets"): ("evaluations", "studio.list_datasets"),
        ("PUT", "/api/v1/evaluations/studio/datasets/{dataset_id}"): (
            "evaluations",
            "studio.put_dataset",
        ),
        ("GET", "/api/v1/evaluations/studio/datasets/{dataset_id}"): (
            "evaluations",
            "studio.get_dataset",
        ),
        ("GET", "/api/v1/evaluations/studio/datasets/{dataset_id}/export"): (
            "evaluations",
            "studio.export_dataset",
        ),
        ("POST", "/api/v1/evaluations/studio/datasets/import-openai"): (
            "evaluations",
            "studio.import_openai",
        ),
        ("DELETE", "/api/v1/evaluations/studio/datasets/{dataset_id}"): (
            "evaluations",
            "studio.delete_dataset",
        ),
        ("POST", "/api/v1/evaluations/studio/datasets/{dataset_id}/cases/from-run"): (
            "evaluations",
            "studio.add_case_from_run",
        ),
        ("POST", "/api/v1/evaluations/studio/runs"): ("evaluations", "studio.create_run"),
        ("GET", "/api/v1/evaluations/studio/runs"): ("evaluations", "studio.list_runs"),
        ("GET", "/api/v1/evaluations/studio/runs/{run_id}"): ("evaluations", "studio.get_run"),
        ("DELETE", "/api/v1/evaluations/studio/runs/{run_id}"): (
            "evaluations",
            "studio.delete_run",
        ),
        ("POST", "/api/v1/evaluations/studio/compare"): ("evaluations", "studio.compare"),
        ("POST", "/api/v1/evaluations/studio/redteam"): ("evaluations", "studio.redteam"),
        # Triggers
        ("GET", "/api/v1/triggers/schedules"): ("triggers", "list_schedules"),
        ("POST", "/api/v1/triggers/schedules"): ("triggers", "create_schedule"),
        ("POST", "/api/v1/triggers/schedules/{schedule_id}/pause"): (
            "triggers",
            "pause_schedule",
        ),
        ("POST", "/api/v1/triggers/schedules/{schedule_id}/resume"): (
            "triggers",
            "resume_schedule",
        ),
        ("DELETE", "/api/v1/triggers/schedules/{schedule_id}"): ("triggers", "delete_schedule"),
        ("GET", "/api/v1/triggers/webhooks"): ("triggers", "list_webhooks"),
        ("POST", "/api/v1/triggers/webhooks"): ("triggers", "register_webhook"),
        ("DELETE", "/api/v1/triggers/webhooks/{endpoint_id}"): ("triggers", "delete_webhook"),
        ("POST", "/api/v1/triggers/webhooks/{endpoint_id}/test"): ("triggers", "test_webhook"),
        ("GET", "/api/v1/triggers/jobs"): ("triggers", "list_jobs"),
        ("GET", "/api/v1/triggers/outbox"): ("triggers", "list_outbox"),
        ("POST", "/api/v1/triggers/outbox/test-dispatch"): ("triggers", "test_dispatch_outbox"),
        # Agents
        ("GET", "/api/v1/agents"): ("agents", "list"),
        ("GET", "/api/v1/agents/options"): ("agents", "options"),
        ("GET", "/api/v1/agents/{agent_id}"): ("agents", "get"),
        ("POST", "/api/v1/agents"): ("agents", "create"),
        ("PUT", "/api/v1/agents/{agent_id}"): ("agents", "update"),
        ("DELETE", "/api/v1/agents/{agent_id}"): ("agents", "delete"),
        # Skills
        ("GET", "/api/v1/skills"): ("skills", "list"),
        ("POST", "/api/v1/skills"): ("skills", "create"),
        ("GET", "/api/v1/skills/{name}"): ("skills", "get"),
        ("GET", "/api/v1/skills/{name}/versions"): ("skills", "versions"),
        ("PUT", "/api/v1/skills/{name}/labels/{label}"): ("skills", "set_label"),
        ("DELETE", "/api/v1/skills/{name}"): ("skills", "remove"),
        # Templates
        ("GET", "/api/v1/templates"): ("templates", "list"),
        ("GET", "/api/v1/templates/{template_id}"): ("templates", "get"),
        ("POST", "/api/v1/templates/{template_id}/instantiate"): (
            "templates",
            "instantiate",
        ),
        # Builder
        ("POST", "/api/v1/builder/draft"): ("builder", "draft"),
        # Local per-session token (local-owner profile only)
        ("GET", "/api/v1/local-session"): ("local_session", "token"),
    }

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        *,
        api_key: str | None = None,
        workspace_id: str = "default",
        http_client: httpx.AsyncClient | None = None,
        session_token: str | None = None,
        auto_session_token: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.workspace_id = workspace_id
        self.session_token = session_token
        self.auto_session_token = auto_session_token
        self._external_client = http_client
        self._internal_client: httpx.AsyncClient | None = None

        self.workflows = WorkflowsClient(self)
        self.runs = RunsClient(self)
        self.modules = ModulesClient(self)
        self.capabilities = CapabilitiesClient(self)
        self.workspaces = WorkspacesClient(self)
        self.sessions = SessionsClient(self)
        self.knowledge = KnowledgeClient(self)
        self.credentials = CredentialsClient(self)
        self.integrations = IntegrationsClient(self)
        self.evaluations = EvaluationsClient(self)
        self.triggers = TriggersClient(self)
        self.agents = AgentsClient(self)
        self.skills = SkillsClient(self)
        self.templates = TemplatesClient(self)
        self.builder = BuilderClient(self)
        self.local_session = LocalSessionClient(self)

    def with_workspace(self, workspace_id: str) -> Self:
        """Create a client copy scoped to a different workspace identifier (same class)."""
        return type(self)(
            base_url=self.base_url,
            api_key=self.api_key,
            workspace_id=workspace_id,
            http_client=self._external_client or self._internal_client,
            session_token=self.session_token,
            auto_session_token=self.auto_session_token,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._external_client:
            return self._external_client
        if self._internal_client is None or self._internal_client.is_closed:
            self._internal_client = httpx.AsyncClient(timeout=30.0)
        return self._internal_client

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Workspace-ID": self.workspace_id,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.session_token:
            headers[SESSION_HEADER] = self.session_token
        return headers

    async def fetch_session_token(self) -> str:
        """Read the local server's session token (``GET /api/v1/local-session``) and keep it."""
        client = await self._get_client()
        response = await client.get(
            f"{self.base_url}/api/v1/local-session",
            headers={"Accept": "application/json", "X-Workspace-ID": self.workspace_id},
        )
        token = response.json().get("token") if response.status_code == 200 else None
        if not isinstance(token, str) or not token:
            raise NexuraApiError(
                response.status_code,
                "SESSION_TOKEN_UNAVAILABLE",
                "The server did not return a session token.",
            )
        self.session_token = token
        return token

    async def _renew_session_token(self, response: httpx.Response) -> bool:
        """After a session-token refusal, read the token again; True when it changed."""
        if not self.auto_session_token or not _session_token_refusal(response):
            return False
        previous = self.session_token
        try:
            await self.fetch_session_token()
        except (NexuraApiError, httpx.HTTPError, ValueError):
            return False
        return self.session_token != previous

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        url = f"{self.base_url}/api/v1{path}"

        response = await client.request(
            method=method,
            url=url,
            params=params,
            json=json_data,
            headers=self._get_headers(),
        )
        if method.upper() not in _SAFE_METHODS and await self._renew_session_token(response):
            response = await client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=self._get_headers(),
            )

        if response.status_code >= 400:
            try:
                err_data = response.json()
                detail = err_data.get("detail", {})
                if isinstance(detail, dict):
                    code = detail.get("code", "HTTP_ERROR")
                    msg = detail.get("message", response.text)
                else:
                    code = "HTTP_ERROR"
                    msg = str(detail)
            except Exception:
                code = "HTTP_ERROR"
                msg = response.text
            raise NexuraApiError(response.status_code, code, msg)

        data = response.json()
        return cast(dict[str, Any], data) if isinstance(data, dict) else {"data": data}

    async def close(self) -> None:
        """Close underlying HTTP client connections."""
        if self._internal_client and not self._internal_client.is_closed:
            await self._internal_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
