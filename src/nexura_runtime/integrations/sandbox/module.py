# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for isolated sandbox execution."""

from __future__ import annotations

import asyncio
import contextlib
import io
import time
import tokenize
import weakref
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import ContractError
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.errors import ExecutionCancelledError

from nexura_runtime.integrations.sandbox.backends import (
    SHARED_SERVICE_MIN_ISOLATION,
    backend_isolation,
    build_backend,
    isolation_rank,
    select_backend,
)
from nexura_runtime.integrations.sandbox.errors import (
    CapabilityUnavailableError,
    ExecutionTimeoutError,
    InputLimitExceededError,
    MemoryLimitExceededError,
    OutputLimitExceededError,
    SandboxError,
    SecurityViolationError,
    UnsupportedFeatureError,
)
from nexura_runtime.integrations.sandbox.models import (
    ExecutionRequest,
    SandboxConfig,
)
from nexura_runtime.integrations.sandbox.worker import WorkerBackend

MODULE_ID = "nexura.tools.sandbox.execute"

#: Node config keys that only the operator may set (through the ``SandboxConfig`` passed to
#: ``SandboxExecutionModule``). A flow author setting them is refused, never honoured.
OPERATOR_ONLY_CONFIG_KEYS = frozenset(
    {*SandboxConfig.model_fields, "runsc_command_prefix", "allow_without_cgroups"}
)


def check_input_limits(inputs: dict[str, Any], config: SandboxConfig) -> None:
    """Refuse code + stdin + files over the operator caps before approval or any worker."""
    files = inputs.get("files") or {}
    if not isinstance(files, dict):
        raise TypeError("files must be an object mapping relative paths to text")
    if len(files) > config.max_input_files:
        raise InputLimitExceededError(
            "too many input files", limit=config.max_input_files, actual=len(files)
        )
    total = 0
    for value in (inputs.get("code", ""), inputs.get("stdin", ""), *files.keys(), *files.values()):
        if not isinstance(value, str):
            raise TypeError("code, stdin and input file names/contents must be strings")
        total += len(value.encode("utf-8", errors="replace"))
    if total > config.max_input_bytes:
        raise InputLimitExceededError(
            "code, stdin and input files are too large", limit=config.max_input_bytes, actual=total
        )


def _needs_full_python(code: str) -> bool:
    """Conservatively recognize full-Python syntax without building an untrusted AST.

    Token scanning stays bounded by the input cap and never executes/compiles user code
    in the service. A ``for`` inside parentheses can also be a comprehension in a call;
    selecting operator-enabled WASI for that case is harmless.
    """
    parentheses = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(code).readline):
            if token.type == tokenize.NAME and (
                token.string in {"class", "yield"} or token.string == "for" and parentheses > 0
            ):
                return True
            if token.type == tokenize.OP:
                if token.string == "(":
                    parentheses += 1
                elif token.string == ")":
                    parentheses = max(0, parentheses - 1)
    except (tokenize.TokenError, SyntaxError, ValueError):
        return False
    return False


