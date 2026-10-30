# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Hosted login and declarative SDK uploads. Python files are parsed, never executed."""

from __future__ import annotations

import argparse
import ast
import asyncio
import getpass
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from nexura_agents.errors import NexuraError
from nexura_agents.hosted import credentials_path, resolve_connection, validate_url

from nexura_runtime.agents_spec import parse_agent_spec
from nexura_runtime.codegen import CodeImportError, python_to_workflow
from nexura_runtime.service.api.client import NexuraApiError, NexuraClient
from nexura_runtime.workflow import WorkflowSpec

MAX_FILE_BYTES = 25_000_000
CODE_REFUSAL = (
    "Custom Python tools and handlers cannot run on hosted Nexura. "
    "Use declarative modules available on the hosted app; keep custom code running locally."
)


def http_client() -> httpx.AsyncClient:
    """Injection boundary for offline tests; redirects never forward credentials."""
    return httpx.AsyncClient(timeout=30, follow_redirects=False)


async def identity(http: httpx.AsyncClient, url: str, key: str) -> dict[str, Any]:
    response = await http.get(url + "/api/v1/workspace/whoami", headers={"X-API-Key": key})
    if response.status_code != 200:
        raise ValueError(f"Login verification failed (HTTP {response.status_code}).")
    value = response.json()
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("workspace_id"), str)
        or not value["workspace_id"]
        or value.get("role") not in ("admin", "editor", "viewer")
    ):
        raise ValueError("Invalid identity response from Nexura.")
    return value


def save_credentials(url: str, key: str) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"url": url, "api_key": key}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Upload file exceeds 25 MB.")
    return path.read_text(encoding="utf-8")


def _workflow(path: Path) -> tuple[dict[str, Any], str | None]:
    if path.suffix == ".py":
        source = _read(path)
        try:
            spec = python_to_workflow(source).to_dict()
        except CodeImportError as exc:
            raise ValueError(
                f"{CODE_REFUSAL} Python import accepts generated workflow files only."
            ) from exc
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module
            not in {"nexura_runtime.workflow", "nexura_runtime.studio.builtin_modules"}
            for node in ast.walk(ast.parse(source))
        ):
            raise ValueError(CODE_REFUSAL)
        return spec, source
    if path.is_dir():
        value = json.loads(_read(path / "workflow.json"))
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            files = [
                item
                for item in archive.infolist()
                if item.filename == "workflow.json" or item.filename.endswith("/workflow.json")
            ]
            if len(files) != 1 or files[0].file_size > MAX_FILE_BYTES:
                raise ValueError("Bundle must contain one workflow.json of at most 25 MB.")
            # Do not extract or upload executable bundle contents.
            with archive.open(files[0]) as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("Bundle workflow.json exceeds 25 MB.")
            value = json.loads(raw)
    else:
        value = json.loads(_read(path))
    if not isinstance(value, dict):
        raise ValueError("Workflow must be a JSON object.")
    spec = value.get("spec", value)
    return WorkflowSpec.from_dict(spec).to_dict(), None


