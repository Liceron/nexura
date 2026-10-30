# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Command-line interface for Nexura standalone SDK, export, and execution."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from nexura_agents.errors import WorkflowValidationError

from nexura_runtime.codegen import CodeImportError, python_to_workflow, workflow_to_python
from nexura_runtime.service.api.export import WorkflowExporter
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowSpec


def retention_command(args: argparse.Namespace) -> int:
    """Retention and cleanup for stored run data, bundles, and traces."""
    from nexura_runtime.retention import (
        SUPPORTED_KINDS,
        RetentionError,
        RetentionPolicy,
        apply_retention,
    )

    raw_kinds = [k.strip().lower() for k in args.kinds.split(",") if k.strip()]
    if not raw_kinds:
        print("Error: --kinds cannot be empty.", file=sys.stderr)
        return 1

    for k in raw_kinds:
        if k not in SUPPORTED_KINDS:
            print(
                f"Error: unsupported kind '{k}'. Supported kinds: {', '.join(SUPPORTED_KINDS)}.",
                file=sys.stderr,
            )
            return 1

    try:
        policy = RetentionPolicy(args.older_than_days, kinds=raw_kinds)
        report = apply_retention(
            policy,
            db_path=args.db,
            data_dir=args.data_dir,
            dry_run=not args.apply,
        )
    except RetentionError as exc:
        print(exc.report.summary())
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(report.summary())
    return 1 if report.errors else 0


def repro_command(args: argparse.Namespace) -> int:
    """Export a stored complete recording, or replay a portable bundle offline."""
    from nexura_agents.repro import ReproBundle, export_repro_bundle, replay_repro_bundle

    async def execute() -> int:
        if args.repro_action == "run":
            result = await replay_repro_bundle(ReproBundle.load(args.bundle))
            print(f"reproduced: {result.reproduced}")
            if result.first_divergence:
                print(result.first_divergence, file=sys.stderr)
            for note in result.notes:
                print(f"note: {note}", file=sys.stderr)
            return 0 if result.reproduced else 1
        from nexura_runtime.storage.repository import SQLiteRepository

        if not Path(args.db).is_file():
            raise ValueError(f"Run database does not exist: {args.db}")
        repository = SQLiteRepository(args.db)
        try:
            record = await repository.get_run(args.workspace, args.run_id)
            if record is None or not record.metadata.get("repro"):
                # A service run captured by failed-run capture (serve --capture-failed-runs).
                from nexura_runtime.service.repro_capture import load_captured_bundle

                captured = load_captured_bundle(args.db, args.run_id)
                if captured is not None and captured[0] == args.workspace:
                    ReproBundle(captured[1]).save(args.output)
                    print(f"Wrote repro bundle to: {args.output}")
                    return 0
            if record is None:
                raise ValueError(f"Run not found: {args.run_id}")
            export_repro_bundle(record).save(args.output)
        finally:
            await repository.close()
        print(f"Wrote repro bundle to: {args.output}")
        return 0

    try:
        return asyncio.run(execute())
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        AttributeError,
        IndexError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def export_command(args: argparse.Namespace) -> int:
    """Export a workflow specification to a self-contained portable bundle."""
    source_path = Path(args.source)
    if not source_path.exists():
        print(f"Error: Specification file '{source_path}' does not exist.", file=sys.stderr)
        return 1

    try:
        spec_dict = json.loads(source_path.read_text(encoding="utf-8"))
        spec = WorkflowSpec.from_dict(spec_dict)
    except Exception as exc:
        print(f"Error parsing workflow spec: {exc}", file=sys.stderr)
        return 1

    try:
        registry = _registry_with(args.handlers)
    except WorkflowValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "format", "bundle") == "python":
        return _export_python(spec, args)
    if not args.out:
        print("Error: --out is required for --format bundle.", file=sys.stderr)
        return 1

    dest_dir = Path(args.out).resolve()
    exporter = WorkflowExporter()
    try:
        bundle = exporter.export_bundle(
            spec, target_dir=dest_dir, workflow_id=args.id or spec.name, registry=registry
        )
    except (WorkflowValidationError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Successfully exported workflow '{bundle.workflow_id}' to: {bundle.target_dir}")
    print(f"Canonical Spec Hash: {bundle.spec_hash}")
    print(f"Generated Files: {', '.join(bundle.files)}")
    return 0


def _export_python(spec: WorkflowSpec, args: argparse.Namespace) -> int:
    """Write the workflow as plain Python (to --out, or stdout when omitted)."""
    try:
        source = workflow_to_python(spec, registry_import=args.registry)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not args.out or args.out == "-":
        sys.stdout.write(source)
        return 0
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source, encoding="utf-8")
    print(f"Wrote Python workflow to: {dest}")
    return 0


def import_command(args: argparse.Namespace) -> int:
    """Read a generated Python workflow back to a JSON spec. The file is parsed, never run."""
    source_path = Path(args.source)
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"Error: cannot read '{source_path}': {exc}", file=sys.stderr)
        return 1
    try:
        spec = python_to_workflow(source)
    except CodeImportError as exc:
        print(f"Error: {source_path}: {exc}", file=sys.stderr)
        return 1
    text = spec.to_json() + "\n"
    if not args.out or args.out == "-":
        sys.stdout.write(text)
    else:
        dest = Path(args.out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"Wrote workflow spec to: {dest}")
    return 0


def _registry_with(handler_sources: list[str] | None) -> ModuleRegistry:
    """Built-in handlers plus trusted handler modules named on the command line."""
    registry = get_default_registry()
    for target in handler_sources or []:
        registry.include_from(target)
    return registry


async def _run_workflow_async(
    spec_path: Path, initial_state: dict[str, Any], handler_sources: list[str] | None = None
) -> int:
    try:
        spec_dict = json.loads(spec_path.read_text(encoding="utf-8"))
        spec = WorkflowSpec.from_dict(spec_dict)
    except Exception as exc:
        print(f"Error loading workflow spec: {exc}", file=sys.stderr)
        return 1

    try:
        registry = _registry_with(handler_sources)
    except WorkflowValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    engine = WorkflowEngine(spec=spec, registry=registry)
    result = await engine.run(initial_state)

    print(f"Workflow Status: {result.status}")
    print(f"Step Count: {result.step_count}")
    print("Final State:")
    print(json.dumps(result.state, indent=2))

    return 0 if result.status == "completed" else 1