class SandboxExecutionModule(BaseModule):
    """Executes untrusted code in disposable isolated workers with strict boundaries.

    Backends (P27): with ``SandboxConfig.backends`` set by the operator, the module selects
    the first enabled backend that is available and meets ``SandboxConfig.min_isolation``
    (``monty`` first by default; ``gvisor_oci`` for operators that require ``gvisor``) and
    becomes visible in Studio; if none qualifies every call returns ``CAPABILITY_UNAVAILABLE``
    and it stays hidden. Without ``backends`` (the default) the module keeps the legacy
    gVisor OCI worker, fails closed if runsc or the rootfs is missing, and stays hidden
    (``internal``). An explicit ``worker=`` is the operator's choice; a non-isolating worker
    (``TrustedSubprocessWorker``) is never selected automatically, and with ``backends`` set
    an explicit worker below ``min_isolation`` is refused unless ``allow_unisolated=True``.

    At most ``SandboxConfig.max_concurrent_executions`` calls (default 4) run at once per
    workspace; further calls queue for a free slot, bounded by the caller's deadline.
    ``refuse_before_approval`` lets the workflow engine refuse inputs this module would
    refuse (oversized code, unavailable backend) before it asks anyone to approve them.

    Every call runs code, so the manifest declares ``always_write``: a workflow pauses for a
    human approval bound to the resolved inputs (the code) and a direct call needs an
    ``ExecutionContext(approval_granted=True)``. Limits, backend, runsc binary, rootfs and
    command prefix come from the operator ``config`` only; inputs may lower the timeout,
    memory and output limits, never raise them, and may not set environment variables.
    """

    def __init__(
        self,
        worker: WorkerBackend | None = None,
        *,
        config: SandboxConfig | None = None,
        module_id: str = "nexura.tools.sandbox.execute",
        version: str = "1.0.0",
        effect_policy: EffectPolicy | None = None,
    ) -> None:
        self.config = config.model_copy(deep=True) if config is not None else SandboxConfig()
        #: The operator enabled the backend selection (``SandboxConfig.backends``).
        self.enabled = worker is None and self.config.backends is not None
        #: Why no enabled backend qualified, or ``None``.
        self.selection_error: str | None = None
        # The operator minimum always applies to selected backends. To an explicit worker it
        # applies when the operator set ``min_isolation`` or enabled ``backends`` (unless
        # ``allow_unisolated``); older callers that pass trusted workers set neither.
        self._enforce_min_isolation = (
            worker is None
            or "min_isolation" in self.config.model_fields_set
            or (self.config.backends is not None and not self.config.allow_unisolated)
        )
        # Concurrency slots per event loop and workspace (asyncio primitives are loop-bound).
        self._slots: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
        ] = weakref.WeakKeyDictionary()
        self.worker: WorkerBackend
        if worker is not None:
            self.worker = worker
        elif self.enabled:
            try:
                self.worker = select_backend(self.config)
            except CapabilityUnavailableError as exc:
                self.selection_error = exc.reason
                # Never runs: invoke refuses first. gVisor is in nexura-sandbox.
                self.worker = build_backend("gvisor_oci")
        else:
            # The legacy gVisor worker (nexura-sandbox); without that package, a placeholder
            # that refuses every call with "pip install nexura-sandbox".
            self.worker = build_backend("gvisor_oci")
        if effect_policy is None:
            # A disposable worker with network off leaves nothing behind, so a retry repeats
            # the same approved code harmlessly; with operator-enabled network it may not.
            effect_policy = (
                "non_idempotent_write" if self.config.allow_network else "idempotent_write"
            )
        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="tool",
            title="Isolated Code Execution Sandbox",
            description=(
                "Executes untrusted Python calculations inside an isolated disposable worker "
                "chosen by the operator, with time, memory, output, network and process "
                "boundaries; print() output is returned. The default Monty backend runs a "
                "Python subset: no class inheritance, generators, third-party packages or "
                "stdin, and a partial standard library. Every call needs human approval; "
                "limits are set by the operator."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "language": {"type": "string", "default": "python"},
                    "stdin": {"type": "string", "default": ""},
                    # Optional; each may only lower the operator's limit (min is used).
                    "timeout_seconds": {"type": "number"},
                    "max_memory_mb": {"type": "integer"},
                    "max_output_bytes": {"type": "integer"},
                    "allow_network": {"type": "boolean", "default": False},
                    "files": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["code"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "duration_ms": {"type": "number"},
                    "worker_id": {"type": "string"},
                    "isolated": {"type": "boolean"},
                    "backend": {"type": "string"},
                    "isolation": {"type": "string"},
                    "limits_advisory": {"type": "boolean"},
                },
                "required": ["status", "exit_code"],
            },
            # Node config sets nothing: limits, backend and binaries are operator-only, so a
            # workflow that sets any key is refused when it is saved, not after an approval.
            config_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            capabilities={"network": self.config.allow_network, "sandbox_isolated": True},
            effect_policy=effect_policy,
            always_write=True,
            # Hidden from Studio's palette (and refused as an agent tool) unless the operator
            # enabled a backend that is available here. Either way it stays registered so
            # saved workflows validate, and every call still needs an approval.
            internal=not self.visible,
        )

    @property
    def visible(self) -> bool:
        """True when the operator enabled a backend and one qualified (Studio shows it)."""
        return self.enabled and self.selection_error is None

    @property
    def selected_backend(self) -> str:
        """Name of the backend that runs the code (``unavailable`` when none qualified)."""
        if self.selection_error is not None:
            return "unavailable"
        return str(getattr(self.worker, "backend_name", type(self.worker).__name__))

    @property
    def selected_isolation(self) -> str:
        """Isolation level of the backend that runs the code (``none`` when none qualified)."""
        if self.selection_error is not None:
            return "none"
        return backend_isolation(self.worker)

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    def describe(self) -> ModuleManifest:
        """Module manifest inspector."""
        return self._manifest

    def _effective_config(self, inputs: dict[str, Any]) -> SandboxConfig:
        """Operator config with the timeout/memory/output limits lowered by ``inputs``."""
        operator = self.config
        timeout_sec = operator.timeout_seconds
        if inputs.get("timeout_seconds") is not None:
            timeout_sec = min(timeout_sec, float(inputs["timeout_seconds"]))
        max_mem = operator.max_memory_mb
        if inputs.get("max_memory_mb") is not None:
            max_mem = min(max_mem, int(inputs["max_memory_mb"]))
        max_out = operator.max_output_bytes
        if inputs.get("max_output_bytes") is not None:
            max_out = min(max_out, int(inputs["max_output_bytes"]))
        backend = operator.backend
        if "backend" not in operator.model_fields_set:
            backend = getattr(self.worker, "backend_name", backend)
        # Network only when the operator allows it and the input asks for it (default off).
        allow_network = operator.allow_network and inputs.get("allow_network") is True
        return SandboxConfig.model_validate(
            {
                **operator.model_dump(),
                "timeout_seconds": timeout_sec,
                "max_memory_mb": max_mem,
                "max_output_bytes": max_out,
                "allow_network": allow_network,
                "backend": backend,
            }
        )

    def _slot(self, workspace_id: str | None) -> asyncio.Semaphore:
        """The concurrency semaphore for ``workspace_id`` on the running event loop."""
        loop = asyncio.get_running_loop()
        per_loop = self._slots.get(loop)
        if per_loop is None:
            per_loop = {}
            self._slots[loop] = per_loop
        key = workspace_id or ""
        slot = per_loop.get(key)
        if slot is None:
            slot = asyncio.Semaphore(self.config.max_concurrent_executions)
            per_loop[key] = slot
        return slot

    def refuse_before_approval(
        self, inputs: dict[str, Any], config: dict[str, Any] | None = None
    ) -> ModuleResult | None:
        """The failure ``invoke`` would return for these inputs whatever the approval, or None.

        The workflow engine calls this before it pauses an always-write step, so a reviewer
        is never asked to approve code that will be refused: operator-only node config,
        ``env``, no qualifying backend, isolation below the operator minimum, network the
        policy denies, invalid limit inputs, and code/stdin/files over the input caps.
        """
        refusal, _ = self._pre_approval_checks(inputs, config or {})
        return refusal

    def _pre_approval_checks(
        self, inputs: dict[str, Any], config: dict[str, Any]
    ) -> tuple[ModuleResult | None, SandboxConfig | None]:
        """(failure, None) when the call is refused regardless of approval, else (None, cfg)."""
        # Backend, runsc binary, rootfs, prefix and limits are operator settings: a node's
        # config (written by a flow author) cannot choose what runs.
        refused = sorted(k for k in (config or {}) if k in OPERATOR_ONLY_CONFIG_KEYS)
        if refused:
            return ModuleResult.failure(
                "INVALID_CONFIG",
                f"Node config cannot set {', '.join(refused)}; the sandbox backend, runsc "
                "binary, rootfs, command prefix and limits are set by the operator "
                "(SandboxExecutionModule(config=SandboxConfig(...)), NEXURA_RUNSC_PATH, "
                "NEXURA_OCI_ROOTFS_PATH or PATH).",
            ), None

        # Model/flow-supplied environment variables are never passed to the worker.
        if inputs.get("env"):
            return ModuleResult.failure(
                "SECURITY_VIOLATION_ENV",
                "Inputs cannot set environment variables for sandboxed code.",
            ), None

        if self.selection_error is not None:
            return ModuleResult.failure("CAPABILITY_UNAVAILABLE", self.selection_error), None
        if self._enforce_min_isolation and isolation_rank(self.selected_isolation) < isolation_rank(
            self.config.min_isolation
        ):
            return ModuleResult.failure(
                "CAPABILITY_UNAVAILABLE",
                f"Sandbox backend '{self.selected_backend}' (isolation "
                f"'{self.selected_isolation}') is below the operator minimum "
                f"'{self.config.min_isolation}'; refusing to run code. An operator who "
                "accepts an unisolated worker sets SandboxConfig(allow_unisolated=True).",
            ), None

        # Prevent untrusted input enabling policy-denied network
        manifest_network_allowed = bool(self._manifest.capabilities.get("network", False))
        input_allow_network = bool(inputs.get("allow_network", False))
        if input_allow_network and not manifest_network_allowed:
            return ModuleResult.failure(
                "SECURITY_VIOLATION_NETWORK_POLICY",
                "Untrusted input cannot enable network access when network is denied by policy.",
            ), None

        try:
            sandbox_config = self._effective_config(inputs)
        except (TypeError, ValueError, OverflowError) as exc:
            return ModuleResult.failure(
                "INVALID_INPUT", f"Invalid sandbox limit input: {exc}"
            ), None

        # Oversized code/files are refused before anyone is asked to approve them.
        try:
            check_input_limits(inputs, sandbox_config)
        except InputLimitExceededError as exc:
            return ModuleResult.failure("INPUT_LIMIT_EXCEEDED", str(exc)), None
        except TypeError as exc:
            return ModuleResult.failure("INVALID_INPUT", str(exc)), None
        return None, sandbox_config

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        context.check_cancelled()

        refusal, sandbox_config = self._pre_approval_checks(inputs, config or {})
        if refusal is not None:
            return refusal
        assert sandbox_config is not None

        # Pass deadline from ExecutionContext
        remaining = context.remaining_seconds()
        if remaining is not None and remaining <= 0.0:
            return ModuleResult.failure(
                "EXECUTION_TIMEOUT",
                "ExecutionContext deadline has already expired before invocation.",
            )

        try:
            context.require_write_approval("running code in the sandbox")
        except ContractError as err:
            return ModuleResult.failure(err.code, str(err))

        # At most ``max_concurrent_executions`` run at once per workspace; the rest queue.
        slot = self._slot(context.workspace_id)
        try:
            if remaining is not None:
                await asyncio.wait_for(slot.acquire(), timeout=remaining)
            else:
                await slot.acquire()
        except TimeoutError:
            return ModuleResult.failure(
                "EXECUTION_TIMEOUT",
                "The deadline passed while waiting for a free sandbox slot (at most "
                f"{self.config.max_concurrent_executions} executions run at once per "
                "workspace).",
            )
        try:
            return await self._execute(context, inputs, sandbox_config)
        finally:
            slot.release()

    async def _execute(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        sandbox_config: SandboxConfig,
    ) -> ModuleResult:
        """Run approved, checked inputs in the worker and map its outcome to a result."""
        remaining = context.remaining_seconds()
        if remaining is not None:
            if remaining <= 0.0:
                return ModuleResult.failure(
                    "EXECUTION_TIMEOUT",
                    "ExecutionContext deadline expired while waiting for a sandbox slot.",
                )
            sandbox_config = sandbox_config.model_copy(
                update={"timeout_seconds": min(sandbox_config.timeout_seconds, remaining)}
            )

        request = ExecutionRequest(
            code=inputs.get("code", ""),
            language=inputs.get("language", "python"),
            stdin=inputs.get("stdin", ""),
            files=inputs.get("files", {}),
        )

        try:
            context.check_cancelled()
            execution_started = time.monotonic()
            worker = self.worker
            if (
                self.selected_backend == "monty"
                and "wasi" in (self.config.backends or [])
                and _needs_full_python(request.code)
            ):
                worker = select_backend(sandbox_config.model_copy(update={"backends": ["wasi"]}))
            try:
                result = await worker.execute(request, sandbox_config, cancellation_token=context)
            except UnsupportedFeatureError:
                # Only an operator-enabled WASI may retry Monty's Python-subset refusal.
                # Keep this call's approval, remaining deadline, caps and isolation floor.
                if getattr(worker, "backend_name", None) != "monty" or "wasi" not in (
                    self.config.backends or []
                ):
                    raise
                fallback = select_backend(sandbox_config.model_copy(update={"backends": ["wasi"]}))
                budget = sandbox_config.timeout_seconds - (time.monotonic() - execution_started)
                remaining = context.remaining_seconds()
                if remaining is not None:
                    budget = min(budget, remaining)
                if budget <= 0:
                    raise ExecutionTimeoutError(sandbox_config.timeout_seconds) from None
                sandbox_config = sandbox_config.model_copy(update={"timeout_seconds": budget})
                result = await fallback.execute(request, sandbox_config, cancellation_token=context)

            return ModuleResult.success(
                {
                    "status": "completed",
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "duration_ms": result.duration_ms,
                    "worker_id": result.worker_id,
                    "isolated": result.isolated,
                    "backend": result.backend,
                    "isolation": result.isolation or backend_isolation(self.worker),
                    "limits_advisory": bool(result.details.get("limits_advisory", False)),
                }
            )

        except CapabilityUnavailableError as exc:
            return ModuleResult.failure("CAPABILITY_UNAVAILABLE", str(exc))
        except ExecutionTimeoutError as exc:
            return ModuleResult.failure("EXECUTION_TIMEOUT", str(exc))
        except OutputLimitExceededError as exc:
            return ModuleResult.failure("OUTPUT_LIMIT_EXCEEDED", str(exc))
        except MemoryLimitExceededError as exc:
            return ModuleResult.failure("MEMORY_LIMIT_EXCEEDED", str(exc))
        except UnsupportedFeatureError as exc:
            return ModuleResult.failure("UNSUPPORTED_FEATURE", str(exc))
        except InputLimitExceededError as exc:
            return ModuleResult.failure("INPUT_LIMIT_EXCEEDED", str(exc))
        except SecurityViolationError as exc:
            return ModuleResult.failure(
                f"SECURITY_VIOLATION_{exc.violation_type.upper()}", str(exc)
            )
        except ExecutionCancelledError as exc:
            return ModuleResult.failure("EXECUTION_CANCELLED", str(exc))
        except SandboxError as exc:
            return ModuleResult.failure("SANDBOX_ERROR", str(exc))
        except Exception as exc:
            return ModuleResult.failure("WORKER_ERROR", f"Sandbox worker execution failed: {exc}")


