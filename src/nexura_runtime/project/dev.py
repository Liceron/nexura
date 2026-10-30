# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""``nexura-runtime dev``: the local REST service and the bundled Studio for one project.

Both servers are the existing ones (``create_api_app`` from ``nexura-runtime serve`` and
``nexura_studio.create_app`` from ``nexura-studio``), started in one process on loopback with
the project's handlers and workflows registered. ``--container`` is the one exception, used
by the Dockerfile that ``nexura-runtime deploy`` writes: Studio alone on 0.0.0.0, refused
without ``NEXURA_STUDIO_TOKEN``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexura_agents.errors import WorkflowValidationError

from nexura_runtime.project.config import (
    ProjectConfig,
    ProjectError,
    find_project,
    project_registry,
    project_workflows,
)
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec

LOOPBACK = "127.0.0.1"
#: Container mode only, and only with a token.
CONTAINER_HOST = "0.0.0.0"
TOKEN_ENV = "NEXURA_STUDIO_TOKEN"


@dataclass
class DevServer:
    label: str
    app: Any
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK if self.host == CONTAINER_HOST else self.host}:{self.port}"


@dataclass
class DevPlan:
    project: ProjectConfig | None
    registry: ModuleRegistry
    workflows: dict[str, WorkflowSpec]
    servers: list[DevServer] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class DevError(RuntimeError):
    """``dev`` cannot start as asked; ``code`` is the exit code."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def _studio_factory() -> Any | None:
    """The bundled Studio's ``create_app``, or None when ``nexura-studio`` is not installed.

    Loaded through the optional-package seam: the runtime never imports the Studio package
    by name (it depends on the runtime, not the other way round).
    """
    from nexura_runtime.modules.optional_packages import STUDIO_PACKAGE, find_package

    studio = find_package(STUDIO_PACKAGE)
    return None if studio is None else studio.create_app


def plan_dev(
    project_dir: Path,
    *,
    data_dir: Path | None = None,
    port: int = 8765,
    studio_port: int = 8000,
    studio: bool = True,
    container: bool = False,
    environ: Mapping[str, str] | None = None,
) -> DevPlan:
    """Build the apps ``dev`` serves (nothing listens until :func:`serve_all`)."""
    env = os.environ if environ is None else environ
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:
        raise DevError(
            "'nexura-runtime dev' needs the studio extra: pip install \"nexura-runtime[studio]\"",
            code=1,
        ) from exc
    if not project_dir.is_dir():
        raise DevError(f"project directory {project_dir} does not exist")
    try:
        project = find_project(project_dir)
        registry = project_registry(project)
        workflows = project_workflows(project)
        for workflow_id, spec in workflows.items():
            try:
                spec.validate(registry)
            except WorkflowValidationError as exc:
                raise ProjectError(f"workflow '{workflow_id}' is not valid: {exc}") from exc
    except ProjectError as exc:
        raise DevError(str(exc)) from exc
    data = (data_dir if data_dir is not None else project_dir / ".nexura").resolve()
    data.mkdir(parents=True, exist_ok=True)
    plan = DevPlan(project=project, registry=registry, workflows=workflows)
    if project is None:
        plan.notes.append(
            f"No [tool.nexura] table in {project_dir / 'pyproject.toml'}: starting with the "
            "built-in modules only (create a project with 'nexura-runtime init')."
        )

    create_studio = _studio_factory()
    if container:
        if not (env.get(TOKEN_ENV) or "").strip():
            raise DevError(
                f"--container listens on {CONTAINER_HOST} and needs {TOKEN_ENV}; set it to a "
                "long random value (see DEPLOY.md)"
            )
        if create_studio is None:
            raise DevError("--container needs the nexura-studio package: pip install nexura-studio")
        studio = True
    elif studio and create_studio is None:
        plan.notes.append(
            "The bundled Studio (package nexura-studio) is not installed, so only the REST "
            "service starts. Install it with: pip install nexura-studio"
        )
        studio = False

    from nexura_runtime.service.auth import UnauthenticatedNetworkError

    if not container:
        from nexura_runtime.service.api.app import create_api_app
        from nexura_runtime.service.api.chat_models import chat_models_from_environment

        try:
            service = create_api_app(
                # Each app gets its own registry: the service adds modules to the one it gets.
                registry=project_registry(project),
                db_path=data / "service.sqlite3",
                chat_models=chat_models_from_environment(None, env),
                profile="local-owner",
                listen_host=LOOPBACK,
            )
        except (UnauthenticatedNetworkError, ValueError) as exc:
            raise DevError(str(exc)) from exc
        plan.servers.append(DevServer("Service", service, LOOPBACK, port))
    if studio and create_studio is not None:
        host = CONTAINER_HOST if container else LOOPBACK
        try:
            studio_app = create_studio(
                registry=project_registry(project),
                db_path=data / "studio.sqlite3",
                allowed_hosts={host, "127.0.0.1", "localhost", "::1", "[::1]"},
                listen_host=host,
            )
        except UnauthenticatedNetworkError as exc:
            raise DevError(str(exc)) from exc
        plan.servers.append(DevServer("Studio", studio_app, host, studio_port))
    return plan


async def seed_workflows(store: Any, workflows: Mapping[str, WorkflowSpec]) -> list[str]:
    """Create each project workflow under its id, or update it when the spec changed."""
    lines = []
    for workflow_id, spec in workflows.items():
        current = await store.get_workflow(workflow_id)
        if current is None:
            await store.create_workflow(spec, workflow_id=workflow_id)
            lines.append(f"registered workflow '{workflow_id}'")
        elif current.get("spec_hash") != spec.canonical_hash():
            await store.update_workflow(workflow_id, spec, int(current["revision"]))
            lines.append(f"updated workflow '{workflow_id}'")
        else:
            lines.append(f"workflow '{workflow_id}' is up to date")
    return lines


async def serve_all(servers: list[DevServer]) -> None:
    """Run every server until one stops; Ctrl-C (or SIGTERM) stops them all cleanly."""
    import uvicorn

    class _Server(uvicorn.Server):
        # One handler for all servers (below) instead of each server capturing signals.
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

        def install_signal_handlers(self) -> None:  # uvicorn releases before capture_signals
            return None

    instances = [
        _Server(uvicorn.Config(s.app, host=s.host, port=s.port, log_level="info")) for s in servers
    ]

    def stop() -> None:
        for instance in instances:
            if instance.should_exit:
                instance.force_exit = True  # a second Ctrl-C
            instance.should_exit = True

    loop = asyncio.get_running_loop()
    handled = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
            handled.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            continue  # not the main thread, or a platform without loop signal handlers
    tasks = [asyncio.create_task(instance.serve()) for instance in instances]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        stop()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for sig in handled:
            loop.remove_signal_handler(sig)


async def _close_stores(plan: DevPlan) -> None:
    for server in plan.servers:
        store = getattr(server.app.state, "store", None)
        if store is not None:
            with contextlib.suppress(Exception):
                await store.close()


def banner(plan: DevPlan, container: bool) -> str:
    lines = ["", "Nexura dev"]
    if plan.project is not None:
        template = f" (template: {plan.project.template})" if plan.project.template else ""
        lines.append(f"  Project:  {plan.project.name}{template} in {plan.project.root}")
    for server in plan.servers:
        if server.label == "Studio":
            lines.append(f"  Studio:   {server.url}  (open this in your browser)")
        else:
            lines.append(
                f"  Service:  {server.url}  (REST API for studio-web and the TypeScript client)"
            )
    if container:
        lines.append(
            "  Container mode: every request needs the token; publish the port on 127.0.0.1 "
            f"only and open {plan.servers[0].url}/#token=<your token>."
        )
    else:
        lines.append("  Loopback only: single user, this machine. Press Ctrl-C to stop.")
    lines.append(
        "  Session: the Studio page reads its per-session token itself; scripts read it from "
        "GET /api/v1/local-session."
    )
    return "\n".join(lines) + "\n"


def run_dev(
    project_dir: Path,
    *,
    data_dir: Path | None = None,
    port: int = 8765,
    studio_port: int = 8000,
    studio: bool = True,
    container: bool = False,
) -> int:
    """Plan, register the project's workflows, print the URLs and serve until stopped."""
    try:
        plan = plan_dev(
            project_dir,
            data_dir=data_dir,
            port=port,
            studio_port=studio_port,
            studio=studio,
            container=container,
        )
    except DevError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.code

    async def main() -> None:
        try:
            for server in plan.servers:
                for line in await seed_workflows(server.app.state.store, plan.workflows):
                    print(f"{server.label}: {line}")
            for note in plan.notes:
                print(f"Note: {note}", file=sys.stderr)
            print(banner(plan, container), flush=True)
            await serve_all(plan.servers)
        finally:
            await _close_stores(plan)

    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(main())
    except WorkflowValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0
