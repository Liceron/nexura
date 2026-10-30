# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Portable workflow bundle exporter.

Includes dependency locks, schema-aware scrubbing, and credential refs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexura_agents.errors import WorkflowValidationError

from nexura_runtime.observability.run_diagnostics import is_secret_key, redact_secret_text
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec


@dataclass(frozen=True)
class ExportBundle:
    """Metadata describing a generated standalone workflow export bundle."""

    target_dir: Path
    workflow_id: str
    spec_hash: str
    files: list[str]
    source_spec_hash: str = ""
    exported_spec_hash: str = ""


class WorkflowExporter:
    """Exports workflows to self-contained, portable project bundles.

    Security & Portability Guarantees:
    1. Secrets & Credentials: Raw secret tokens, keys, and passwords are never exported.
       They are replaced with scoped CredentialReference placeholders (e.g., {"ref": "KEY"}).
       Non-secret configuration (such as token_limit, max_tokens, primary_key) is preserved.
    2. Local Paths: Absolute host filesystem paths are sanitized to relative paths.
    3. Dependency Locks: Generates pyproject.toml and requirements.txt pinning nexura-runtime.
    4. Standalone Runner: Generates a self-contained run.py that can run in an external dir,
       dynamically resolving environment variables for credential references.
    5. Hash Transparency: Records both source_spec_hash and exported_spec_hash.
    """

    NON_SECRET_FIELDS = frozenset(
        {
            "token_limit",
            "max_tokens",
            "token_count",
            "primary_key",
            "foreign_key",
            "key_attribute",
            "sort_key",
            "partition_key",
            "cache_key",
            "session_key",
            "idempotency_key",
        }
    )

    EXACT_SECRET_FIELDS = frozenset(
        {
            "key",
            "secret",
            "token",
            "password",
            "auth",
            "credential",
            "api_key",
            "secret_key",
            "auth_token",
            "access_token",
            "refresh_token",
            "private_key",
            "client_secret",
            "bearer_token",
            "jwt_token",
            "webhook_secret",
        }
    )

    @classmethod
    def is_secret_field(cls, key: str) -> bool:
        """Determine whether a configuration key represents a confidential secret."""
        k_lower = key.lower().replace("-", "_")
        if k_lower in cls.NON_SECRET_FIELDS:
            return False
        # Same rules as run-event redaction (apikey, authorization, x-api-key, ...).
        return k_lower in cls.EXACT_SECRET_FIELDS or is_secret_key(key)

    @classmethod
    def validate_target_dir(cls, target_dir: Path | str) -> Path:
        """Validate destination path against path traversal and restricted system roots."""
        raw_str = str(target_dir)
        if ".." in raw_str or "\x00" in raw_str:
            raise ValueError(f"Path traversal detected in target directory: '{raw_str}'")

        resolved = Path(target_dir).resolve()
        restricted_prefixes = (
            "/etc",
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/var",
            "/boot",
            "/root",
            "/sys",
            "/proc",
            "/dev",
        )
        resolved_str = str(resolved)
        for pref in restricted_prefixes:
            if resolved_str == pref or resolved_str.startswith(f"{pref}/"):
                raise ValueError(f"Target directory targets protected system path: '{pref}'")
        return resolved

    def sanitize_spec_dict(self, data: Any, found_credentials: set[str] | None = None) -> Any:
        """Recursively scrub raw host secrets, database files, and absolute host paths."""
        creds = found_credentials if found_credentials is not None else set()
        if isinstance(data, dict):
            # If already a credential reference dictionary
            if "ref" in data and isinstance(data["ref"], str):
                creds.add(data["ref"])
                return {"ref": data["ref"]}

            sanitized: dict[str, Any] = {}
            for k, v in data.items():
                if self.is_secret_field(str(k)) and v not in (None, "", [], {}, False):
                    if isinstance(v, dict) and "ref" in v:
                        sanitized[k] = self.sanitize_spec_dict(v, creds)
                        continue
                    ref_name = re.sub(r"[^A-Z0-9_]", "_", str(k).upper())
                    creds.add(ref_name)
                    sanitized[k] = {"ref": ref_name}
                elif isinstance(v, str):
                    val = redact_secret_text(v)
                    if "nexura v2" in val.lower() or "nexura_v2" in val.lower():
                        val = Path(val).name
                    if val.endswith(".sqlite3") or val.endswith(".db"):
                        val = "workflow.sqlite3"
                    is_host_path = (
                        val.startswith("/home/")
                        or val.startswith("/Users/")
                        or val.startswith("C:\\")
                        or val.startswith("/tmp/")
                    )
                    if is_host_path:
                        val = Path(val).name
                    sanitized[k] = val
                else:
                    sanitized[k] = self.sanitize_spec_dict(v, creds)
            return sanitized
        elif isinstance(data, list):
            return [self.sanitize_spec_dict(item, creds) for item in data]
        elif isinstance(data, str):
            return redact_secret_text(data)
        return data

    def export_bundle(
        self,
        spec: WorkflowSpec,
        target_dir: Path | str,
        workflow_id: str = "exported_workflow",
        registry: ModuleRegistry | None = None,
        validate_handlers: bool = True,
    ) -> ExportBundle:
        """Generate a self-contained, portable bundle into target_dir."""
        dest = self.validate_target_dir(target_dir)
        dest.mkdir(parents=True, exist_ok=True)

        if validate_handlers:
            from nexura_runtime.studio.builtin_modules import get_default_registry

            reg = registry or get_default_registry()
            for node_id, node in spec.nodes.items():
                handler_name = node.handler or node.module_id or ""
                if not handler_name:
                    raise WorkflowValidationError(f"Node '{node_id}' missing handler or module_id.")
                is_mcp = handler_name.startswith("mcp.")
                is_openapi = handler_name.startswith("openapi.")
                is_known = reg.has_handler(handler_name) or (
                    bool(node.module_id and reg.has_module(node.module_id, node.module_version))
                )
                if not (is_mcp or is_openapi or is_known):
                    raise WorkflowValidationError(
                        f"Cannot export runnable bundle: node '{node_id}' references "
                        f"unsupported or unregistered handler '{handler_name}'."
                    )

        source_spec_hash = spec.canonical_hash()

        found_credentials: set[str] = set()
        spec_dict = spec.to_dict()
        sanitized_spec = self.sanitize_spec_dict(spec_dict, found_credentials)

        # Validate that the sanitized spec is structurally valid
        exported_spec = WorkflowSpec.from_dict(sanitized_spec)
        exported_spec_hash = exported_spec.canonical_hash()

        generated_files: list[str] = []

        # Detect connectors (MCP / OpenAPI) used by nodes
        connectors: list[dict[str, Any]] = []
        for node in spec.nodes.values():
            handler = node.handler or ""
            module_id = node.module_id or ""
            target_id = module_id if module_id else handler

            if target_id.startswith("mcp."):
                parts = target_id.split(".")
                server_name = parts[1] if len(parts) > 1 else "srv"
                tool_name = parts[2] if len(parts) > 2 else "tool"
                fid = (
                    "echo_fixture"
                    if "echo" in target_id or "reverse" in target_id
                    else "math_fixture"
                )
                connectors.append(
                    {
                        "kind": "mcp_fixture",
                        "server_name": server_name,
                        "fixture_id": fid,
                        "tools": [tool_name],
                    }
                )
            elif target_id.startswith("openapi."):
                parts = target_id.split(".")
                prefix = parts[1] if len(parts) > 1 else "api"
                op_id = parts[2] if len(parts) > 2 else "op"
                fid = "weather_fixture" if "weather" in target_id else "petstore_fixture"
                connectors.append(
                    {
                        "kind": "openapi_fixture",
                        "prefix": prefix,
                        "fixture_id": fid,
                        "operations": [op_id],
                    }
                )

        if connectors:
            conn_path = dest / "connectors.json"
            conn_path.write_text(json.dumps(connectors, indent=2, sort_keys=True), encoding="utf-8")
            generated_files.append("connectors.json")

        # 1. workflow.json
        wf_path = dest / "workflow.json"
        wf_path.write_text(json.dumps(sanitized_spec, indent=2, sort_keys=True), encoding="utf-8")
        generated_files.append("workflow.json")

        # 2. requirements.txt
        reqs_path = dest / "requirements.txt"
        reqs_path.write_text("nexura-runtime>=0.4.0\n", encoding="utf-8")
        generated_files.append("requirements.txt")

        # 3. pyproject.toml
        pyproject_path = dest / "pyproject.toml"
        pyproject_content = f"""[project]
name = "{workflow_id.replace("_", "-")}"
version = "0.1.0"
description = "Exported standalone Nexura AgentKit workflow bundle"
dependencies = [
    "nexura-runtime>=0.4.0",
]
requires-python = ">=3.12"
"""
        pyproject_path.write_text(pyproject_content, encoding="utf-8")
        generated_files.append("pyproject.toml")

        # 4. run.py - Self-contained runner script
        runner_path = dest / "run.py"
        runner_content = """# Standalone portable workflow runner
import asyncio
import json
import os
import sys
from pathlib import Path

from nexura_runtime.workflow import WorkflowEngine, WorkflowSpec
from nexura_runtime.studio.builtin_modules import get_default_registry


def resolve_credential_refs(data):
    if isinstance(data, dict):
        if "ref" in data and len(data) == 1 and isinstance(data["ref"], str):
            ref_name = data["ref"]
            val = (
                os.environ.get(ref_name)
                or os.environ.get(ref_name.upper())
                or os.environ.get(ref_name.lower())
            )
            return val if val is not None else data
        return {k: resolve_credential_refs(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [resolve_credential_refs(item) for item in data]
    return data


_VALUE_OPTIONS = ("--handlers", "--state-update")


def _option_values(name: str) -> list[str]:
    args = sys.argv[1:]
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == name]


def _positional_args() -> list[str]:
    args = sys.argv[1:]
    values = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("--"):
            values.append(arg)
    return values


async def main() -> None:
    if "--print-package-location" in sys.argv:
        import nexura_runtime
        print(f"Package import location: {nexura_runtime.__file__}")

    # 1. Load exported workflow spec
    spec_file = Path(__file__).parent / "workflow.json"
    if not spec_file.exists():
        print(f"Error: workflow spec not found at {spec_file}", file=sys.stderr)
        sys.exit(1)

    spec_dict = json.loads(spec_file.read_text(encoding="utf-8"))
    resolved_dict = resolve_credential_refs(spec_dict)
    spec = WorkflowSpec.from_dict(resolved_dict)

    # 2. Bind built-in handlers, plus any trusted handler modules the operator names
    #    with --handlers package.module:attribute (never chosen by workflow.json).
    registry = get_default_registry()
    for target in _option_values("--handlers"):
        registry.include_from(target)

    # 3. Reconnect any bundled connectors
    connectors_file = Path(__file__).parent / "connectors.json"
    if connectors_file.exists():
        conn_data = json.loads(connectors_file.read_text(encoding="utf-8"))
        for item in conn_data:
            kind = item.get("kind")
            if kind == "mcp_fixture":
                from nexura_agents.mcp.transports import McpInMemoryTransport
                from nexura_agents.mcp.client import McpClientSession
                from nexura_agents.mcp.module import McpToolModule
                from nexura_runtime.service.api.integrations import _create_mcp_fixture_server

                fid = item.get("fixture_id", "math_fixture")
                srv_name = item.get("server_name", "calc")

                def make_client_factory(fixture_id: str):
                    async def _client_factory():
                        c_pair, s_pair = McpInMemoryTransport.create_pair()
                        srv = _create_mcp_fixture_server(fixture_id, s_pair)
                        srv_loop_task = asyncio.create_task(srv.serve_loop())
                        cli = McpClientSession(c_pair)
                        await cli.initialize()

                        async def _cleanup():
                            await cli.close()
                            await srv.close()
                            srv_loop_task.cancel()

                        return cli, _cleanup

                    return _client_factory

                # Pin each tool to the definition the fixture server actually lists, so a
                # changed tool is refused at call time.
                d_cli, d_cleanup = await make_client_factory(fid)()
                try:
                    discovered = {t.name: t for t in await d_cli.list_tools()}
                finally:
                    await d_cleanup()
                for t_name in item.get("tools", []):
                    t_def = discovered.get(t_name)
                    if t_def is None:
                        raise SystemExit(f"MCP fixture '{fid}' has no tool '{t_name}'.")
                    mod = McpToolModule(
                        client=None,
                        tool_def=t_def,
                        server_name=srv_name,
                        session_factory=make_client_factory(fid),
                    )
                    registry.register(mod.manifest.module_id, mod)
            elif kind == "openapi_fixture":
                import httpx
                from nexura_agents.openapi.module import import_openapi_operations
                from nexura_runtime.service.api.integrations import (
                    WEATHER_FIXTURE_SPEC,
                    PETSTORE_FIXTURE_SPEC,
                )

                fid = item.get("fixture_id", "petstore_fixture")
                spec_data = WEATHER_FIXTURE_SPEC if "weather" in fid else PETSTORE_FIXTURE_SPEC
                prefix = item.get("prefix", "api")

                def mock_fixture_transport(req: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        200,
                        json={"status": "success", "petId": "1", "name": "Fido", "fixture": True},
                        headers={"Content-Type": "application/json", "X-Fixture": "true"},
                    )

                transport = httpx.MockTransport(mock_fixture_transport)
                mods = import_openapi_operations(spec_data, prefix=prefix, transport=transport)
                for m in mods:
                    registry.register(m.manifest.module_id, m)
                    registry.register(f"openapi.{prefix}.{m.op_spec.operation_id}", m)

    # 4. Initialize engine with local checkpoint store
    from nexura_runtime.workflow import SQLiteWorkflowStore
    store = SQLiteWorkflowStore(Path(__file__).parent / "workflow.db")
    engine = WorkflowEngine(spec=spec, registry=registry, store=store)

    # 5. Execute workflow
    state_arg = _positional_args()[0] if _positional_args() else None
    initial_state = json.loads(state_arg) if state_arg else {}
    print(f"Starting standalone workflow '{spec.name}'...")
    result = await engine.run(initial_state=initial_state)

    if result.status == "paused" and result.pending_approval is not None:
        print(f"Approval ID: {result.pending_approval.approval_id}")
        print(f"Node: {result.pending_approval.node_id}")
        if "--approve" in sys.argv:
            state_update_data = None
            if "--state-update" in sys.argv:
                try:
                    idx = sys.argv.index("--state-update")
                    if idx + 1 < len(sys.argv):
                        state_update_data = json.loads(sys.argv[idx + 1])
                except Exception as ex:
                    print(f"Warning: could not parse --state-update: {ex}", file=sys.stderr)
            print("Auto-approving via --approve flag...")
            first = result.pending_approval
            result = await engine.resume(
                result.workflow_run_id,
                first.approval_id,
                decision="approve",
                state_update=state_update_data,
            )
            again = result.pending_approval
            if (
                state_update_data
                and result.status == "paused"
                and again is not None
                and again.node_id == first.node_id
                and again.approval_id != first.approval_id
                and all(again.input_state.get(k) == v for k, v in state_update_data.items())
            ):
                # The edit changed what the node reads, so the engine asked for a fresh
                # approval of the edited inputs. They are the inputs this operator wrote
                # with --state-update, so --approve approves them (once, this node only).
                print(f"Approval ID: {again.approval_id} (edited inputs)")
                result = await engine.resume(
                    result.workflow_run_id, again.approval_id, decision="approve"
                )
            print(f"Resumed Status: {result.status}")

    print(f"Status: {result.status}")
    print(f"Steps: {result.step_count}")
    print(f"State: {json.dumps(result.state, indent=2)}")

    if result.status == "failed":
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
"""
        runner_path.write_text(runner_content, encoding="utf-8")
        generated_files.append("run.py")

        # 5. README.md with requirements and credential-reference instructions
        readme_path = dest / "README.md"
        creds_list = sorted(list(found_credentials))
        if creds_list:
            cred_instructions = (
                "The following credential references are used in this workflow.\n"
                "Before execution, ensure they are supplied as environment variables "
                "or secret vaults:\n\n"
                + "\n".join(f'- `{c}`: `export {c}="your-{c.lower()}-here"`' for c in creds_list)
            )
        else:
            cred_instructions = (
                "No external credential references required. The workflow runs self-contained."
            )

        readme_content = f"""# Standalone Workflow Bundle: {workflow_id}

- **Source Spec Hash**: `{source_spec_hash}`
- **Exported Spec Hash (Scrubbed)**: `{exported_spec_hash}`

## Runtime & Dependency Requirements

- **Python**: `>= 3.12`
- **SDK**: `nexura-runtime >= 0.4.0`
- **TypeScript Consumer (optional)**: `@liceron/nexura-sdk >= 0.4.0`

## Credential-Reference Instructions

{cred_instructions}

## Installation & Setup

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Execute workflow via Python runner:
```bash
python run.py '{{"order_id": "ORD-123", "amount": 50}}'
```

3. Or execute via Nexura CLI:
```bash
nexura-runtime workflow run . --state '{{"order_id": "ORD-123", "amount": 50}}'
```
"""
        readme_path.write_text(readme_content, encoding="utf-8")
        generated_files.append("README.md")

        return ExportBundle(
            target_dir=dest,
            workflow_id=workflow_id,
            spec_hash=exported_spec_hash,
            files=generated_files,
            source_spec_hash=source_spec_hash,
            exported_spec_hash=exported_spec_hash,
        )