def sandbox_modules_in(*registries: Any) -> list[SandboxExecutionModule]:
    """Sandbox modules registered in versioned module registries or workflow registries.

    A workflow ``ModuleRegistry`` is searched with the versioned registry behind it (its
    ``versioned_registry``, ``default_registry`` unless one was passed), where the sandbox
    registers itself on import, and any registry it wraps (``_inner``).
    """
    found: list[SandboxExecutionModule] = []
    pending: list[Any] = list(registries)
    seen: set[int] = set()
    while pending:
        registry = pending.pop(0)
        if registry is None or id(registry) in seen:
            continue
        seen.add(id(registry))
        candidates: list[Any] = []
        has = getattr(registry, "has", None)
        if callable(has) and hasattr(registry, "get_entry"):
            with contextlib.suppress(Exception):
                if has(MODULE_ID):
                    candidates.append(registry.get(MODULE_ID))
            # Sandbox instances registered under another module id.
            for entry in dict(getattr(registry, "_modules", {}) or {}).values():
                candidates.append(getattr(entry, "_instance", None))
        for attr in ("_handlers", "_overrides"):
            candidates.extend(dict(getattr(registry, attr, {}) or {}).values())
        for module in candidates:
            if isinstance(module, SandboxExecutionModule) and module not in found:
                found.append(module)
        with contextlib.suppress(Exception):
            pending.append(getattr(registry, "versioned_registry", None))
        pending.append(getattr(registry, "_inner", None))
    return found