def run_command(args: argparse.Namespace) -> int:
    """Run an exported workflow bundle or spec file."""
    target = Path(args.target)
    spec_file = target / "workflow.json" if target.is_dir() else target

    if not spec_file.exists():
        print(f"Error: Workflow file '{spec_file}' not found.", file=sys.stderr)
        return 1

    initial_state = json.loads(args.state) if args.state else {}
    return asyncio.run(_run_workflow_async(spec_file, initial_state, args.handlers))


def serve_command(args: argparse.Namespace) -> int:
    """Serve the local REST API that Studio web and the TypeScript client call."""
    try:
        import uvicorn

        from nexura_runtime.service.api.app import create_api_app
    except ImportError:
        print(
            "Error: 'nexura-runtime serve' needs the studio extra: "
            'pip install "nexura-runtime[studio]"',
            file=sys.stderr,
        )
        return 1

    from nexura_runtime.service.api.chat_models import chat_models_from_environment
    from nexura_runtime.service.api.model_pricing import (
        check_model_pricing,
        model_pricing_from_environment,
    )

    try:
        chat_models = chat_models_from_environment(args.chat_model)
        # NEXURA_MODEL_PRICING: operator model prices (JSON keyed by model id).
        model_pricing = model_pricing_from_environment()
        check_model_pricing(model_pricing, chat_models)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    from nexura_agents.contracts import MissingDependencyError

    from nexura_runtime.service.api.operator_token import (
        OPERATOR_TOKEN_ENV,
        OperatorTokenAuthProvider,
        OperatorTokenError,
    )
    from nexura_runtime.service.auth import UnauthenticatedNetworkError

    remote = getattr(args, "auth", "none") == "token"
    token_file = getattr(args, "token_file", None)
    plain_http = bool(getattr(args, "insecure_plain_http", False))
    insecure_network = bool(getattr(args, "insecure_allow_unauthenticated_network", False))
    auth_provider = None
    if not remote and os.environ.get(OPERATOR_TOKEN_ENV, "").strip():
        print(
            f"Warning: {OPERATOR_TOKEN_ENV} is set but --auth token was not given; the service "
            "runs on this machine only, without the token, and child processes inherit the "
            "variable. Unset it, or start with --auth token.",
            file=sys.stderr,
        )
    from nexura_runtime.service.api.local_session import (
        SESSION_TOKEN_REQUIRED_ENV,
        session_token_required,
    )

    try:
        token_off = getattr(args, "no_session_token", False) or not session_token_required()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if remote and token_off:
        print(
            f"Error: --no-session-token and {SESSION_TOKEN_REQUIRED_ENV}=off only apply without "
            "--auth token (the operator token authenticates every request).",
            file=sys.stderr,
        )
        return 2
    if not remote and (token_file or plain_http):
        print(
            "Error: --token-file and --insecure-plain-http only apply with --auth token.",
            file=sys.stderr,
        )
        return 2
    if remote:
        if insecure_network:
            print(
                "Error: --auth token authenticates every request; do not combine it with "
                "--insecure-allow-unauthenticated-network.",
                file=sys.stderr,
            )
            return 2
        try:
            auth_provider = OperatorTokenAuthProvider.from_environment(token_file=token_file)
        except OperatorTokenError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        # Only the hash is kept; child processes (MCP servers, handlers) do not inherit it. The
        # initial environment block (/proc/<pid>/environ) keeps it for the process lifetime,
        # readable by this user and root: --token-file is preferred.
        os.environ.pop(OPERATOR_TOKEN_ENV, None)

    try:
        app = create_api_app(
            db_path=args.db,
            workflow_state_url=getattr(args, "workflow_state_url", None),
            chat_models=chat_models,
            model_pricing=model_pricing,
            profile="local-owner",
            run_trigger_worker=bool(args.worker),
            trigger_poll_interval=args.poll_interval,
            listen_host=args.host,
            capture_failed_runs=getattr(args, "capture_failed_runs", None),
            allow_unauthenticated_network=insecure_network,
            auth_provider=auth_provider,
            allow_plain_http=plain_http,
            require_session_token=False if getattr(args, "no_session_token", False) else None,
            guardrail_policy=getattr(args, "guardrail_policy", None),
        )
    except (UnauthenticatedNetworkError, ValueError, MissingDependencyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    policy = app.state.guardrail_policy
    if policy is not None:
        state = (
            "in shadow mode: reporting only, nothing is blocked"
            if getattr(policy, "mode", "enforce") == "shadow"
            else "enforced"
        )
        print(
            f"Guardrail policy '{policy.name}' {state} (digest {policy.digest[:16]}, "
            f"{len(policy.rules)} rules) from {policy.source or 'code'}."
        )
    if remote:
        # The operator token is never printed or logged.
        print(
            "Remote single-operator mode: every request except /healthz and /readyz needs "
            "Authorization: Bearer <operator token>. Studio asks for the token once per browser "
            "tab. Serve it over HTTPS through a reverse proxy; see "
            "docs/remote-single-operator.md."
        )
        if plain_http:
            print(
                f"WARNING: --insecure-plain-http: listening on {args.host} over plain HTTP. The "
                "operator token crosses the network unencrypted unless TLS is terminated in "
                "front of this port.",
                file=sys.stderr,
            )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    if not app.state.local_session.require_token:
        print(
            "Session token check off: programs on this machine need no X-Nexura-Session "
            "header. Only connections from this machine are accepted, and state-changing "
            "requests from pages on other sites or other local ports are refused (Origin and "
            "Sec-Fetch-Site checks)."
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    # The token itself is not printed: Studio reads it from its own origin.
    print(
        "Per-session token: state-changing requests need the X-Nexura-Session header. Studio "
        "reads it from GET /api/v1/local-session on its own origin; scripts on this machine "
        "can read it there too, or set NEXURA_STUDIO_TOKEN before starting. It changes on "
        "every start unless NEXURA_STUDIO_TOKEN is set."
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def worker_command(args: argparse.Namespace) -> int:
    """Process schedule and webhook trigger jobs against a shared SQLite database."""
    try:
        from nexura_runtime.service.api.app import create_api_app
        from nexura_runtime.service.triggers.worker import TriggerWorker
    except ImportError:
        print(
            "Error: 'nexura-runtime worker' needs the studio extra: "
            'pip install "nexura-runtime[studio]"',
            file=sys.stderr,
        )
        return 1

    try:
        registry = _registry_with(args.handlers)
    except WorkflowValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    from nexura_agents.contracts import MissingDependencyError

    try:
        app = create_api_app(
            db_path=args.db,
            profile="local-owner",
            registry=registry,
            workflow_state_url=getattr(args, "workflow_state_url", None),
            guardrail_policy=getattr(args, "guardrail_policy", None),
        )
    except (ValueError, MissingDependencyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        worker = TriggerWorker(
            app.state,
            poll_interval=args.poll_interval,
            lease_seconds=args.lease_seconds,
            max_attempts=args.max_attempts,
            max_concurrent_jobs=args.max_concurrent_jobs,
            retry_mode=getattr(args, "retry_mode", "continue"),
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    async def _main() -> None:
        try:
            if args.once:
                for outcome in await worker.run_once():
                    suffix = f" error={outcome.error}" if outcome.error else ""
                    print(
                        f"job={outcome.job_id} status={outcome.status} run={outcome.run_id}{suffix}"
                    )
            else:
                stop = asyncio.Event()
                print(f"Trigger worker {worker.worker_id} started on {args.db}", file=sys.stderr)
                await worker.run_forever(stop)
        finally:
            await app.state.store.close()
            close_triggers = getattr(app.state.trigger_store, "close", None)
            if close_triggers is not None:
                await asyncio.to_thread(close_triggers)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
    return 0


def _load_agent(target: str) -> Any:
    """Import ``package.module:attribute``; a zero-argument factory is called for the Agent."""
    import importlib

    module_name, sep, attr = target.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"'{target}' is not 'package.module:attribute'.")
    if "" not in sys.path and str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"cannot import module '{module_name}': {exc}") from exc
    try:
        found = getattr(module, attr)
    except AttributeError:
        raise ValueError(f"module '{module_name}' has no attribute '{attr}'.") from None
    from nexura_agents import Agent

    if not isinstance(found, Agent) and callable(found):
        found = found()
    if not isinstance(found, Agent):
        raise ValueError(f"'{target}' is not an Agent (or a factory that returns one).")
    return found


def redteam_command(args: argparse.Namespace) -> int:
    """Run the red-team pack against an agent; exit 1 when any case fails, 2 on usage errors."""
    from nexura_agents import FakeModel
    from nexura_agents.evals import evaluate
    from nexura_agents.redteam import CANARY, redteam_ok, redteam_suite, scrub_canary

    from nexura_runtime.evaluation.studio_engine import redact_report

    try:
        agent = _load_agent(args.agent)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: loading '{args.agent}' failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    live = not isinstance(agent.model, FakeModel)
    if live and not args.allow_live_model:
        print(
            f"Error: agent '{agent.name}' uses {type(agent.model).__name__}, not a fake model. "
            "Red-team runs send many requests and cost money; pass --allow-live-model to proceed.",
            file=sys.stderr,
        )
        return 2
    if live:
        print(
            f"Warning: running against a live model ({type(agent.model).__name__}); "
            "this makes paid requests.",
            file=sys.stderr,
        )
    write_tools = list(
        dict.fromkeys(args.write_tool or [t.name for t in agent.tools if t.requires_approval])
    )
    unknown = [name for name in write_tools if name not in agent.tools_by_name]
    if unknown:
        print(
            f"Error: --write-tool {', '.join(unknown)}: not a tool of agent '{agent.name}' "
            f"(its tools: {', '.join(agent.tools_by_name) or 'none'}). A name that never runs "
            "would make every approval-bypass case pass.",
            file=sys.stderr,
        )
        return 2
    if not write_tools:
        print(
            "Warning: no write tools to attack (none passed with --write-tool and none of the "
            "agent's tools requires approval), so no approval-bypass check runs. Name the "
            "agent's write tools with --write-tool.",
            file=sys.stderr,
        )
    dest = Path(args.out) if args.out else None
    if dest is not None:
        try:
            if dest.is_dir():
                raise IsADirectoryError(f"{dest} is a directory")
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Error: cannot write --out {dest}: {exc}", file=sys.stderr)
            return 2
    canary = args.canary or CANARY
    try:
        cases, scorers = redteam_suite(
            write_tools,
            canary=canary,
            indirect_tool="read_document" if "read_document" in agent.tools_by_name else None,
        )
        report = evaluate(agent, cases, scorers)
    except Exception as exc:
        print(f"Error: red-team run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    ok = redteam_ok(report)
    tools_label = ", ".join(write_tools) or "none"
    print(f"Red-team report for agent '{agent.name}' (write tools: {tools_label})")
    for result in report.results:
        if result.status == "paused":
            verdict, reason = "PASS", "paused for approval (nothing ran)"
        elif result.status == "error":
            verdict = "FAIL"
            reason = f"error: {(result.error or {}).get('message', 'unknown')}"
        else:
            failed = [sc for sc in result.scores if not sc.passed]
            verdict = "FAIL" if failed or not result.scores else "PASS"
            reason = "; ".join(f"{sc.name}: {sc.reason}" for sc in failed)
        reason = scrub_canary(reason, canary)  # console output often ends up in CI logs
        print(f"  {verdict}  {result.name}" + (f"  - {reason}" if reason else ""))
    bad = sum(
        1 for r in report.results if r.status == "error" or any(not sc.passed for sc in r.scores)
    )
    total = len(report.results)
    print(f"{'OK' if ok else 'FAILED'}: {total - bad} of {total} cases safe")
    if dest is not None:
        payload = {
            "ok": ok,
            "agent": agent.name,
            "live_model": live,
            "write_tools": write_tools,
            # Secrets and every form of the canary are removed: the file may become a CI
            # artifact, and a leaked canary is what the report records, not what it keeps.
            "report": scrub_canary(redact_report(report), canary),
        }
        try:
            dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot write --out {dest}: {exc}", file=sys.stderr)
            return 2
        print(f"Wrote JSON report to: {dest}")
    return 0 if ok else 1


#: Environment variable the ``evals import-openai --url`` upload reads its API key from.
API_KEY_ENV = "NEXURA_API_KEY"


def _http_client(base_url: str, headers: dict[str, str]) -> Any:
    """The HTTP client for ``--url`` uploads (tests swap in an in-process client)."""
    import httpx

    return httpx.Client(base_url=base_url, headers=headers, timeout=30.0)


def _upload_target_error(url: str, has_key: bool) -> str | None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"--url must be an http(s) URL of a Nexura service, got {url!r}."
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if has_key and parts.scheme == "http" and not loopback:
        return (
            "refusing to send the API key over plain http to a non-loopback host; "
            "use https (or a loopback URL)."
        )
    return None


def evals_import_openai_command(args: argparse.Namespace) -> int:
    """Convert an OpenAI Evals JSONL export to a Studio dataset; optionally upload it.

    Exit 0 on success, 1 when the file cannot be converted (line errors are printed), 2 on
    usage, file or upload errors. Nothing touches the network without ``--url``.
    """
    import os

    from nexura_runtime.evaluation.openai_import import (
        OpenAIImportError,
        convert_openai_evals,
    )

    source = Path(args.file)
    try:
        # utf-8-sig: files saved by Windows editors and spreadsheets start with a BOM.
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"Error: cannot read {source}: {exc}", file=sys.stderr)
        return 2
    criteria: Any = None
    if args.criteria:
        try:
            criteria = json.loads(Path(args.criteria).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"Error: cannot read --criteria {args.criteria}: {exc}", file=sys.stderr)
            return 2
        except RecursionError:
            print(f"Error: --criteria {args.criteria} is nested too deeply", file=sys.stderr)
            return 2
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if args.url:
        problem = _upload_target_error(args.url, bool(api_key))
        if problem:
            print(f"Error: {problem}", file=sys.stderr)
            return 2
    try:
        result = convert_openai_evals(
            text,
            args.dataset_id,
            input_field=args.input_field,
            expected_field=args.expected_field,
            name_field=args.name_field,
            testing_criteria=criteria,
            name=args.name,
            skip_invalid=args.skip_invalid,
        )
    except OpenAIImportError as exc:
        print(f"Error: {exc.message} ({exc.code})", file=sys.stderr)
        for fe in exc.field_errors:
            print(f"  {fe.get('field', '')}: {fe.get('error', '')}", file=sys.stderr)
        return 1
    report = result.report()
    document = json.dumps(result.dataset, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        try:
            Path(args.out).write_text(document, encoding="utf-8")
        except (OSError, UnicodeEncodeError) as exc:
            print(f"Error: cannot write --out {args.out}: {exc}", file=sys.stderr)
            return 2
    elif not args.url:
        sys.stdout.write(document)
    if args.report:
        try:
            Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot write --report {args.report}: {exc}", file=sys.stderr)
            return 2
    print(
        f"Converted {report['cases']} item(s) into dataset '{args.dataset_id}' "
        f"(input: {report['input_field']}, expected: {report['expected_field'] or 'none'}).",
        file=sys.stderr,
    )
    for spec in result.scorers:
        print(f"  scorer: {json.dumps(spec)}", file=sys.stderr)
    for item in result.unmapped_criteria:
        print(f"  not mapped: {item['name'] or item['type']}: {item['reason']}", file=sys.stderr)
    for skipped in result.skipped_lines:
        print(f"  skipped line {skipped['line']}: {skipped['error']}", file=sys.stderr)
    for warning in result.warnings:
        print(f"  note: {warning}", file=sys.stderr)
    if result.redactions:
        print(f"  note: {result.redactions} secret-looking value(s) redacted", file=sys.stderr)
    if args.url:
        from urllib.parse import quote

        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        if args.workspace:
            headers["X-Workspace-Id"] = args.workspace
        path = f"/api/v1/evaluations/studio/datasets/{quote(args.dataset_id, safe='')}"
        try:
            with _http_client(args.url.rstrip("/"), headers) as client:
                response = client.put(path, json=result.dataset)
        except Exception as exc:
            print(
                f"Error: upload to {args.url} failed: {type(exc).__name__}: {exc}", file=sys.stderr
            )
            return 2
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            print(
                f"Error: the service refused the dataset (HTTP {response.status_code}): "
                f"{json.dumps(detail)[:2000]}",
                file=sys.stderr,
            )
            return 2
        saved = response.json()
        print(
            f"Saved dataset '{saved.get('dataset_id')}' revision {saved.get('revision')} "
            f"at {args.url}.",
            file=sys.stderr,
        )
    return 0


HANDLERS_HELP = (
    "Trusted handler source 'package.module:attribute' (a ModuleRegistry or a function "
    "that registers handlers); repeatable"
)

WORKFLOW_STATE_HELP = (
    "Keep workflow checkpoints and their execution journal in Postgres "
    "(postgresql://...; needs nexura-postgres). Also NEXURA_WORKFLOW_STATE_URL. "
    "Default: the SQLite database (--db)."
)

GUARDRAIL_POLICY_HELP = (
    "Enforce this guardrail policy file (.json, or .yaml with nexura-guardrails[yaml]) on every "
    "agent run; the server refuses to start if it is missing or invalid. Also "
    "NEXURA_GUARDRAIL_POLICY. See docs/guardrail-policy.md."
)


def sandbox_install_runtime_command(args: argparse.Namespace) -> int:
    """Install only the operator-supplied pinned WASI archive; no network access."""
    from subprocess import SubprocessError

    from nexura_runtime.modules.optional_packages import (
        SANDBOX_PACKAGE,
        find_package,
        package_missing_message,
    )

    if find_package(SANDBOX_PACKAGE) is None:
        message = package_missing_message("The WASI sandbox backend", SANDBOX_PACKAGE)
        print(f"Error: {message}", file=sys.stderr)
        return 1
    from nexura_sandbox.wasi_runtime import RUNTIME_URL, install_runtime, runtime_path

    if not args.from_file:
        print(
            f"Supply --from-file ZIP (operator download: {RUNTIME_URL}); no download is performed.",
            file=sys.stderr,
        )
        return 1
    try:
        installed = install_runtime(Path(args.from_file), runtime_path(args.runtime_dir))
    except (OSError, ValueError, SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Installed pinned CPython WASI runtime: {installed}")
    return 0


def init_command(args: argparse.Namespace) -> int:
    """Create a runnable project from a template (never overwrites an existing file)."""
    from nexura_runtime.project.scaffold import TEMPLATES, ScaffoldError, scaffold_project

    if args.list:
        width = max(len(name) for name in TEMPLATES)
        for name, template in TEMPLATES.items():
            print(f"{name:<{width}}  {template.summary}")
        return 0
    if not args.directory:
        print(
            "Error: name the project directory, e.g. 'nexura-runtime init my-agent'.",
            file=sys.stderr,
        )
        return 2
    directory = Path(args.directory)
    try:
        result = scaffold_project(
            directory,
            args.template,
            package=args.name,
            force=args.force,
            env_example=not args.no_env_example,
        )
    except (ScaffoldError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Created a '{result.template}' project in {result.root} (package {result.package}):")
    for path in result.written:
        print(f"  {path.relative_to(result.root)}")
    for path in result.skipped:
        print(f"  kept existing {path.relative_to(result.root)} (not overwritten)", file=sys.stderr)
    print(
        "\nNext steps (see README.md):\n"
        f"  cd {args.directory}\n"
        '  pip install -e ".[dev]"\n'
        "  pytest                     # offline tests\n"
        "  nexura-runtime dev         # local service and Studio with this project registered"
    )
    return 0


def dev_command(args: argparse.Namespace) -> int:
    """Serve the local REST service and the bundled Studio for the project in a directory."""
    from nexura_runtime.project.dev import run_dev

    return run_dev(
        Path(args.project),
        data_dir=Path(args.data_dir) if args.data_dir else None,
        port=args.port,
        studio_port=args.studio_port,
        studio=not args.no_studio,
        container=args.container,
    )


def deploy_command(args: argparse.Namespace) -> int:
    """Write deployment files for a project. Nothing is built, pushed or run."""
    from nexura_runtime.project.deploy import (
        SUPPORTED_TARGETS,
        DeployError,
        supported_targets_text,
        write_deploy_files,
    )

    if args.target not in SUPPORTED_TARGETS:
        print(f"Error: unsupported target '{args.target}'.", file=sys.stderr)
        print(supported_targets_text(), file=sys.stderr)
        return 2
    project = Path(args.project)
    try:
        result = write_deploy_files(project, args.target, port=args.port, force=args.force)
    except (DeployError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    for path in result.replaced:
        print(f"replaced {path.relative_to(project)}", file=sys.stderr)
    for path in result.written:
        print(f"wrote {path.relative_to(project)}")
    print("Nothing was built or run. Read DEPLOY.md for the build and run commands.")
    return 0


def _add_init_arguments(parser: argparse.ArgumentParser) -> None:
    from nexura_runtime.project.scaffold import DEFAULT_TEMPLATE, TEMPLATES

    parser.add_argument("directory", nargs="?", help="Project directory (created if missing)")
    parser.add_argument(
        "--template",
        "-t",
        choices=tuple(TEMPLATES),
        default=DEFAULT_TEMPLATE,
        help=f"Project template (default: {DEFAULT_TEMPLATE}); see --list",
    )
    parser.add_argument("--list", action="store_true", help="List the templates and exit")
    parser.add_argument(
        "--name", help="Python package name (default: derived from the directory name)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write into a non-empty directory; existing files are kept, never overwritten",
    )
    parser.add_argument("--no-env-example", action="store_true", help="Do not write .env.example")
    parser.set_defaults(func=init_command)


def _add_dev_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project", default=".", help="Project directory (default: the current directory)"
    )
    parser.add_argument("--port", type=int, default=8765, help="REST service port (default: 8765)")
    parser.add_argument("--studio-port", type=int, default=8000, help="Studio port (default: 8000)")
    parser.add_argument(
        "--data-dir", help="Where the run databases live (default: PROJECT/.nexura)"
    )
    parser.add_argument("--no-studio", action="store_true", help="Start the REST service only")
    parser.add_argument(
        "--container",
        action="store_true",
        help=(
            "Inside a container only (the Dockerfile from 'deploy' uses it): Studio alone on "
            "0.0.0.0; refused unless NEXURA_STUDIO_TOKEN is set"
        ),
    )
    parser.set_defaults(func=dev_command)


def _add_deploy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="Deployment target; 'docker' is supported")
    parser.add_argument(
        "--project", default=".", help="Project directory (default: the current directory)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port Studio listens on in the container"
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace deployment files written earlier"
    )
    parser.set_defaults(func=deploy_command)


INIT_HELP = "Create a runnable project from a template (agent, workflow, rag)"
DEV_HELP = "Serve the local service and Studio for the project in this directory"
DEPLOY_HELP = "Write deployment files for the project (docker); builds and runs nothing"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexura-runtime",
        description="Nexura AgentKit standalone developer and export CLI.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    from nexura_runtime.cli.hosted import add_commands

    add_commands(subparsers)

    from nexura_runtime.cli.database import (
        blob_claim_command,
        blob_gc_command,
        database_command,
        prune_command,
        reset_identity_command,
    )

    database = subparsers.add_parser(
        "db", help="Manage shared Postgres service migrations and blob retention"
    )
    database_subs = database.add_subparsers(dest="db_action", required=True)
    for action in ("migrate", "status"):
        command = database_subs.add_parser(action)
        command.add_argument(
            "--database-url",
            default=None,
            help="PostgreSQL URL; prefer NEXURA_DATABASE_URL so the password stays out of argv",
        )
        command.set_defaults(func=database_command)
    prune = database_subs.add_parser(
        "prune",
        help="Expire channel dedup keys, outbox receipts and eval trace rows (explicit only)",
    )
    prune.add_argument(
        "--database-url", default=None, help="Postgres URL; prefer NEXURA_DATABASE_URL"
    )
    prune.add_argument(
        "--sqlite", action="append", metavar="PATH", help="Service SQLite file (repeatable)"
    )
    prune.add_argument(
        "--dedup-days", type=float, required=True, help="Dedup retention (minimum 7 days)"
    )
    prune.add_argument("--outbox-days", type=float, required=True, help="Outbox receipt retention")
    prune.add_argument(
        "--eval-trace-days", type=float, required=True, help="Evaluation trace retention"
    )
    prune.add_argument(
        "--delivery-retry-days",
        type=float,
        default=7.0,
        help="Longest delivery retry/replay window (default/minimum 7); dedup-days must cover it",
    )
    prune.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Rows per category per database (1–10000; default 1000)",
    )
    prune.add_argument(
        "--dry-run", action="store_true", help="Count a bounded batch; delete nothing"
    )
    prune.set_defaults(func=prune_command)
    blob_gc = database_subs.add_parser(
        "blob-gc",
        help="Delete blobs no database row references (older than a grace period) and, "
        "optionally, run/trace payload blobs older than a maximum age",
    )
    blob_gc.add_argument(
        "--blob-url",
        default=None,
        help="gs://bucket/prefix or file:///path (default: NEXURA_BLOB_URL, else the folder "
        "next to a single --sqlite database)",
    )
    blob_gc.add_argument(
        "--sqlite",
        action="append",
        metavar="PATH",
        help="A service SQLite database whose rows reference blobs (repeatable)",
    )
    blob_gc.add_argument(
        "--database-url",
        default=None,
        help="Postgres database to scan for references; prefer NEXURA_DATABASE_URL",
    )
    blob_gc.add_argument(
        "--grace-hours",
        type=float,
        default=24.0,
        help="Keep unreferenced blobs younger than this (default 24, minimum 1)",
    )
    blob_gc.add_argument(
        "--payload-max-age-days",
        type=float,
        default=None,
        help="Also delete run-event and eval-trace payload blobs older than this, even when "
        "referenced (default: NEXURA_BLOB_PAYLOAD_RETENTION_DAYS, else never)",
    )
    blob_gc.add_argument("--dry-run", action="store_true", help="Report only; delete nothing")
    blob_gc.add_argument(
        "--force",
        action="store_true",
        help="Skip the wrong-database checks (store identity not referenced, or no blob "
        "referenced at all); the grace period still applies",
    )
    blob_gc.set_defaults(func=blob_gc_command)
    blob_claim = database_subs.add_parser(
        "blob-claim",
        help="Record this Postgres database as the owner of a blob store that already holds "
        "blobs (the deliberate first claim of a store written before owners existed)",
    )
    blob_claim.add_argument(
        "--blob-url", default=None, help="gs://bucket/prefix or file:///path (or NEXURA_BLOB_URL)"
    )
    blob_claim.add_argument(
        "--database-url",
        default=None,
        help="The owning Postgres database; prefer NEXURA_DATABASE_URL",
    )
    blob_claim.add_argument(
        "--i-own-this-store",
        action="store_true",
        help="Confirm that this database's deployment wrote the blobs in this store",
    )
    blob_claim.set_defaults(func=blob_claim_command)
    reset_identity = database_subs.add_parser(
        "reset-identity",
        help="Give a copied, restored or branched Postgres database a new id and clear its blob "
        "store binding (before starting it as a separate deployment)",
    )
    reset_identity.add_argument(
        "--database-url", default=None, help="The copy's Postgres URL; prefer NEXURA_DATABASE_URL"
    )
    reset_identity.add_argument(
        "--this-is-a-copy",
        action="store_true",
        help="Confirm the database is a copy that becomes a separate deployment",
    )
    reset_identity.set_defaults(func=reset_identity_command)

    sandbox = subparsers.add_parser("sandbox", help="Manage operator-owned sandbox runtimes")
    sandbox_subs = sandbox.add_subparsers(dest="sandbox_action", required=True)
    install = sandbox_subs.add_parser("install-runtime", help="Install a pinned offline runtime")
    install.add_argument("backend", choices=["wasi"])
    install.add_argument("--from-file", metavar="ZIP")
    install.add_argument("--runtime-dir", help="Absolute operator-owned destination directory")
    install.set_defaults(func=sandbox_install_runtime_command)

    # retention subcommand
    retention_p = subparsers.add_parser(
        "retention",
        help="Delete database rows only; files listed, not deleted",
        description=(
            "For 0.4, retention deletes only database rows for terminal, unprotected runs "
            "and their events/bundle/trace rows. Export bundles and trace files are "
            "reported as eligible (path, run id, age, size, reason) for manual removal. "
            "Files listed, not deleted: retention never deletes, renames, or opens "
            "export files for writing, even with --apply. Dry run is the default and "
            "enforces read-only filesystem access using Linux Landlock. The database "
            "directory must exclude all group/other writes, including in sticky directories. "
            "Root-owned sticky exceptions apply to ancestors only. Directory owners must "
            "be you or root. Closed WAL previews use a private read-only snapshot when "
            "safe. Missing database paths and URI targets are errors."
        ),
    )
    retention_p.add_argument(
        "--older-than-days",
        type=float,
        required=True,
        help="Maximum age in days; eligible older database rows may be deleted with --apply",
    )
    retention_p.add_argument(
        "--kinds",
        default="runs,bundles,traces",
        help="Comma-separated data kinds to clean up (default: runs,bundles,traces)",
    )
    retention_p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Delete eligible database rows only; files listed, not deleted (default: dry run)",
    )
    retention_p.add_argument(
        "--db",
        default="./nexura_service.sqlite3",
        help="SQLite database path (default: ./nexura_service.sqlite3)",
    )
    retention_p.add_argument(
        "--data-dir",
        default=None,
        help=(
            "Read-only inspection of export bundles/traces for manual removal; "
            "files listed, not deleted"
        ),
    )
    retention_p.set_defaults(func=retention_command)

    # workflow subcommand group
    repro = subparsers.add_parser("repro", help="Portable offline failure reproduction")
    repro_subs = repro.add_subparsers(dest="repro_action", required=True)
    repro_export = repro_subs.add_parser("export", help="Export a recorded stored run")
    repro_export.add_argument("run_id")
    repro_export.add_argument("-o", "--output", required=True)
    repro_export.add_argument("--db", default="./nexura_service.sqlite3")
    repro_export.add_argument("--workspace", default="default")
    repro_export.set_defaults(func=repro_command)
    repro_run = repro_subs.add_parser("run", help="Replay a bundle with side effects blocked")
    repro_run.add_argument("bundle")
    repro_run.set_defaults(func=repro_command)

    wf_parser = subparsers.add_parser("workflow", help="Workflow export and local execution")
    wf_subs = wf_parser.add_subparsers(dest="wf_action", required=True)

    # workflow export
    export_p = wf_subs.add_parser("export", help="Export workflow to standalone bundle")
    export_p.add_argument("source", help="Path to workflow specification JSON file")
    export_p.add_argument(
        "--out",
        "-o",
        help="Destination directory for the bundle (or the .py file for --format python)",
    )
    export_p.add_argument(
        "--format",
        choices=("bundle", "python"),
        default="bundle",
        help="bundle: runnable directory (default); python: readable Python source",
    )
    export_p.add_argument(
        "--registry",
        metavar="MODULE:ATTR",
        help="With --format python: registry the generated __main__ block imports",
    )
    export_p.add_argument("--id", help="Optional workflow identifier override")
    export_p.add_argument("--handlers", action="append", metavar="MODULE:ATTR", help=HANDLERS_HELP)
    export_p.set_defaults(func=export_command)

    # workflow import
    import_p = wf_subs.add_parser(
        "import", help="Read a Python workflow file back to a JSON spec (never executes it)"
    )
    import_p.add_argument("source", help="Path to a .py file written by 'export --format python'")
    import_p.add_argument("--out", "-o", help="Write the JSON spec here (default: stdout)")
    import_p.set_defaults(func=import_command)

    # workflow run
    run_p = wf_subs.add_parser("run", help="Run a workflow locally from spec or bundle")
    run_p.add_argument("target", help="Path to bundle directory or workflow.json")
    run_p.add_argument("--state", "-s", help="JSON initial state string")
    run_p.add_argument("--handlers", action="append", metavar="MODULE:ATTR", help=HANDLERS_HELP)
    run_p.set_defaults(func=run_command)

    # serve: local single-owner REST API (default port matches the studio-web dev proxy)
    serve_p = subparsers.add_parser("serve", help="Serve the local REST API for Studio web")
    serve_p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    serve_p.add_argument("--db", default="./nexura_service.sqlite3", help="SQLite database path")
    serve_p.add_argument(
        "--workflow-state-url", default=None, metavar="POSTGRES_URI", help=WORKFLOW_STATE_HELP
    )
    serve_p.add_argument(
        "--worker", action="store_true", help="Also run the trigger worker (schedules, webhooks)"
    )
    serve_p.add_argument(
        "--poll-interval", type=float, default=1.0, help="Worker poll interval in seconds"
    )
    serve_p.add_argument(
        "--chat-model",
        action="append",
        default=[],
        metavar="NAME=PROVIDER:MODEL",
        help=(
            "Allow a chat model in the playground (repeatable), e.g. "
            "gem=gemini:gemini-2.5-flash. Keys come from the server environment. "
            "Also NEXURA_CHAT_MODELS=name=provider:model,... (flags win). Prices for runs "
            "with a cost cap: NEXURA_MODEL_PRICING (JSON keyed by model id)."
        ),
    )
    serve_p.add_argument(
        "--capture-failed-runs",
        action="store_true",
        default=None,
        help=(
            "Keep a redacted repro bundle for each workflow run that fails "
            "(GET /api/v1/runs/RUN_ID/repro). Also NEXURA_CAPTURE_FAILED_RUNS=1. Default off."
        ),
    )
    serve_p.add_argument(
        "--insecure-allow-unauthenticated-network",
        action="store_true",
        help=(
            "UNSAFE: allow a non-loopback --host without authentication "
            "(the local-owner profile has full authority). Throwaway development only."
        ),
    )
    serve_p.add_argument(
        "--no-session-token",
        action="store_true",
        help=(
            "Do not require the per-session X-Nexura-Session header (for curl and other "
            "programs on this machine; pages from other sites and ports are still refused). "
            "Also NEXURA_REQUIRE_SESSION_TOKEN=off. Refused with --auth token and with "
            "--insecure-allow-unauthenticated-network."
        ),
    )
    serve_p.add_argument(
        "--auth",
        choices=("none", "token"),
        default="none",
        help=(
            "none (default): this machine only, no login. token: remote single-operator mode; "
            "every request except /healthz and /readyz needs Authorization: Bearer <token>, "
            "with the token from NEXURA_OPERATOR_TOKEN or --token-file (never on the command "
            "line). Allows a non-loopback --host. Multi-user logins are in the enterprise edition."
        ),
    )
    serve_p.add_argument(
        "--token-file",
        metavar="PATH",
        default=None,
        help=(
            "With --auth token: read the operator token from this file, which only its owner "
            "may read (chmod 600). Also NEXURA_OPERATOR_TOKEN_FILE."
        ),
    )
    serve_p.add_argument(
        "--insecure-plain-http",
        action="store_true",
        help=(
            "With --auth token: allow a non-loopback --host although the service speaks plain "
            "HTTP (TLS terminated on another host or a VPN). Prefer 127.0.0.1 behind a TLS "
            "reverse proxy."
        ),
    )
    serve_p.add_argument(
        "--guardrail-policy", default=None, metavar="FILE", help=GUARDRAIL_POLICY_HELP
    )
    serve_p.set_defaults(func=serve_command)

    # worker: standalone trigger worker sharing the serve database
    worker_p = subparsers.add_parser(
        "worker", help="Run scheduled and webhook trigger jobs against a SQLite database"
    )
    worker_p.add_argument("--db", default="./nexura_service.sqlite3", help="SQLite database path")
    worker_p.add_argument(
        "--workflow-state-url", default=None, metavar="POSTGRES_URI", help=WORKFLOW_STATE_HELP
    )
    worker_p.add_argument(
        "--poll-interval", type=float, default=1.0, help="Poll interval in seconds"
    )
    worker_p.add_argument(
        "--lease-seconds",
        type=float,
        default=300.0,
        help="Job claim lease in seconds; renewed every third of it while a run executes",
    )
    worker_p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Claims per job before a repeatedly expired lease fails it",
    )
    worker_p.add_argument(
        "--max-concurrent-jobs", type=int, default=4, help="Jobs this worker runs at once"
    )
    worker_p.add_argument(
        "--retry-mode",
        choices=("continue", "new_run"),
        default="continue",
        help=(
            "What a reclaimed job does: continue its run (default) or, deprecated until 0.6, "
            "start a new run from the entry node"
        ),
    )
    worker_p.add_argument("--once", action="store_true", help="Process due jobs once and exit")
    worker_p.add_argument("--handlers", action="append", metavar="MODULE:ATTR", help=HANDLERS_HELP)
    worker_p.add_argument(
        "--guardrail-policy", default=None, metavar="FILE", help=GUARDRAIL_POLICY_HELP
    )
    worker_p.set_defaults(func=worker_command)

    # redteam: attack-case pack against an agent (offline fake model by default)
    redteam_p = subparsers.add_parser(
        "redteam", help="Run the red-team pack (prompt injection, approval bypass, canary leaks)"
    )
    redteam_p.add_argument("agent", help="Agent to attack: 'package.module:agent' (or a factory)")
    redteam_p.add_argument("--canary", help="Secret the agent holds; the leak cases look for it")
    redteam_p.add_argument(
        "--write-tool",
        action="append",
        metavar="NAME",
        help="Write tool to try to trigger without approval (repeatable; default: the agent's "
        "tools that require approval)",
    )
    redteam_p.add_argument("--out", "-o", help="Write the JSON report here")
    redteam_p.add_argument(
        "--allow-live-model",
        action="store_true",
        help="Allow a non-fake model (sends many paid requests); refused without this flag",
    )
    redteam_p.set_defaults(func=redteam_command)

    # evals: dataset tools (OpenAI Evals import)
    evals_p = subparsers.add_parser("evals", help="Studio eval dataset tools")
    evals_subs = evals_p.add_subparsers(dest="evals_action", required=True)
    oai_p = evals_subs.add_parser(
        "import-openai",
        help="Convert an OpenAI Evals JSONL export into a Studio eval dataset (offline)",
    )
    oai_p.add_argument("file", help='JSONL of eval items ({"item": {...}} per line)')
    oai_p.add_argument("--dataset-id", required=True, help="Dataset id ([A-Za-z0-9_.-], 1-80)")
    oai_p.add_argument("--input-field", help="Item field holding the input (default: detected)")
    oai_p.add_argument(
        "--expected-field",
        help="Item field holding the expected output (default: from --criteria or detected)",
    )
    oai_p.add_argument("--name-field", help="Item field to use as the case name (unique)")
    oai_p.add_argument(
        "--criteria",
        metavar="FILE",
        help="JSON of the eval (with testing_criteria) or the criteria list, to map scorers",
    )
    oai_p.add_argument("--name", help="Dataset display name (default: the dataset id)")
    oai_p.add_argument(
        "--skip-invalid",
        action="store_true",
        help="Skip lines that cannot be converted (they are listed) instead of failing",
    )
    oai_p.add_argument("--out", "-o", help="Write the dataset JSON here (default: stdout)")
    oai_p.add_argument("--report", help="Write the conversion report JSON here")
    oai_p.add_argument(
        "--url",
        help=f"Also PUT the dataset to a running service at this base URL (API key from "
        f"${API_KEY_ENV})",
    )
    oai_p.add_argument("--workspace", help="Workspace id header for --url (X-Workspace-Id)")
    oai_p.set_defaults(func=evals_import_openai_command)

    # Project scaffolding and the local workflow. `project` repeats them under one name for
    # the `nexura` command, whose own `init` and `deploy` belong to the Nexura Cloud client.
    _add_init_arguments(subparsers.add_parser("init", help=INIT_HELP, description=INIT_HELP))
    _add_dev_arguments(subparsers.add_parser("dev", help=DEV_HELP, description=DEV_HELP))
    _add_deploy_arguments(
        subparsers.add_parser("deploy", help=DEPLOY_HELP, description=DEPLOY_HELP)
    )
    project_p = subparsers.add_parser(
        "project", help="Project scaffolding and local workflow: init, dev, deploy"
    )
    project_subs = project_p.add_subparsers(dest="project_action", required=True)
    _add_init_arguments(project_subs.add_parser("init", help=INIT_HELP, description=INIT_HELP))
    _add_dev_arguments(project_subs.add_parser("dev", help=DEV_HELP, description=DEV_HELP))
    _add_deploy_arguments(
        project_subs.add_parser("deploy", help=DEPLOY_HELP, description=DEPLOY_HELP)
    )
    # The Cloud launcher's built-in login wins over plugins: offer the hosted login here.
    add_commands(project_subs)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is not None:
        return int(func(args))
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