def _handlers(value: Any) -> set[str]:
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "python_source",
                "source_code",
                "tool_code",
                "registry_import",
                "handler_sources",
            }:
                raise ValueError(CODE_REFUSAL)
            if key in ("handler", "module_id") and isinstance(item, str):
                found.add(item)
            found.update(_handlers(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_handlers(item))
    return found


async def push(http: httpx.AsyncClient, url: str, key: str, args: argparse.Namespace) -> None:
    path = Path(args.file)
    # Parse before contacting the service. Never import uploaded Python.
    source = None
    if args.push_kind == "workflow":
        spec, source = _workflow(path)
        handlers = _handlers(spec)
    else:
        spec = json.loads(_read(path))
        if not isinstance(spec, dict):
            raise ValueError("Upload must be a JSON object.")
        handlers = set()
        if args.push_kind == "agent":
            spec = spec.get("spec", spec)
            if not isinstance(spec, dict) or any(
                not isinstance(tool, str) for tool in spec.get("tools", [])
            ):
                raise ValueError(CODE_REFUSAL)
            _handlers(spec)
            spec = parse_agent_spec(spec).model_dump(mode="json")
    who = await identity(http, url, key)
    # The web link is informational: a service that reports an unusable address (for example
    # http:// behind a TLS proxy) still accepts the upload, linked under the API address.
    try:
        web_url = validate_url(str(who.get("web_url") or url))
    except ValueError:
        web_url = url
    async with NexuraClient(
        url,
        api_key=key,
        workspace_id=who["workspace_id"],
        http_client=http,
        auto_session_token=False,
    ) as client:
        if args.push_kind == "workflow":
            modules = (await client.modules.list(available_only=True))["items"]
            available = {str(item["module_id"]) for item in modules}
            available.update(
                f"{item['module_id']}@{item['version']}" for item in modules if item.get("version")
            )
            if handlers - available:
                raise ValueError(CODE_REFUSAL)
            if source is not None:
                result = await client.workflows.import_python(source)
            else:
                result = await client.workflows.import_bundle(spec=spec)
            resource_id = result["workflow_id"]
            print(f"Workflow: {resource_id}")
            print(web_url + "/workflows/" + quote(str(resource_id), safe=""))
        elif args.push_kind == "agent":
            options = await client.agents.options()
            available = {item["module_id"] for item in options.get("tools", [])}
            if set(spec.get("tools", [])) - available:
                raise ValueError(CODE_REFUSAL)
            existing = [
                item
                for item in (await client.agents.list())["items"]
                if item["name"] == spec["name"]
            ]
            if existing:
                old = await client.agents.get(existing[0]["agent_id"])
                result = await client.agents.update(old["agent_id"], spec, old["revision"])
            else:
                result = await client.agents.create(spec)
            resource_id = result["agent_id"]
            print(f"Agent: {resource_id}")
            print(web_url + "/agents/" + quote(str(resource_id), safe=""))
        else:
            result = await client._request("POST", "/sdk-traces/otlp", json_data=spec)
            rejected = result.get("partialSuccess", {}).get("rejectedSpans", "0")
            print(f"Traces uploaded. Rejected spans: {rejected}")


def hosted_command(args: argparse.Namespace) -> int:
    key = ""
    action = args.hosted_action

    async def run() -> None:
        nonlocal key
        if action == "logout":
            credentials_path().unlink(missing_ok=True)
            print("Logged out. Local credentials removed.")
            return
        if action == "login":
            url = validate_url(args.url)
            key = (
                sys.stdin.readline().strip()
                if args.api_key_stdin
                else os.environ.get("NEXURA_API_KEY") or getpass.getpass("Nexura API key: ")
            )
            url, key = resolve_connection(url, key)
        else:
            url, key = resolve_connection()
        async with http_client() as http:
            if action == "push":
                await push(http, url, key, args)
                return
            who = await identity(http, url, key)
            if action == "login":
                save_credentials(url, key)
            print(
                f"Workspace: {who['workspace_id']}  Role: {who['role']}".replace(key, "[REDACTED]")
            )

    try:
        asyncio.run(run())
        return 0
    except NexuraApiError as exc:
        # A remote error can echo submitted content or credentials: print only the status, the
        # machine error code and the names of the rejected fields, never server messages.
        print(f"Error: hosted request failed ({_api_error_summary(exc)}).", file=sys.stderr)
    except httpx.HTTPError:
        print("Error: unable to contact hosted Nexura.", file=sys.stderr)
    # NexuraError: an invalid workflow or agent file (WorkflowValidationError and friends).
    except (NexuraError, OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        message = str(exc).replace(key, "[REDACTED]") if key else str(exc)
        print(f"Error: {message}", file=sys.stderr)
    return 1


def _api_error_summary(exc: NexuraApiError) -> str:
    parts = [f"HTTP {exc.status_code}"]
    if isinstance(exc.code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", exc.code):
        parts.append(exc.code)
    errors = exc.details.get("field_errors") if isinstance(exc.details, dict) else None
    fields = sorted(
        {
            item["field"]
            for item in (errors if isinstance(errors, list) else [])
            if isinstance(item, dict)
            and isinstance(item.get("field"), str)
            and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,64}", item["field"])
        }
    )
    if fields:
        parts.append("check: " + ", ".join(fields[:10]))
    return "; ".join(parts)


def add_commands(subparsers: Any) -> None:
    login = subparsers.add_parser("login", help="Sign in to hosted Nexura with a workspace API key")
    login.add_argument("--url", required=True)
    login.add_argument("--api-key-stdin", action="store_true")
    login.set_defaults(func=hosted_command, hosted_action="login")
    for name in ("logout", "whoami"):
        subparsers.add_parser(name).set_defaults(func=hosted_command, hosted_action=name)
    command = subparsers.add_parser(
        "push", help="Upload workflows, saved agents or OTLP JSON traces"
    )
    kinds = command.add_subparsers(dest="push_kind", required=True)
    for kind in ("workflow", "agent", "traces"):
        child = kinds.add_parser(kind)
        child.add_argument("file")
        child.set_defaults(func=hosted_command, hosted_action="push")