def assert_sandbox_allowed_for_profile(profile: str, *registries: Any) -> None:
    """Refuse a shared-service start whose sandbox could run code below ``gvisor``.

    The authenticated shared (multi-tenant) service needs gVisor or a micro-VM, never Monty
    alone. A sandbox module whose selected backend (or explicit worker) is weaker raises
    ``SandboxError`` with the fix; a module with no qualifying backend never runs code and
    is allowed. ``create_api_app`` calls this at startup with the service registry.
    Test-only command prefixes and cgroup bypasses are refused even when the
    process environment enables the real-gVisor test profile.
    """
    if profile != "authenticated-shared-service":
        return
    floor = isolation_rank(SHARED_SERVICE_MIN_ISOLATION)
    for module in sandbox_modules_in(*registries):
        if module.config.extra.get("allow_without_cgroups") or module.config.extra.get(
            "runsc_command_prefix"
        ):
            raise SandboxError(
                "The authenticated-shared-service profile refuses the gVisor test-only "
                "command prefix and cgroup bypass, regardless of NEXURA_REAL_GVISOR."
            )
        if module.selection_error is not None:
            continue
        if isolation_rank(module.selected_isolation) < floor:
            raise SandboxError(
                "The authenticated-shared-service profile requires the code sandbox to use "
                f"'{SHARED_SERVICE_MIN_ISOLATION}' isolation or stronger (gVisor or a "
                "micro-VM, never Monty alone), but "
                f"'{module.selected_backend}' provides '{module.selected_isolation}'. Set "
                "NEXURA_SANDBOX_MIN_ISOLATION=gvisor (or SandboxConfig(min_isolation='gvisor')) "
                "and configure runsc, or do not enable the sandbox.",
                details={
                    "backend": module.selected_backend,
                    "isolation": module.selected_isolation,
                },
            )
