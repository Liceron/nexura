"""Local release gate: execute every required check and record raw receipts.

Usage (from the repository root):

    uv run --frozen python experiments/release-gate/run_gate.py [--skip-browser]

Writes ``oss-completion-blueprint/reports/evidence/receipts.json`` and a
``READINESS.md`` matrix generated only from those receipts. That output directory is
created on demand, including in a public export; no pre-existing planning file is read
or required. It is generated local evidence, not distributed documentation. Every check runs a
real command; nothing is copied from earlier reports. No network beyond
loopback, no credentials, no publishing.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexura_runtime.release.evidence import (
    Invariant,
    Receipt,
    environment_profile,
    summarize,
    write_receipts,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "oss-completion-blueprint" / "reports" / "evidence"
INFRA_FILE = ROOT / ".agy" / "review-infrastructure.json"


def sh(
    cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None, timeout: int = 1800
) -> tuple[subprocess.CompletedProcess[str], float]:
    start = time.monotonic()
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc, time.monotonic() - start


def pytest_counts(output: str) -> dict[str, int]:
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    return {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|skipped|errors?)", tail)}


def command_receipt(
    check_id: str,
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    extra: Callable[[str], tuple[list[Invariant], dict[str, Any]]] | None = None,
    timeout: int = 1800,
) -> Receipt:
    proc, duration = sh(cmd, cwd=cwd, env=env, timeout=timeout)
    output = proc.stdout + proc.stderr
    invariants = [Invariant("exit code zero", proc.returncode == 0, f"exit {proc.returncode}")]
    measurements: dict[str, Any] = {}
    if extra is not None:
        more, measurements = extra(output)
        invariants += more
    passed = all(i.held for i in invariants)
    return Receipt(
        check_id=check_id,
        status="passed" if passed else "failed",
        command=" ".join(cmd),
        exit_code=proc.returncode,
        duration_s=duration,
        invariants=tuple(invariants),
        measurements=measurements,
        reason="" if passed else _failure_reason(output),
    )


def _failure_reason(output: str) -> str:
    """The tail of the output, led by pytest's FAILED/ERROR lines so the failing test is named."""
    failed = [
        line.strip() for line in output.splitlines() if line.startswith(("FAILED ", "ERROR "))
    ][:20]
    tail = output[-600:].replace("\n", " ")
    return " | ".join(failed) + (" || " if failed else "") + tail


def pytest_extra(output: str) -> tuple[list[Invariant], dict[str, Any]]:
    counts = pytest_counts(output)
    return (
        [
            Invariant("some tests ran", counts.get("passed", 0) > 0),
            Invariant("no failures", counts.get("failed", 0) == 0 and "error" not in counts),
        ],
        counts,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


@contextmanager
def test_services() -> Iterator[None]:
    """Start the local Postgres/Qdrant test containers, restoring their prior state after."""
    names = os.environ.get("NEXURA_GATE_CONTAINERS", "nexura-test-pg nexura-test-qd").split()
    started: list[str] = []
    if shutil.which("docker"):
        for name in names:
            state, _ = sh(["docker", "inspect", "-f", "{{.State.Running}}", name])
            if state.returncode == 0 and state.stdout.strip() == "false":
                proc, _ = sh(["docker", "start", name])
                if proc.returncode == 0:
                    started.append(name)
        if started:
            time.sleep(5)  # let the databases accept connections
    try:
        yield
    finally:
        for name in started:
            sh(["docker", "stop", name])


def check_python_tests() -> Receipt:
    env = dict(os.environ)
    if INFRA_FILE.exists():
        infra = json.loads(INFRA_FILE.read_text())
        env["NEXURA_TEST_POSTGRES_URI"] = infra.get("postgres_uri", "")
        env["NEXURA_TEST_QDRANT_URL"] = infra.get("qdrant_url", "")
        if Path(infra.get("runsc_path", "")).exists():
            env["NEXURA_REAL_GVISOR"] = "1"

    def extra(output: str) -> tuple[list[Invariant], dict[str, Any]]:
        invariants, counts = pytest_extra(output)
        # A skipped profile is unverified, so it cannot count toward a pass.
        invariants.append(Invariant("no skipped tests", counts.get("skipped", 0) == 0))
        return invariants, counts

    with test_services():
        return command_receipt(
            "python-tests",
            [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "-q",
                "-rfEs",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                # Parallel like CI (one file per worker); the serial suite outgrew 30 minutes.
                "-n",
                "4",
                "--dist",
                "loadfile",
            ],
            env=env,
            extra=extra,
            timeout=3600,
        )


def check_ruff() -> Receipt:
    return command_receipt(
        "python-lint",
        [
            "uv",
            "run",
            "--frozen",
            "ruff",
            "check",
            "--no-cache",
            "src",
            "packages",
            "tests",
            "examples",
        ],
    )


#: Source roots of the workspace packages under packages/ (P28 step 10 stages 4a and 4b).
PACKAGE_SOURCES = tuple(
    f"packages/{name}/src"
    for name in (
        "nexura-agents",
        "nexura-connectors",
        "nexura-guardrails",
        "nexura-postgres",
        "nexura-qdrant",
        "nexura-sandbox",
        "nexura-studio",
        "nexura-vertex",
    )
)


def check_mypy() -> Receipt:
    return command_receipt(
        "python-types",
        [
            "uv",
            "run",
            "--frozen",
            "mypy",
            "src",
            *PACKAGE_SOURCES,
        ],
        extra=lambda out: ([Invariant("mypy success", "Success: no issues found" in out)], {}),
    )


def check_examples() -> Receipt:
    examples = sorted((ROOT / "examples" / "oss").glob("o0*.py")) + [
        ROOT / "examples" / name
        for name in ("approval_workflow.py", "persistent_chat.py", "order_support.py")
    ]
    invariants: list[Invariant] = []
    start = time.monotonic()
    for path in examples:
        proc, _ = sh(["uv", "run", "--frozen", "python", str(path)], timeout=600)
        invariants.append(Invariant(f"{path.name} exits 0", proc.returncode == 0))
    ok = all(i.held for i in invariants)
    return Receipt(
        "keyless-examples",
        "passed" if ok else "failed",
        "uv run python <each example>",
        0 if ok else 1,
        time.monotonic() - start,
        tuple(invariants),
        {"examples": len(examples)},
    )


def check_reference_scenario() -> Receipt:
    proc, duration = sh(["uv", "run", "--frozen", "python", "examples/oss/o09.py"])
    data: dict[str, Any] = {}
    if proc.returncode == 0:
        data = json.loads(proc.stdout[proc.stdout.index("{") :])
    ref = data.get("reference", {})
    tiny = data.get("budget_exhaustion", {})
    invariants = [
        Invariant("exit code zero", proc.returncode == 0),
        Invariant("pauses before refund", ref.get("paused_at") == "refund"),
        Invariant("completes after approval", ref.get("final_status") == "completed"),
        Invariant("refund executed exactly once", ref.get("refunds_executed") == 1),
        Invariant("duplicate resume rejected", ref.get("duplicate_resume_rejected") is True),
        Invariant("one provider dispatch", ref.get("provider_dispatches") == 1),
        Invariant(
            "spend survives restart",
            ref.get("spent_after_restart") == ref.get("spent_before_restart") is not None,
        ),
        Invariant(
            "deleted and foreign evidence omitted",
            set(ref.get("evidence_omitted", {})) == {"policy-retired", "policy-other-team"},
        ),
        Invariant("restored backup resumes", ref.get("restored_resume_status") == "completed"),
        Invariant(
            "restored spend preserved",
            ref.get("restored_spent_usd") == ref.get("spent_before_restart"),
        ),
        Invariant("export hash matches", ref.get("export_spec_hash_matches") is True),
        Invariant("exhausted budget dispatches nothing", tiny.get("provider_dispatches") == 0),
        Invariant("exhausted budget executes no refund", tiny.get("refunds_executed") == 0),
    ]
    ok = all(i.held for i in invariants)
    return Receipt(
        "reference-scenario",
        "passed" if ok else "failed",
        "uv run python examples/oss/o09.py",
        proc.returncode,
        duration,
        tuple(invariants),
        {
            "total_spent_usd": ref.get("total_spent_usd"),
            "cost_per_success_usd": ref.get("cost_per_success"),
            "unknown_usage": ref.get("unknown_usage_count"),
        },
    )


def check_paired_selection() -> Receipt:
    """Baseline vs deterministic selection on paired inputs (integration only)."""
    from nexura_agents.context import ContextItem, ContextPolicy, ContextSelector

    start = time.monotonic()
    baseline_tokens = selected_tokens = 0
    evidence_kept = True
    pairs = 20
    for n in range(pairs):
        log = "\n".join(f"2026-10-07T10:{i:02d} worker heartbeat ok seq={i}" for i in range(40 + n))
        items = [
            ContextItem(
                item_id="sys",
                item_type="system_prompt",
                content="Never refund twice.",
                role="system",
            ),
            ContextItem(
                item_id="order",
                item_type="structured_json",
                content={"order_id": f"ORD-{n}", "amount": 1000 + n},
                is_required_evidence=True,
                workspace_id="ws",
            ),
            ContextItem(item_id="log", item_type="log", content=log, workspace_id="ws"),
        ]
        base = ContextSelector(ContextPolicy(enabled=False)).select(
            items, current_workspace_id="ws"
        )
        sel = ContextSelector(
            ContextPolicy(
                enabled=True,
                max_context_tokens=400,
                short_context_threshold_tokens=0,
                overflow_strategy="truncate_unprotected",
            )
        ).select(items, current_workspace_id="ws")
        baseline_tokens += base.decision_record.initial_tokens or base.total_tokens
        selected_tokens += sel.decision_record.projected_tokens
        kept = {i.item_id for i in sel.decision_record.items if i.decision != "omitted"}
        evidence_kept &= {"sys", "order"} <= kept
    invariants = (
        Invariant("required evidence kept on every pair", evidence_kept),
        Invariant("selection never increases tokens", selected_tokens <= baseline_tokens),
    )
    ok = all(i.held for i in invariants)
    return Receipt(
        "paired-context-selection",
        "passed" if ok else "failed",
        "in-process ContextSelector over 20 paired synthetic inputs",
        0 if ok else 1,
        time.monotonic() - start,
        invariants,
        {
            "pairs": pairs,
            "baseline_tokens": baseline_tokens,
            "selected_tokens": selected_tokens,
            "note": "estimated tokens on fake fixtures; not quality or billing evidence",
        },
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def check_external_consumers(work: Path) -> list[Receipt]:
    receipts: list[Receipt] = []
    start = time.monotonic()
    dist = work / "dist"
    proc, _ = sh(["uv", "build", "--all-packages", "--out-dir", str(dist)])
    wheel = next(dist.glob("nexura_runtime-*.whl"), None)
    venv = work / "venv"
    py = venv / "bin" / "python"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    inv: list[Invariant] = [Invariant("wheel built", proc.returncode == 0 and wheel is not None)]
    meas: dict[str, Any] = {}
    if wheel is not None:
        sh(["uv", "venv", "--python", "3.12", str(venv)], cwd=work)
        find_links = ["--find-links", str(dist)]  # nexura-agents is a local sibling wheel
        sh(["uv", "pip", "install", "--python", str(py), *find_links, str(wheel)], cwd=work)
        probe = (
            "import sys, json, nexura_runtime as m; print(json.dumps({'exe': sys.executable, "
            "'file': m.__file__, 'fastapi': 'fastapi' in sys.modules}))"
        )
        core, _ = sh([str(py), "-I", "-c", probe], cwd=work, env=env)
        info = json.loads(core.stdout) if core.returncode == 0 else {}
        inv += [
            Invariant("core imports from site-packages", "site-packages" in info.get("file", "")),
            Invariant("core import does not load fastapi", info.get("fastapi") is False),
        ]
        sh(["uv", "pip", "install", "--python", str(py), *find_links, f"{wheel}[studio]"], cwd=work)
        shutil.copy2(ROOT / "examples" / "oss" / "o07.py", work / "o07.py")
        ex, _ = sh([str(py), "-I", str(work / "o07.py")], cwd=work, env=env)
        inv.append(Invariant("o07 example runs from installed wheel", ex.returncode == 0))
        meas = {"wheel": wheel.name, "python": info.get("exe"), "import": info.get("file")}
    ok = all(i.held for i in inv)
    receipts.append(
        Receipt(
            "python-external-consumer",
            "passed" if ok else "failed",
            "uv build; fresh venv; install wheel; import; run o07.py",
            0 if ok else 1,
            time.monotonic() - start,
            tuple(inv),
            meas,
        )
    )

    # Service from the installed wheel: readiness time, RSS and concurrent load.
    receipts.append(_service_receipt(py, env, work))

    # Packed TypeScript client in a fresh npm project talking to that service.
    receipts.append(_ts_consumer_receipt(py, env, work))
    return receipts


def _start_service(
    py: Path, env: dict[str, str], work: Path
) -> tuple[subprocess.Popen[str], int, float]:
    port = _free_port()
    start = time.monotonic()
    proc = subprocess.Popen(
        [
            str(py),
            "-I",
            "-m",
            "nexura_runtime.cli.main",
            "serve",
            "--port",
            str(port),
            "--db",
            str(work / f"svc-{port}.sqlite3"),
        ],
        cwd=work,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready_s = -1.0
    for _ in range(300):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as r:
                if r.status == 200:
                    ready_s = time.monotonic() - start
                    break
        except OSError:
            time.sleep(0.05)
    return proc, port, ready_s


def _service_receipt(py: Path, env: dict[str, str], work: Path) -> Receipt:
    proc, port, ready_s = _start_service(py, env, work)
    base = f"http://127.0.0.1:{port}/api/v1"
    try:
        status = Path(f"/proc/{proc.pid}/status").read_text()
        rss_match = re.search(r"VmRSS:\s+(\d+)", status)
        rss_kb = int(rss_match[1]) if rss_match else 0
        # State-changing requests need the per-session token; local scripts read it here.
        try:
            with urllib.request.urlopen(f"{base}/local-session", timeout=5) as r:
                session_token = str(json.loads(r.read()).get("token", ""))
        except OSError:
            session_token = ""

        def one_run(i: int) -> tuple[bool, float]:
            t0 = time.monotonic()
            spec = json.dumps(
                {"spec": _builtin_spec(), "initial_state": {"order_id": "ORD-MUMBAI-202"}}
            )
            req = urllib.request.Request(
                f"{base}/runs",
                data=spec.encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Workspace-ID": f"load-{i % 4}",
                    "X-Nexura-Session": session_token,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = json.loads(r.read())
                    return r.status == 201 and body["status"] == "completed", time.monotonic() - t0
            except OSError:
                return False, time.monotonic() - t0

        requests = 40
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one_run, range(requests)))
        latencies = sorted(d for _, d in results)
        ok_runs = sum(ok for ok, _ in results)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    inv = (
        Invariant("service became ready", ready_s >= 0),
        Invariant("every concurrent run completed", ok_runs == requests),
    )
    ok = all(i.held for i in inv)
    return Receipt(
        "service-readiness-and-load",
        "passed" if ok else "failed",
        "nexura-runtime serve from installed wheel; 40 runs over 8 threads",
        0 if ok else 1,
        ready_s,
        inv,
        {
            "ready_s": round(ready_s, 2),
            "rss_mb": round(rss_kb / 1024, 1),
            "runs": requests,
            "p50_ms": round(latencies[len(latencies) // 2] * 1000),
            "p95_ms": round(latencies[int(len(latencies) * 0.95) - 1] * 1000),
            "cpus": os.cpu_count(),
        },
    )


def _builtin_spec() -> dict[str, Any]:
    from nexura_runtime.studio.builtin_modules import get_default_workflow_spec

    return get_default_workflow_spec().to_dict()


def _ts_consumer_receipt(py: Path, env: dict[str, str], work: Path) -> Receipt:
    start = time.monotonic()
    client_dir = ROOT / "clients" / "typescript"
    pack, _ = sh(["npm", "pack", "--pack-destination", str(work)], cwd=client_dir)
    tarball = next(work.glob("liceron-nexura-sdk-*.tgz"), None)
    app = work / "ts-consumer"
    app.mkdir()
    (app / "package.json").write_text('{"name":"consumer","private":true,"type":"module"}')
    inv = [Invariant("client packed", pack.returncode == 0 and tarball is not None)]
    meas: dict[str, Any] = {"tarball": tarball.name if tarball else None}
    if tarball is not None:
        npm_i, _ = sh(["npm", "install", "--no-audit", "--no-fund", str(tarball)], cwd=app)
        inv.append(Invariant("installed into fresh project", npm_i.returncode == 0))
        proc, port, _ = _start_service(py, env, work)
        try:
            script = (
                "import { NexuraClient } from '@liceron/nexura-sdk';\n"
                f"const c = new NexuraClient({{ baseUrl: 'http://127.0.0.1:{port}' }});\n"
                "const caps = await c.capabilities.get();\n"
                "console.log(JSON.stringify({ version: caps.sdk_version }));\n"
            )
            (app / "check.mjs").write_text(script)
            run, _ = sh(["node", "check.mjs"], cwd=app)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        version = json.loads(run.stdout).get("version") if run.returncode == 0 else None
        inv.append(Invariant("packed client calls wheel service", version is not None))
        meas["service_version_seen"] = version
    ok = all(i.held for i in inv)
    return Receipt(
        "ts-external-consumer",
        "passed" if ok else "failed",
        "npm pack; npm install tarball in fresh project; call nexura-runtime serve",
        0 if ok else 1,
        time.monotonic() - start,
        tuple(inv),
        meas,
    )


def check_agents_standalone(work: Path) -> Receipt:
    """The nexura-agents wheel alone, in a fresh venv, runs every README example."""
    start = time.monotonic()
    dist = work / "agents-dist"
    proc, _ = sh(["uv", "build", "--package", "nexura-agents", "--out-dir", str(dist)])
    wheel = next(dist.glob("nexura_agents-*.whl"), None)
    inv = [Invariant("agents wheel built", proc.returncode == 0 and wheel is not None)]
    meas: dict[str, Any] = {}
    if wheel is not None:
        venv = work / "agents-venv"
        py = venv / "bin" / "python"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
        sh(["uv", "venv", "--python", "3.12", str(venv)], cwd=work)
        sh(["uv", "pip", "install", "--python", str(py), str(wheel)], cwd=work)
        listing, _ = sh(
            [
                str(py),
                "-c",
                "import importlib.metadata as m; "
                "print(sorted(d.metadata['Name'] for d in m.distributions()))",
            ]
        )
        installed = listing.stdout.strip()
        inv.append(Invariant("platform not installed", "nexura-runtime" not in installed))
        readme = (ROOT / "packages" / "nexura-agents" / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
        for i, block in enumerate(blocks):
            script = work / f"agents_readme_{i}.py"
            script.write_text(block, encoding="utf-8")
            run, _ = sh([str(py), "-I", str(script)], cwd=work, env=env)
            inv.append(Invariant(f"README example {i} runs", run.returncode == 0))
        meas = {"wheel": wheel.name, "installed": installed, "examples": len(blocks)}
    ok = all(i.held for i in inv)
    return Receipt(
        "agents-standalone-consumer",
        "passed" if ok else "failed",
        "uv build --package nexura-agents; fresh venv; run README examples",
        0 if ok else 1,
        time.monotonic() - start,
        tuple(inv),
        meas,
    )


_CONNECTORS_ABSENT_PROBE = """
import asyncio, importlib.util, json
from nexura_runtime.modules import default_registry
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

mods = [m for m in default_registry.list_modules()
        if m["module_id"].startswith("nexura.connectors.")]
node = WorkflowNode(id="send", handler="nexura.connectors.slack",
                    module_id="nexura.connectors.slack", config={"credential_ref": "cred"})
spec = WorkflowSpec(name="c", entry_node="send", nodes={"send": node})
try:
    asyncio.run(WorkflowEngine(spec, ModuleRegistry()).run({"operation": "list_channels"}))
    error = ""
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"
print(json.dumps({
    "spec": importlib.util.find_spec("nexura_connectors") is not None,
    "ids": len(mods),
    "available": sum(bool(m["is_available"]) for m in mods),
    "reasons_name_pip": all("pip install nexura-connectors" in (m["missing_reason"] or "")
                            for m in mods),
    "workflow_error": error,
}))
"""

_CONNECTORS_PRESENT_PROBE = """
import json, nexura_connectors
from nexura_runtime.modules import default_registry
mods = [m for m in default_registry.list_modules()
        if m["module_id"].startswith("nexura.connectors.")]
print(json.dumps({"file": nexura_connectors.__file__, "ids": len(mods),
                  "available": sum(bool(m["is_available"]) for m in mods)}))
"""


def check_connectors_standalone(work: Path) -> Receipt:
    """nexura-runtime alone reports connectors unavailable; with nexura-connectors they register.

    Reuses the `uv build --all-packages` wheels of the external-consumer step (or builds them).
    """
    start = time.monotonic()
    dist = work / "dist"
    if not any(dist.glob("nexura_connectors-*.whl")):
        dist = work / "connectors-dist"
        sh(["uv", "build", "--all-packages", "--out-dir", str(dist)], timeout=600)
    runtime = next(dist.glob("nexura_runtime-*.whl"), None)
    connectors = next(dist.glob("nexura_connectors-*.whl"), None)
    inv = [Invariant("runtime and connectors wheels built", bool(runtime and connectors))]
    meas: dict[str, Any] = {}
    if runtime is not None and connectors is not None:
        venv = work / "connectors-venv"
        py = venv / "bin" / "python"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
        find_links = ["--find-links", str(dist)]
        sh(["uv", "venv", "--python", "3.12", str(venv)], cwd=work)
        sh(["uv", "pip", "install", "--python", str(py), *find_links, str(runtime)], cwd=work)
        absent, _ = sh([str(py), "-I", "-c", _CONNECTORS_ABSENT_PROBE], cwd=work, env=env)
        before = json.loads(absent.stdout) if absent.returncode == 0 else {}
        inv += [
            Invariant(
                "runtime imports without nexura-connectors",
                absent.returncode == 0,
                absent.stderr[-300:],
            ),
            Invariant("nexura_connectors not installed", before.get("spec") is False),
            Invariant(
                "15 connector ids listed, none available",
                before.get("ids") == 15 and before.get("available") == 0,
            ),
            Invariant(
                "reasons name pip install nexura-connectors", before.get("reasons_name_pip") is True
            ),
            Invariant(
                "workflow using a connector refused with the install hint",
                "pip install nexura-connectors" in before.get("workflow_error", ""),
                before.get("workflow_error", "")[:200],
            ),
        ]
        sh(["uv", "pip", "install", "--python", str(py), *find_links, str(connectors)], cwd=work)
        present, _ = sh([str(py), "-I", "-c", _CONNECTORS_PRESENT_PROBE], cwd=work, env=env)
        after = json.loads(present.stdout) if present.returncode == 0 else {}
        inv += [
            Invariant(
                "nexura_connectors imports from site-packages",
                "site-packages" in after.get("file", ""),
            ),
            Invariant(
                "15 connectors registered and available",
                after.get("ids") == 15 and after.get("available") == 15,
            ),
        ]
        readme = (ROOT / "packages" / "nexura-connectors" / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
        for i, block in enumerate(blocks):
            script = work / f"connectors_readme_{i}.py"
            script.write_text(block, encoding="utf-8")
            run, _ = sh([str(py), "-I", str(script)], cwd=work, env=env)
            inv.append(Invariant(f"README example {i} runs", run.returncode == 0))
        meas = {
            "runtime_wheel": runtime.name,
            "connectors_wheel": connectors.name,
            "without": before,
            "with": after,
            "examples": len(blocks),
        }
    ok = all(i.held for i in inv)
    return Receipt(
        "connectors-standalone-consumer",
        "passed" if ok else "failed",
        "fresh venv; install nexura-runtime wheel alone; probe connectors unavailable; "
        "install nexura-connectors wheel; probe registered; run README examples",
        0 if ok else 1,
        time.monotonic() - start,
        tuple(inv),
        meas,
    )


@dataclass(frozen=True)
class PackageCheck:
    """A package split out of nexura-runtime (P28 step 10 stage 4b) and how to check it.

    ``base`` is installed alone first and ``absent_probe`` must show the feature naming
    ``pip install <package>``; then the package wheel is added and ``present_probe`` must show
    it working. Each probe prints one JSON object; each invariant is a name and a predicate
    over (absent, present). Finally every ```python block of the package README is run.
    """

    package: str
    base: str
    absent_probe: str
    present_probe: str
    invariants: tuple[tuple[str, Callable[[dict[str, Any], dict[str, Any]], bool]], ...]
    wheel_extra: str = ""

    @property
    def check_id(self) -> str:
        return f"{self.package.removeprefix('nexura-')}-standalone-consumer"

    @property
    def import_name(self) -> str:
        return self.package.replace("-", "_")


_ABSENT_SPEC = "import importlib.util as u; spec = u.find_spec({name!r}) is not None"
_PIP = "pip install {package}"

PACKAGE_CHECKS = (
    PackageCheck(
        package="nexura-postgres",
        base="nexura_runtime",
        absent_probe=f"""
{_ABSENT_SPEC.format(name="nexura_postgres")}
import json, os, tempfile
from nexura_agents.contracts import MissingDependencyError
from nexura_runtime.storage import SQLiteRepository, get_storage_repository
sqlite = isinstance(get_storage_repository(os.path.join(tempfile.mkdtemp(), "r.db")),
                    SQLiteRepository)
try:
    get_storage_repository("postgresql://u:p@localhost:5432/db")
    error = ""
except MissingDependencyError as exc:
    error = str(exc)
print(json.dumps({{"spec": spec, "sqlite": sqlite, "error": error}}))
""",
        present_probe="""
import json, nexura_postgres
from nexura_runtime.storage import get_storage_repository
repo = get_storage_repository("postgresql://u:p@localhost:5432/db")
print(json.dumps({"file": nexura_postgres.__file__,
                  "returned": type(repo) is nexura_postgres.PostgresRepository}))
""",
        invariants=(
            ("SQLite storage works without nexura-postgres", lambda a, p: a.get("sqlite") is True),
            (
                "postgresql:// names pip install nexura-postgres",
                lambda a, p: _PIP.format(package="nexura-postgres") in a.get("error", ""),
            ),
            (
                "get_storage_repository returns nexura_postgres.PostgresRepository",
                lambda a, p: p.get("returned") is True,
            ),
        ),
    ),
    PackageCheck(
        package="nexura-qdrant",
        base="nexura_runtime",
        absent_probe=f"""
{_ABSENT_SPEC.format(name="nexura_qdrant")}
import json, sys
from nexura_runtime.knowledge.vector.store import InMemoryVectorStore, VectorStore
print(json.dumps({{"spec": spec, "builtin": isinstance(InMemoryVectorStore(), VectorStore),
                   "qdrant_client": "qdrant_client" in sys.modules}}))
""",
        present_probe="""
import json, nexura_qdrant
from nexura_runtime.knowledge.vector.store import VectorStore
store = nexura_qdrant.QdrantVectorStoreAdapter(url="http://127.0.0.1:9")
print(json.dumps({"file": nexura_qdrant.__file__, "protocol": isinstance(store, VectorStore)}))
""",
        invariants=(
            (
                "built-in vector stores work without nexura-qdrant",
                lambda a, p: a.get("builtin") is True and a.get("qdrant_client") is False,
            ),
            ("adapter implements the runtime VectorStore", lambda a, p: p.get("protocol") is True),
        ),
    ),
    PackageCheck(
        package="nexura-sandbox",
        base="nexura_runtime",
        wheel_extra="[wasi]",
        absent_probe=f"""
{_ABSENT_SPEC.format(name="nexura_sandbox")}
import json
from nexura_runtime.integrations.sandbox import PackageNotInstalledBackend, SandboxConfig
from nexura_runtime.integrations.sandbox import build_backend
from nexura_runtime.modules import default_registry
backends = {{n: build_backend(n) for n in ("wasi", "bubblewrap", "gvisor_oci")}}
module = default_registry.get("nexura.tools.sandbox.execute")
print(json.dumps({{
    "spec": spec,
    "placeholders": all(isinstance(b, PackageNotInstalledBackend) for b in backends.values()),
    "reasons": [b.probe(SandboxConfig()).reason for b in backends.values()],
    "default_hidden": module.manifest.internal,
}}))
""",
        present_probe="""
import json, nexura_sandbox
from nexura_runtime.integrations.sandbox import build_backend
from nexura_runtime.modules import default_registry
classes = {"wasi": nexura_sandbox.WasiWorker, "bubblewrap": nexura_sandbox.BubblewrapWorker,
           "gvisor_oci": nexura_sandbox.GVisorOCIWorker}
module = default_registry.get("nexura.tools.sandbox.execute")
print(json.dumps({"file": nexura_sandbox.__file__,
                  "classes": all(type(build_backend(n)) is c for n, c in classes.items()),
                  "default": type(module.worker).__name__}))
""",
        invariants=(
            (
                "moved backends are placeholders naming pip install nexura-sandbox",
                lambda a, p: (
                    a.get("placeholders") is True
                    and len(a.get("reasons", [])) == 3
                    and all(_PIP.format(package="nexura-sandbox") in r for r in a["reasons"])
                ),
            ),
            ("default sandbox module stays hidden", lambda a, p: a.get("default_hidden") is True),
            (
                "build_backend returns the nexura_sandbox classes",
                lambda a, p: p.get("classes") is True and p.get("default") == "GVisorOCIWorker",
            ),
        ),
    ),
    PackageCheck(
        package="nexura-studio",
        base="nexura_runtime[studio]",
        absent_probe=f"""
{_ABSENT_SPEC.format(name="nexura_studio")}
import json, os, sys, tempfile
from nexura_agents.contracts import MissingDependencyError
from nexura_runtime.service.api import create_api_app
import nexura_runtime.studio as studio
service = type(create_api_app(db_path=os.path.join(tempfile.mkdtemp(), "s.db"))).__name__
try:
    studio.create_app()
    error = ""
except MissingDependencyError as exc:
    error = str(exc)
print(json.dumps({{"spec": spec, "service": service, "error": error,
                   "command": os.path.exists(os.path.join(os.path.dirname(sys.executable),
                                                          "nexura-studio"))}}))
""",
        present_probe="""
import json, os, tempfile, nexura_studio
from nexura_runtime.studio import create_app
app = create_app(db_path=os.path.join(tempfile.mkdtemp(), "studio.sqlite3"))
static = os.path.join(os.path.dirname(nexura_studio.__file__), "static", "index.html")
print(json.dumps({"file": nexura_studio.__file__, "title": app.title,
                  "static": os.path.isfile(static)}))
""",
        invariants=(
            (
                "REST service works without nexura-studio",
                lambda a, p: a.get("service") == "FastAPI",
            ),
            (
                "create_app names pip install nexura-studio",
                lambda a, p: _PIP.format(package="nexura-studio") in a.get("error", ""),
            ),
            (
                "Studio app and its web page come from the package",
                lambda a, p: p.get("title") == "Nexura Studio" and p.get("static") is True,
            ),
        ),
    ),
    PackageCheck(
        package="nexura-vertex",
        base="nexura_agents",
        absent_probe=f"""
{_ABSENT_SPEC.format(name="nexura_vertex")}
import json
from nexura_agents import infer_model
from nexura_agents.errors import SDKValidationError
try:
    infer_model("vertex:gemini-2.5-flash")
    error = ""
except SDKValidationError as exc:
    error = str(exc)
print(json.dumps({{"spec": spec, "error": error,
                   "runtime": u.find_spec("nexura_runtime") is not None}}))
""",
        present_probe="""
import importlib.util as u, json, nexura_vertex
print(json.dumps({"file": nexura_vertex.__file__,
                  "runtime": u.find_spec("nexura_runtime") is not None}))
""",
        invariants=(
            (
                "vertex: model strings name pip install nexura-vertex",
                lambda a, p: _PIP.format(package="nexura-vertex") in a.get("error", ""),
            ),
            (
                "nexura-vertex needs nexura-agents only, not nexura-runtime",
                lambda a, p: a.get("runtime") is False and p.get("runtime") is False,
            ),
        ),
    ),
    PackageCheck(
        package="nexura-guardrails",
        base="nexura_agents",
        absent_probe=f"""
{_ABSENT_SPEC.format(name="nexura_guardrails")}
import json
from nexura_agents import block_pii
print(json.dumps({{"spec": spec, "builtin": callable(block_pii),
                   "runtime": u.find_spec("nexura_runtime") is not None}}))
""",
        present_probe="""
import asyncio, importlib.util as u, json, nexura_guardrails
from nexura_agents import FakeModel
from nexura_guardrails import JudgeCheck, evaluate, injection, tiered
reply = '{"decision": "block", "categories": ["fraud"], "score": 0.9, "reason": "otp"}'
judge = JudgeCheck(FakeModel([reply]), "Block OTP requests.", categories=["fraud", "other"])
verdict = asyncio.run(evaluate(tiered(injection(), judge), "share your OTP"))
print(json.dumps({"file": nexura_guardrails.__file__, "decision": verdict.decision,
                  "runtime": u.find_spec("nexura_runtime") is not None}))
""",
        invariants=(
            (
                "nexura-agents alone keeps its pre-filters and has no nexura_guardrails",
                lambda a, p: a.get("spec") is False and a.get("builtin") is True,
            ),
            (
                "an installed nexura-guardrails runs a tiered judge check offline",
                lambda a, p: p.get("decision") == "block",
            ),
            (
                "nexura-guardrails needs nexura-agents only, not nexura-runtime",
                lambda a, p: a.get("runtime") is False and p.get("runtime") is False,
            ),
        ),
    ),
)


def check_package_standalone(work: Path, check: PackageCheck) -> Receipt:
    """``check.base`` alone names the package; with the package wheel the feature works.

    Reuses the `uv build --all-packages` wheels of the external-consumer step (or builds them).
    """
    start = time.monotonic()
    dist = work / "dist"
    if not any(dist.glob(f"{check.import_name}-*.whl")):
        dist = work / "packages-dist"
        if not any(dist.glob(f"{check.import_name}-*.whl")):
            sh(["uv", "build", "--all-packages", "--out-dir", str(dist)], timeout=600)
    base_dist, _, base_extra = check.base.partition("[")
    base = next(dist.glob(f"{base_dist}-*.whl"), None)
    wheel = next(dist.glob(f"{check.import_name}-*.whl"), None)
    inv = [Invariant("base and package wheels built", bool(base and wheel))]
    meas: dict[str, Any] = {}
    if base is not None and wheel is not None:
        venv = work / f"{check.import_name}-venv"
        py = venv / "bin" / "python"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
        find_links = ["--find-links", str(dist)]
        sh(["uv", "venv", "--python", "3.12", str(venv)], cwd=work)
        base_req = f"{base}[{base_extra.rstrip(']')}]" if base_extra else str(base)
        sh(["uv", "pip", "install", "--python", str(py), *find_links, base_req], cwd=work)
        absent, _ = sh([str(py), "-I", "-c", check.absent_probe], cwd=work, env=env)
        before = json.loads(absent.stdout) if absent.returncode == 0 else {}
        sh(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(py),
                *find_links,
                f"{wheel}{check.wheel_extra}",
            ],
            cwd=work,
        )
        present, _ = sh([str(py), "-I", "-c", check.present_probe], cwd=work, env=env)
        after = json.loads(present.stdout) if present.returncode == 0 else {}
        inv += [
            Invariant(
                f"{base_dist} imports without {check.package}",
                absent.returncode == 0,
                absent.stderr[-300:],
            ),
            Invariant(f"{check.import_name} not installed at first", before.get("spec") is False),
            Invariant(
                f"{check.import_name} imports from site-packages",
                "site-packages" in after.get("file", ""),
                present.stderr[-300:],
            ),
        ]
        inv += [Invariant(name, bool(test(before, after))) for name, test in check.invariants]
        if check.package == "nexura-studio":
            help_run, _ = sh([str(venv / "bin" / "nexura-studio"), "--help"], cwd=work, env=env)
            inv.append(
                Invariant(
                    "nexura-studio command absent before, installed by the package",
                    before.get("command") is False and help_run.returncode == 0,
                )
            )
        readme = (ROOT / "packages" / check.package / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
        for i, block in enumerate(blocks):
            script = work / f"{check.import_name}_readme_{i}.py"
            script.write_text(block, encoding="utf-8")
            run, _ = sh([str(py), "-I", str(script)], cwd=work, env=env)
            inv.append(
                Invariant(f"README example {i} runs", run.returncode == 0, run.stderr[-300:])
            )
        meas = {
            "base_wheel": base.name,
            "package_wheel": wheel.name,
            "without": before,
            "with": after,
            "examples": len(blocks),
        }
    ok = all(i.held for i in inv)
    return Receipt(
        check.check_id,
        "passed" if ok else "failed",
        f"fresh venv; install {check.base} alone; probe; install {check.package} wheel; probe; "
        "run README examples",
        0 if ok else 1,
        time.monotonic() - start,
        tuple(inv),
        meas,
    )


def check_ts_client() -> Receipt:
    cwd = ROOT / "clients" / "typescript"
    return command_receipt("ts-client", ["sh", "-c", "npm test && npm run build"], cwd=cwd)


def check_studio_unit() -> Receipt:
    cwd = ROOT / "studio-web"
    cmd = ["sh", "-c", "npm run typecheck && npm run lint && npx vitest run && npm run build"]
    return command_receipt("studio-web-unit-and-build", cmd, cwd=cwd)


def check_studio_assets() -> Receipt:
    start = time.monotonic()
    assets = sorted((ROOT / "studio-web" / "dist" / "assets").glob("*"))
    js = [a for a in assets if a.suffix == ".js"]
    css = [a for a in assets if a.suffix == ".css"]
    gz = {a.name: len(gzip.compress(a.read_bytes())) for a in js + css}
    inv = (Invariant("built JS and CSS assets present", bool(js) and bool(css)),)
    return Receipt(
        "studio-route-assets",
        "passed" if inv[0].held else "failed",
        "measure studio-web/dist/assets after npm run build",
        0 if inv[0].held else 1,
        time.monotonic() - start,
        inv,
        {
            "js_files": len(js),
            "total_gzip_kb": round(sum(gz.values()) / 1024, 1),
            "largest_gzip_kb": round(max(gz.values(), default=0) / 1024, 1),
        },
    )


def check_browser() -> Receipt:
    def extra(out: str) -> tuple[list[Invariant], dict[str, Any]]:
        m = re.search(r"(\d+) passed", out)
        failed = re.search(r"(\d+) failed", out)
        return (
            [Invariant("browser tests ran", bool(m)), Invariant("no browser failures", not failed)],
            {"passed": int(m[1]) if m else 0},
        )

    return command_receipt(
        "studio-browser-e2e", ["npx", "playwright", "test"], cwd=ROOT / "studio-web", extra=extra
    )


# ---------------------------------------------------------------------------
# Edition boundary (EDITIONS_PLAN_2026-10-18 section 2.4). REPORT-ONLY until P28
# step 9: the receipt is not in REQUIRED, so its findings never change readiness.
# ---------------------------------------------------------------------------

BOUNDARY_FILE = OUT / "edition_boundary.json"
BOUNDARY_ENFORCED_FROM = "P28 step 9"

# Where OSS code lives (plan 2.4 check 5), with the file suffixes scanned there.
OSS_SOURCE_ROOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("src", (".py",)),
    ("packages", (".py",)),
    ("clients/typescript/src", (".ts", ".tsx", ".js", ".mjs")),
    ("studio-web/src", (".ts", ".tsx", ".js", ".jsx")),
)
_SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", ".venv"}

_ENTERPRISE_MEMBER = re.compile(r"(?:^|/)enterprise(?:/|$)|nexura[_-]enterprise")
_PY_IMPORT = re.compile(
    r"^\s*(?:from\s+nexura_enterprise\b|import\s+(?:[\w.]+\s*,\s*)*nexura_enterprise\b)"
)
_PY_DYNAMIC = re.compile(r"""(?:import_module|__import__)\(\s*['"]nexura_enterprise\b""")
_TS_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]([^'"]+)['"]""")
_ENTERPRISE_SPECIFIER = re.compile(r"(?:^|/)enterprise(?:/|$)|nexura[_-]enterprise")

#: API path segments of closed-edition routes: organisations (with their members and invites),
#: publishing (publications, environments, promotions), the hosted secrets admin API and
#: workspace members. Their clients live in the closed edition (audit finding PKG-03), so no OSS
#: artifact (wheel/sdist member, npm tarball member, Studio bundle file) may contain one as a route
#: string. A segment counts when it follows `/api/v1`, a quote, a backtick, `}` (a template
#: placeholder) or any character that cannot end an identifier or file path; a module path such
#: as `nexura_runtime/secrets/store.py` does not count.
CLOSED_CLIENT_ROUTE_SEGMENTS = ("organisations", "published", "secrets", "members", "my-membership")
_CLOSED_CLIENT_ROUTE = re.compile(
    r"(?:/api/v1|(?<![\w.\-]))/("
    + "|".join(re.escape(s) for s in CLOSED_CLIENT_ROUTE_SEGMENTS)
    + r")(?![\w\-])"
)
#: Files never scanned for route strings (binary payloads).
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf"}


@dataclass(frozen=True)
class ClosedModule:
    """One path the plan marks CLOSED (section 1) that must leave the OSS tree."""

    path: str
    step: int
    kind: str = "move"  # "move": the whole path leaves; "split": only `symbols` leave
    symbols: tuple[str, ...] = ()
    note: str = ""
    #: Where it went in the closed edition (relative to `enterprise/`), once moved.
    dest: str = ""


#: The last migration step that has been carried out. Entries of this step or earlier must be
#: absent from the OSS tree ("moved"); later ones are still expected in it.
MOVED_THROUGH_STEP = 8

# Step 7 test files now contain OSS-local equivalents. Track the closed fixture
# APIs rather than their old scenario names (security scenarios stay OSS).
CLOSED_AUTH_TEST_MARKERS = (
    "nexura_runtime.service.auth.tokens",
    "nexura_runtime.service.auth.api_keys",
    "ApiKeyManager(",
    "TokenValidator(",
    "create_token(",
    "SharedServiceAuthProvider(",
    "ProfileAuthProvider(",
    "token_validator=",
    "api_key_manager=",
    ".add_member(",
    ".remove_member(",
)

# Maintained list of what has to move, derived from EDITIONS_PLAN section 1 and the step table
# in section 5. Entries stay after their step so the report shows them as moved; `dest` says
# where they went.
CLOSED_MODULES: tuple[ClosedModule, ...] = (
    # Step 4 (done): publishing, the widget and the multi-tenant hosting files. Founder decision
    # 2026-10-18 on question 7: the single-user local Docker setup (`deployment/`) stays OSS.
    ClosedModule(
        "src/nexura_runtime/publishing",
        4,
        note="gateway, keys, selfhost, widget; ratelimit stays OSS (service/api/ratelimit.py)",
        dest="src/nexura_enterprise/publishing",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/publishing.py",
        4,
        dest="src/nexura_enterprise/publishing/routes.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/builtin_extensions.py",
        4,
        "split",
        ("PublishingExtension", "mount_publishing"),
        dest="src/nexura_enterprise/publishing/extension.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/client.py",
        4,
        "split",
        (
            '("GET", "/api/v1/published"): ("published", "list")',
            "class PublishedClient",
            "self.published = ",
        ),
        "COVERED_ROUTES entries; the client (client.published) followed in PKG-03 to "
        "enterprise/src/nexura_enterprise/clients/hosted.py",
        dest="src/nexura_enterprise/publishing/client_routes.py",
    ),
    ClosedModule(
        "docs/api-reference.md",
        4,
        "split",
        ("## Publishing (`nexura_runtime.publishing`)",),
    ),
    ClosedModule("studio-web/src/features/publish", 4, dest="studio-web/features/publish"),
    ClosedModule(
        "studio-web/src/app/builtinExtensions.ts",
        4,
        "split",
        ("publishExtension", "PublishPanel"),
        dest="studio-web/features/publish/extension.ts",
    ),
    ClosedModule(
        "studio-web/src/i18n/en.json",
        4,
        "split",
        ('"publish": {',),
        dest="studio-web/features/publish/i18n/en.json",
    ),
    ClosedModule(
        "studio-web/src/i18n/hi.json",
        4,
        "split",
        ('"publish": {',),
        dest="studio-web/features/publish/i18n/hi.json",
    ),
    ClosedModule("studio-web/package.json", 4, "split", ('"build:widget"',)),
    ClosedModule("studio-web/widget", 4, dest="widget"),
    ClosedModule("studio-web/vite.widget.config.ts", 4, dest="studio-web/vite.widget.config.ts"),
    ClosedModule(
        "studio-web/tests/e2e/publish.spec.ts", 4, dest="studio-web/tests/e2e/publish.spec.ts"
    ),
    ClosedModule(
        "studio-web/tests/unit/publish_widget.test.ts",
        4,
        dest="studio-web/tests/unit/publish_widget.test.ts",
    ),
    ClosedModule(
        "studio-web/tests/unit/publish_snippet.test.ts",
        4,
        note="deriveEndpoint cases stay OSS (tests/unit/endpoint.test.ts)",
        dest="studio-web/tests/unit/publish_snippet.test.ts",
    ),
    ClosedModule("docs/publishing.md", 4, dest="docs/publishing.md"),
    ClosedModule("docs/self-hosting.md", 4, dest="docs/self-hosting.md"),
    ClosedModule("tests/test_publish.py", 4, dest="tests/test_publish.py"),
    ClosedModule("tests/test_publish_review.py", 4, dest="tests/test_publish_review.py"),
    ClosedModule(
        "tests/test_publishing_environments.py", 4, dest="tests/test_publishing_environments.py"
    ),
    ClosedModule("tests/test_selfhost_config.py", 4, dest="tests/test_selfhost_config.py"),
    ClosedModule(
        "Dockerfile", 4, note="multi-tenant hosting (question 7)", dest="deploy/Dockerfile"
    ),
    ClosedModule(
        "docker-compose.yml",
        4,
        note="multi-tenant hosting (question 7)",
        dest="deploy/docker-compose.yml",
    ),
    ClosedModule(
        ".env.example", 4, note="multi-tenant hosting (question 7)", dest="deploy/.env.example"
    ),
    # Step 5 (done): organisations and the hosted secrets features. Founder decision 2026-10-18
    # on question 3: the local encrypted secrets file (`secrets/store.py`, `keys.py`, `errors.py`)
    # stays OSS, so `secrets/` is split: the `/api/v1/secrets` routes and master-key rotation move.
    ClosedModule(
        "src/nexura_runtime/secrets/api.py",
        5,
        note="hosted /api/v1/secrets admin routes",
        dest="src/nexura_enterprise/secrets/routes.py",
    ),
    ClosedModule(
        "src/nexura_runtime/secrets/store.py",
        5,
        "split",
        ("def rotate_master_key",),
        "the local encrypted store stays OSS (question 3); rotation moved",
        dest="src/nexura_enterprise/secrets/rotation.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/organisations.py",
        5,
        note="local encrypted store wiring stays OSS (service/api/local_secrets.py)",
        dest="src/nexura_enterprise/organisations/routes.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/workspaces/organisations.py",
        5,
        dest="src/nexura_enterprise/organisations/repository.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/workspaces/models.py",
        5,
        "split",
        ("OrgRole", "Organisation", "OrgMember", "OrgInvite", "OrgAuditEvent"),
        dest="src/nexura_enterprise/organisations/models.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/workspaces/__init__.py",
        5,
        "split",
        ("OrganisationRepository", "OrgRole"),
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/builtin_extensions.py",
        5,
        "split",
        ("OrganisationsExtension", "mount_organisations"),
        dest="src/nexura_enterprise/organisations/extension.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/client.py",
        5,
        "split",
        (
            '("GET", "/api/v1/organisations"): ("organisations", "list")',
            '("GET", "/api/v1/secrets"): ("secrets", "list")',
            "class OrganisationsClient",
            "class SecretsClient",
            "self.organisations = ",
            "self.secrets = ",
        ),
        "COVERED_ROUTES entries; the clients (client.organisations, client.secrets) followed in "
        "PKG-03 to enterprise/src/nexura_enterprise/clients/hosted.py",
        dest="src/nexura_enterprise/organisations/client_routes.py",
    ),
    ClosedModule(
        "studio-web/src/features/organisation", 5, dest="studio-web/features/organisation"
    ),
    ClosedModule(
        "studio-web/src/app/builtinExtensions.ts",
        5,
        "split",
        ("organisationExtension", "OrganisationView"),
        dest="studio-web/features/organisation/extension.ts",
    ),
    ClosedModule(
        "studio-web/src/i18n/en.json",
        5,
        "split",
        ('"organisation": {', '"organisation": "'),
        dest="studio-web/features/organisation/i18n/en.json",
    ),
    ClosedModule(
        "studio-web/src/i18n/hi.json",
        5,
        "split",
        ('"organisation": {', '"organisation": "'),
        dest="studio-web/features/organisation/i18n/hi.json",
    ),
    ClosedModule(
        "studio-web/tests/e2e/organisation.spec.ts",
        5,
        dest="studio-web/tests/e2e/organisation.spec.ts",
    ),
    ClosedModule("docs/organisations-and-secrets.md", 5, dest="docs/organisations-and-secrets.md"),
    ClosedModule("tests/test_orgs_and_secrets.py", 5, dest="tests/test_orgs_and_secrets.py"),
    ClosedModule(
        "tests/test_b40_revocation_stops_runs.py",
        5,
        "split",
        ("test_org_member_removal_cancels_runs_in_every_org_workspace",),
        dest="tests/test_b40_org_revocation.py",
    ),
    # Step 6 (done): Nexura V2 compatibility. Founder decision 2026-10-18 on question 5: the
    # Langflow importer, the export back to Langflow JSON and the trace exporter
    # (`nexura_v2/exporter.py`, used by `examples/sdk_integration/p01.py`) stay OSS; the V2-shaped
    # run server, the V2 SSE shapes, Firebase auth, V2 RBAC, V2 records and the shadow harness move.
    ClosedModule(
        "src/nexura_runtime/integrations/langflow/server.py",
        6,
        note="V2FlowServer, PerCaller (V7)",
        dest="src/nexura_enterprise/v2/server.py",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/langflow/events.py",
        6,
        note="the engine keeps an OSS FlowEventMapper seam (build.py)",
        dest="src/nexura_enterprise/v2/events.py",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/langflow/__init__.py",
        6,
        "split",
        ("SSEEvent", "V2EventMapper"),
        "the importer and export stay OSS",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/langflow/build.py",
        6,
        "split",
        ("V2EventMapper", "from .events import"),
        "the engine stays OSS; stream_events/stream_flow_events now take a mapper",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/nexura_v2/auth.py",
        6,
        dest="src/nexura_enterprise/v2/auth.py",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/nexura_v2/policy.py",
        6,
        dest="src/nexura_enterprise/v2/policy.py",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/nexura_v2/records.py",
        6,
        dest="src/nexura_enterprise/v2/records.py",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/nexura_v2/shadow.py",
        6,
        dest="src/nexura_enterprise/v2/shadow.py",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/nexura_v2/__init__.py",
        6,
        "split",
        ("FirebaseIdTokenVerifier", "RbacPolicy", "run_shadow", "build_run_records"),
        "the trace exporter exports stay OSS (question 5)",
        dest="src/nexura_enterprise/v2/__init__.py",
    ),
    ClosedModule(
        "src/nexura_runtime/integrations/nexura_v2/README.md",
        6,
        "split",
        ("## Platform-service adapters (V4)",),
        dest="docs/v2-compatibility.md",
    ),
    ClosedModule("tests/integrations/nexura_v2", 6, dest="tests/v2"),
    ClosedModule("tests/integrations/langflow/test_v2_api.py", 6, dest="tests/v2/test_v2_api.py"),
    ClosedModule(
        "tests/integrations/langflow/test_v2_api_review.py",
        6,
        dest="tests/v2/test_v2_api_review.py",
    ),
    ClosedModule(
        "tests/integrations/langflow/test_v7_per_caller.py",
        6,
        dest="tests/v2/test_v7_per_caller.py",
    ),
    ClosedModule(
        "tests/integrations/langflow/test_v7_review_fixes.py",
        6,
        dest="tests/v2/test_v7_review_fixes.py",
    ),
    ClosedModule(
        "tests/integrations/langflow/test_langflow_run.py",
        6,
        "split",
        ("V2EventMapper", "def test_sse_"),
        dest="tests/v2/test_v2_event_stream.py",
    ),
    ClosedModule(
        "tests/integrations/langflow/test_langflow_review.py",
        6,
        "split",
        ("test_tokens_reach_consumer_before_model_finishes",),
        dest="tests/v2/test_v2_event_stream.py",
    ),
    ClosedModule(
        "tests/integrations/langflow/test_v3_review.py",
        6,
        "split",
        ("V2FlowServer", "test_v2_server_extended_option_is_opt_in"),
        dest="tests/v2/test_v3_review_server.py",
    ),
    ClosedModule(
        "tests/test_audit10_fixes.py",
        6,
        "split",
        ("build_run_records", "V2FlowServer"),
        dest="tests/v2/test_audit10_v2.py",
    ),
    ClosedModule(
        "tests/security/b28/test_cve_patterns_runs.py",
        6,
        "split",
        ("V2FlowServer", "V2Principal", "RbacPolicy"),
        dest="tests/v2/test_b28_v2_server.py",
    ),
    ClosedModule("docs/migrating-from-langflow.md", 6, dest="docs/migrating-from-langflow.md"),
    ClosedModule(
        "docs/langflow-import.md",
        6,
        "split",
        (
            "## V2 event stream",
            "## Serving flows with a V2-compatible API",
            "## Behind V2 identity and policies",
        ),
        "the importer and export sections stay OSS",
        dest="docs/v2-compatibility.md",
    ),
    ClosedModule(
        "docs/api-reference.md",
        6,
        "split",
        ("### `V2EventMapper`", "### `RbacPolicy`", "### `run_shadow`"),
        "regenerated by scripts/gen_api_reference.py",
    ),
    # Step 7 (first half, backend): authentication and membership. Section 3 of the plan: the
    # open-source edition has one principal (`local-owner`, role admin) and no logins, tokens,
    # API keys, members or invites; `X-Principal-*` trust is gone. Safety controls stay OSS.
    ClosedModule(
        "src/nexura_runtime/service/auth/tokens.py", 7, dest="src/nexura_enterprise/auth/tokens.py"
    ),
    ClosedModule(
        "src/nexura_runtime/service/auth/api_keys.py",
        7,
        dest="src/nexura_enterprise/auth/api_keys.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/auth/resources.py",
        7,
        "split",
        ("RESOURCE_SCOPED_ROUTES", "def route_allows_resource_scoped", "def validate_resources"),
        "require_resource (the per-route check on a Principal) stays OSS",
        dest="src/nexura_enterprise/auth/resources.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/module.py", 7, dest="src/nexura_enterprise/auth/module.py"
    ),
    ClosedModule(
        "src/nexura_runtime/service/auth/models.py",
        7,
        "split",
        ("class ApiKey", "class TokenPayload", "class MembershipAuditEvent"),
        dest="src/nexura_enterprise/auth/models.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/auth/profile.py",
        7,
        "split",
        ("def resolve_principal", "token_validator", "api_key_manager", "operator_principal_ids"),
        "the profile switch stays OSS",
        dest="src/nexura_enterprise/auth/profile.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/auth/__init__.py",
        7,
        "split",
        ("ApiKeyManager", "TokenValidator", "create_token"),
    ),
    ClosedModule(
        "src/nexura_runtime/service/__init__.py",
        7,
        "split",
        ("AuthServiceModule", "WorkspaceServiceModule", "ApiKeyManager", "TokenValidator"),
    ),
    ClosedModule(
        "src/nexura_runtime/service/workspaces/repository.py",
        7,
        "split",
        ("MembershipAuditEvent", "def add_member", "def remove_member", "nexura_service_members"),
        "workspaces and ownership stay OSS",
        dest="src/nexura_enterprise/membership/repository.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/workspaces/membership.py",
        7,
        "split",
        ("def add_member", "def get_audit_events", "MembershipAuditEvent"),
        "the read side of MembershipStore stays OSS",
        dest="src/nexura_enterprise/membership/repository.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/middleware.py",
        7,
        "split",
        (
            "def authenticate_shared",
            "def extract_credentials",
            "INVITE_ACCEPT_ROUTE",
            "invite_accept",
        ),
        "invite-accept exemption replaced by AuthProvider.routes_without_membership",
        dest="src/nexura_enterprise/auth/shared.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/auth_providers.py",
        7,
        "split",
        (
            "x-principal-id",
            "x-principal-role",
            "class SharedServiceAuthProvider",
            "class ProfileAuthProvider",
        ),
        "header trust is removed from OSS (an enterprise test helper keeps it)",
        dest="src/nexura_enterprise/auth/providers.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/app.py",
        7,
        "split",
        ("token_validator: ", "api_key_manager: ", "api_key_revocation_listener"),
        dest="src/nexura_enterprise/app.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/run_revocation.py",
        7,
        "split",
        ("def api_key_revocation_listener",),
        dest="src/nexura_enterprise/auth/revocation.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/workspaces.py",
        7,
        "split",
        ('"/members"', '"/my-membership"', "_reject_org_member_change", "X-Principal-ID"),
        dest="src/nexura_enterprise/membership/routes.py",
    ),
    ClosedModule(
        "src/nexura_runtime/service/api/client.py",
        7,
        "split",
        (
            '("GET", "/api/v1/workspaces/{workspace_id}/members"): ("members", "list_members")',
            "class WorkspaceMembersClient",
            "self.members = ",
        ),
        "COVERED_ROUTES entries; the Python members client followed in PKG-03 to "
        "enterprise/src/nexura_enterprise/clients/hosted.py",
        dest="src/nexura_enterprise/membership/client_routes.py",
    ),
    ClosedModule(
        "clients/typescript/src/client.ts",
        7,
        "split",
        ("WorkspaceMembersClient",),
        dest="clients/typescript/src/members.ts",
    ),
    ClosedModule(
        "clients/typescript/src/types.ts",
        7,
        "split",
        ("interface MyMembershipInfo", "interface WorkspaceMemberItem"),
        dest="clients/typescript/src/members.ts",
    ),
    ClosedModule(
        "clients/typescript/tests/p29_client_coverage.test.mjs",
        7,
        "split",
        ("client.members.",),
        dest="clients/typescript/tests/members.test.mjs",
    ),
    ClosedModule("examples/backend/b24.py", 7, dest="examples/b24.py"),
    ClosedModule(
        "docs/studio-and-service.md",
        7,
        "split",
        ("## Resource-scoped API keys",),
        dest="docs/authentication.md",
    ),
    ClosedModule(
        "tests/b39_review/test_b39_review_shared_delivery.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/b39_review/test_b39_review_shared_delivery.py",
    ),
    ClosedModule(
        "tests/b39_review/test_b39_review_snippet.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/b39_review/test_b39_review_snippet.py",
    ),
    ClosedModule(
        "tests/b40_review/test_b40_review.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/b40_review/test_b40_review.py",
    ),
    ClosedModule(
        "tests/backend/b24/test_identity_workspaces.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/b24/test_identity_workspaces.py",
    ),
    ClosedModule(
        "tests/backend/b31/test_api_and_clients.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/b31/test_api_and_clients.py",
    ),
    ClosedModule(
        "tests/backend/f00/test_composite_gate_and_handoff.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/f00/test_composite_gate_and_handoff.py",
    ),
    ClosedModule(
        "tests/backend/test_audit3_platform.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_audit3_platform.py",
    ),
    ClosedModule(
        "tests/backend/test_chat_models.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_chat_models.py",
    ),
    ClosedModule(
        "tests/backend/test_credentials_and_roles_api.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_credentials_and_roles_api.py",
    ),
    ClosedModule(
        "tests/backend/test_runs_interrupt_history_fork.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_runs_interrupt_history_fork.py",
    ),
    ClosedModule(
        "tests/backend/test_runs_subruns_child.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_runs_subruns_child.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b7_auth_app.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b7_auth_app.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b7_runs_workflows.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b7_runs_workflows.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b7_sessions_knowledge.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b7_sessions_knowledge.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b7_triggers.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b7_triggers.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b8_auth_app.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b8_auth_app.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b8_runs_workflows.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b8_runs_workflows.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b8_sessions_integrations.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b8_sessions_integrations.py",
    ),
    ClosedModule(
        "tests/backend/test_security_b8_triggers.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/backend/test_security_b8_triggers.py",
    ),
    ClosedModule(
        "tests/p26_review/test_p26_review_chat.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p26_review/test_p26_review_chat.py",
    ),
    ClosedModule(
        "tests/p26_review/test_p26_review_output_schema.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p26_review/test_p26_review_output_schema.py",
    ),
    ClosedModule(
        "tests/p26_review/test_p26_review_redteam.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p26_review/test_p26_review_redteam.py",
    ),
    ClosedModule(
        "tests/p26_review/test_p26_review_tools.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p26_review/test_p26_review_tools.py",
    ),
    ClosedModule(
        "tests/p28_seams/test_p28_auth_provider.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p28_seams/test_p28_auth_provider.py",
    ),
    ClosedModule(
        "tests/p28_seams/test_p28_credentials_and_membership.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p28_seams/test_p28_credentials_and_membership.py",
    ),
    ClosedModule(
        "tests/p28_seams/test_p28_service_extensions.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p28_seams/test_p28_service_extensions.py",
    ),
    ClosedModule(
        "tests/p28_seams/test_p28s2_extension_guards.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p28_seams/test_p28s2_extension_guards.py",
    ),
    ClosedModule(
        "tests/p28_seams/test_p28s2_review_seams.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/p28_seams/test_p28s2_review_seams.py",
    ),
    ClosedModule(
        "tests/security/b28/test_cve_patterns_runs.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/security/b28/test_cve_patterns_runs.py",
    ),
    ClosedModule(
        "tests/security/b28/test_resource_scoped_keys.py",
        7,
        dest="tests/auth/security/b28/test_resource_scoped_keys.py",
    ),
    ClosedModule(
        "tests/security/p27_review/test_p27_review_sandbox_fixes.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/security/p27_review/test_p27_review_sandbox_fixes.py",
    ),
    ClosedModule(
        "tests/security/test_secure_defaults.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/security/test_secure_defaults.py",
    ),
    ClosedModule(
        "tests/test_audit10_fixes.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_audit10_fixes.py",
    ),
    ClosedModule(
        "tests/test_audit14_security_fixes.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_audit14_security_fixes.py",
    ),
    ClosedModule(
        "tests/test_b34_palette_flags.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_b34_palette_flags.py",
    ),
    ClosedModule(
        "tests/test_b40_revocation_stops_runs.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_b40_revocation_stops_runs.py",
    ),
    ClosedModule(
        "tests/test_builder_assistant.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_builder_assistant.py",
    ),
    ClosedModule(
        "tests/test_builder_assistant_review.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_builder_assistant_review.py",
    ),
    ClosedModule(
        "tests/test_local_encrypted_secrets.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_local_encrypted_secrets.py",
    ),
    ClosedModule(
        "tests/test_p22_workflow_tests.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_p22_workflow_tests.py",
    ),
    ClosedModule(
        "tests/test_p26_agents.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_p26_agents.py",
    ),
    ClosedModule(
        "tests/test_p30_openai_import.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_p30_openai_import.py",
    ),
    ClosedModule(
        "tests/test_playground_knowledge.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_playground_knowledge.py",
    ),
    ClosedModule(
        "tests/test_redteam_service.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_redteam_service.py",
    ),
    ClosedModule(
        "tests/test_skills_service.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_skills_service.py",
    ),
    ClosedModule(
        "tests/test_studio_traces_review.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_studio_traces_review.py",
    ),
    ClosedModule(
        "tests/test_templates.py",
        7,
        "split",
        CLOSED_AUTH_TEST_MARKERS,
        "Enterprise auth fixtures moved; OSS local equivalents retained",
        dest="tests/auth/test_templates.py",
    ),
    # Audit finding PKG-03 (founder decision 2026-10-20): the client methods for the routes that
    # moved in steps 4, 5 and 7 follow them. Python: `EnterpriseClient(NexuraClient)` (the
    # symbols are in the step 4, 5 and 7 `client.py` rows above);
    # TypeScript: the `@liceron/nexura-sdk-enterprise` add-on; Studio: a membership source
    # registered by the enterprise Studio extension. The release gate also scans every OSS
    # artifact for their route strings (CLOSED_CLIENT_ROUTE_SEGMENTS).
    ClosedModule(
        "clients/typescript/src/client.ts",
        4,
        "split",
        ("class PublishedClient", "this.published = "),
        "PKG-03: client.published",
        dest="clients/typescript/src/published.ts",
    ),
    ClosedModule(
        "clients/typescript/src/client.ts",
        5,
        "split",
        (
            "class OrganisationsClient",
            "this.organisations = ",
            "class SecretsClient",
            "this.secrets = ",
        ),
        "PKG-03: client.organisations (and client.secrets, now clients/typescript/src/secrets.ts)",
        dest="clients/typescript/src/organisations.ts",
    ),
    ClosedModule(
        "clients/typescript/src/types.ts",
        4,
        "split",
        ("interface PublicationRecord", "interface PromotionRecord", "interface MapChangeRecord"),
        "PKG-03",
        dest="clients/typescript/src/types.ts",
    ),
    ClosedModule(
        "clients/typescript/src/types.ts",
        5,
        "split",
        ("interface SecretMetadataItem", "interface OrgMemberItem", "interface OrgInviteItem"),
        "PKG-03",
        dest="clients/typescript/src/types.ts",
    ),
    ClosedModule(
        "studio-web/src/api/membership.ts",
        7,
        "split",
        ("/my-membership", "}/members"),
        "PKG-03: Studio reads memberships from an extension's source",
        dest="studio-web/features/membership/extension.ts",
    ),
    ClosedModule(
        "tests/test_p29_client_coverage.py",
        4,
        "split",
        ("client.published.",),
        "PKG-03",
        dest="tests/clients/test_p29_client_coverage.py",
    ),
    ClosedModule(
        "tests/test_p29_client_coverage.py",
        5,
        "split",
        ("client.organisations.", "client.secrets."),
        "PKG-03",
        dest="tests/clients/test_p29_client_coverage.py",
    ),
    ClosedModule(
        "tests/test_p29_client_coverage.py",
        7,
        "split",
        ("client.members.",),
        "PKG-03",
        dest="tests/clients/test_p29_client_coverage.py",
    ),
    ClosedModule(
        "clients/typescript/tests/p29_client_coverage.test.mjs",
        4,
        "split",
        ("client.published.",),
        "PKG-03",
        dest="clients/typescript/tests/hosted.test.mjs",
    ),
    ClosedModule(
        "clients/typescript/tests/p29_client_coverage.test.mjs",
        5,
        "split",
        ("client.organisations.", "client.secrets."),
        "PKG-03",
        dest="clients/typescript/tests/hosted.test.mjs",
    ),
    # Step 8: capability split.
    ClosedModule(
        "src/nexura_runtime/release/capabilities.json", 8, "split", ('"profile": "auth"',)
    ),
)


def archive_members(path: Path) -> list[str]:
    """Member paths of a wheel (zip) or sdist (.tar.gz)."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    with tarfile.open(path, "r:*") as tf:
        return tf.getnames()


def enterprise_members(members: Iterable[str]) -> list[str]:
    """Members whose path has an `enterprise/` segment or names `nexura_enterprise`."""
    return sorted(m for m in members if _ENTERPRISE_MEMBER.search(m))


def _member_candidates(path: str) -> tuple[str, ...]:
    """Spellings of a repo path inside an artifact (sdist keeps it; a wheel drops `src/`)."""
    out = [path]
    for prefix in ("src/", *(f"{source}/" for source in PACKAGE_SOURCES)):
        if path.startswith(prefix):
            out.append(path[len(prefix) :])
    return tuple(out)


def closed_modules_in_members(
    members: Iterable[str],
    *,
    sdist: bool = False,
    closed: Iterable[ClosedModule] = CLOSED_MODULES,
) -> list[str]:
    """Closed-module paths (whole-path moves only) that an artifact still ships."""
    names = [m.split("/", 1)[1] if sdist and "/" in m else m for m in members]
    shipped: list[str] = []
    for module in closed:
        if module.kind != "move":
            continue
        cands = _member_candidates(module.path)
        if any(n == c or n.startswith(c + "/") for n in names for c in cands):
            shipped.append(module.path)
    return shipped


def enterprise_imports_in_text(text: str, suffix: str) -> list[tuple[int, str]]:
    """(line, text) of every import of `nexura_enterprise` or of a path under `enterprise/`."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if suffix == ".py":
            found = bool(_PY_IMPORT.search(line) or _PY_DYNAMIC.search(line))
        else:
            found = any(_ENTERPRISE_SPECIFIER.search(spec) for spec in _TS_SPECIFIER.findall(line))
        if found:
            hits.append((lineno, line.strip()[:200]))
    return hits


def closed_route_strings_in_text(text: str) -> list[str]:
    """Closed-edition route segments (`/organisations`, ...) used as route strings in `text`."""
    return sorted({f"/{m.group(1)}" for m in _CLOSED_CLIENT_ROUTE.finditer(text)})


def _scan_blob(name: str, data: bytes) -> list[str]:
    if Path(name).suffix.lower() in _BINARY_SUFFIXES or b"\x00" in data[:8192]:
        return []
    return closed_route_strings_in_text(data.decode("utf-8", errors="replace"))


def closed_route_strings_in_archive(path: Path) -> dict[str, list[str]]:
    """`{member: segments}` for every member of a wheel, sdist or npm tarball that has one."""
    hits: dict[str, list[str]] = {}
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir() and (found := _scan_blob(info.filename, zf.read(info))):
                    hits[info.filename] = found
        return hits
    with tarfile.open(path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            handle = tf.extractfile(member)
            if handle is not None and (found := _scan_blob(member.name, handle.read())):
                hits[member.name] = found
    return hits


def closed_route_strings_in_dir(path: Path) -> dict[str, list[str]]:
    """`{relative path: segments}` for every file under `path` (a built Studio bundle)."""
    hits: dict[str, list[str]] = {}
    for file in sorted(path.rglob("*")):
        if file.is_file() and (found := _scan_blob(file.name, file.read_bytes())):
            hits[file.relative_to(path).as_posix()] = found
    return hits


def scan_source_imports(
    root: Path = ROOT,
    roots: Iterable[tuple[str, tuple[str, ...]]] = OSS_SOURCE_ROOTS,
) -> list[dict[str, Any]]:
    """Every OSS source line that imports closed code (plan 2.4 check 5)."""
    hits: list[dict[str, Any]] = []
    for base, suffixes in roots:
        base_dir = root / base
        if not base_dir.is_dir():
            continue
        for path in sorted(base_dir.rglob("*")):
            if path.suffix not in suffixes or not path.is_file():
                continue
            if _SKIP_DIRS.intersection(path.relative_to(root).parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enterprise_imports_in_text(text, path.suffix):
                rel = path.relative_to(root).as_posix()
                hits.append({"file": rel, "line": lineno, "text": line})
    return hits


def closed_modules_status(
    root: Path = ROOT,
    closed: Iterable[ClosedModule] = CLOSED_MODULES,
    *,
    moved_through_step: int = MOVED_THROUGH_STEP,
) -> list[dict[str, Any]]:
    """For each closed module: is it still in the OSS tree, what of it remains, and is that
    what the migration step reached so far expects (`expected`: "moved" or "in_oss")."""
    rows: list[dict[str, Any]] = []
    enterprise = root / "enterprise"
    for module in closed:
        target = root / module.path
        row: dict[str, Any] = {
            "path": module.path,
            "step": module.step,
            "kind": module.kind,
            "note": module.note,
            "expected": "moved" if module.step <= moved_through_step else "in_oss",
        }
        if module.dest:
            row["dest"] = f"enterprise/{module.dest}"
            # Only meaningful in a checkout that holds the closed edition next to OSS.
            row["dest_present"] = (
                (enterprise / module.dest).exists() if enterprise.is_dir() else None
            )
        if module.kind == "split":
            text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
            remaining = [s for s in module.symbols if s in text]
            row["symbols_remaining"] = remaining
            row["present"] = bool(remaining)
        elif target.is_dir():
            files = sorted(
                p.relative_to(target).as_posix()
                for p in target.rglob("*")
                if p.is_file() and not _SKIP_DIRS.intersection(p.relative_to(target).parts)
            )
            row["present"] = True
            row["files"] = files
        else:
            row["present"] = target.exists()
        row["status"] = "in_oss" if row["present"] else "moved"
        rows.append(row)
    return rows


def build_boundary_report(
    artifacts: dict[str, list[str]],
    source_imports: list[dict[str, Any]],
    closed_status: list[dict[str, Any]],
    artifact_source: str,
    route_strings: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """The edition-boundary report (JSON-serialisable).

    `route_strings` maps each scanned artifact (wheel, sdist, npm tarball, Studio bundle) to its
    files that contain a closed-edition route string (see `CLOSED_CLIENT_ROUTE_SEGMENTS`).
    """
    route_strings = route_strings or {}
    artifact_rows: list[dict[str, Any]] = [
        {
            "name": name,
            "members": len(members),
            "enterprise_members": enterprise_members(members),
            "closed_modules_shipped": closed_modules_in_members(
                members, sdist=name.endswith((".tar.gz", ".tgz"))
            ),
        }
        for name, members in sorted(artifacts.items())
    ]
    present = [r for r in closed_status if r["present"]]
    by_step: dict[str, int] = {}
    for row in present:
        by_step[str(row["step"])] = by_step.get(str(row["step"]), 0) + 1
    moved = [r for r in closed_status if not r["present"]]
    moved_by_step: dict[str, int] = {}
    for row in moved:
        moved_by_step[str(row["step"])] = moved_by_step.get(str(row["step"]), 0) + 1
    # A module whose step is done but that is still in OSS, or one moved ahead of its step.
    off_plan = [
        r["path"]
        for r in closed_status
        if r.get("expected") is not None
        and r["expected"] != ("in_oss" if r["present"] else "moved")
    ]
    summary = {
        "artifacts": len(artifact_rows),
        "enterprise_members": sum(len(r["enterprise_members"]) for r in artifact_rows),
        "enterprise_imports_in_oss_sources": len(source_imports),
        "closed_modules_listed": len(closed_status),
        "closed_modules_still_in_oss": len(present),
        "closed_modules_still_in_oss_by_step": dict(sorted(by_step.items())),
        "closed_modules_moved": len(moved),
        "closed_modules_moved_by_step": dict(sorted(moved_by_step.items())),
        "moved_through_step": MOVED_THROUGH_STEP,
        "closed_modules_off_plan": off_plan,
        "closed_modules_shipped_in_artifacts": len(
            {p for r in artifact_rows for p in r["closed_modules_shipped"]}
        ),
        "route_string_artifacts_scanned": len(route_strings),
        "closed_client_route_strings_in_artifacts": sum(
            len(files) for files in route_strings.values()
        ),
    }
    return {
        "schema_version": 1,
        "check_id": "edition-boundary",
        "mode": "enforced",
        "enforced_from": BOUNDARY_ENFORCED_FROM,
        "plan": "edition-boundary policy maintained in CLOSED_MODULES in this script",
        "artifact_source": artifact_source,
        "summary": summary,
        "artifacts": artifact_rows,
        "source_imports": source_imports,
        "closed_modules": closed_status,
        "closed_client_route_segments": [f"/{s}" for s in CLOSED_CLIENT_ROUTE_SEGMENTS],
        "closed_client_route_strings": {
            name: dict(sorted(files.items())) for name, files in sorted(route_strings.items())
        },
    }


def boundary_summary_line(report: dict[str, Any]) -> str:
    s = report["summary"]
    return (
        f"edition-boundary ({report['mode']}): {s['artifacts']} artifacts, "
        f"{s['enterprise_members']} enterprise members, "
        f"{s['enterprise_imports_in_oss_sources']} enterprise imports in OSS sources, "
        f"{s['closed_modules_still_in_oss']}/{s['closed_modules_listed']} closed modules "
        f"still in OSS (by step {s['closed_modules_still_in_oss_by_step']}), "
        f"{s.get('closed_modules_moved', 0)} moved "
        f"(by step {s.get('closed_modules_moved_by_step', {})}), "
        f"{len(s.get('closed_modules_off_plan', []))} off plan, "
        f"{s['closed_modules_shipped_in_artifacts']} shipped in artifacts, "
        f"{s.get('closed_client_route_strings_in_artifacts', 0)} files with closed client route "
        f"strings in {s.get('route_string_artifacts_scanned', 0)} scanned artifacts"
    )


def _boundary_artifacts(work: Path) -> tuple[list[Path], str]:
    """Reuse the gate's `uv build --all-packages` output, or build it once for this step."""
    reused = sorted([*(work / "dist").glob("*.whl"), *(work / "dist").glob("*.tar.gz")])
    if reused:
        return reused, "reused the gate build (uv build --all-packages)"
    dist = work / "boundary-dist"
    proc, _ = sh(["uv", "build", "--all-packages", "--out-dir", str(dist)], timeout=600)
    built = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if proc.returncode != 0:
        return built, f"uv build --all-packages failed (exit {proc.returncode})"
    return built, "built for this step (uv build --all-packages)"


def pack_ts_client(work: Path) -> tuple[Path | None, str]:
    """`npm pack` the open-source TypeScript client into `work/npm-pack` (its prepack builds it)."""
    dest = work / "npm-pack"
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.tgz"):
        old.unlink()
    proc, _ = sh(
        ["npm", "pack", "--pack-destination", str(dest)], cwd=ROOT / "clients" / "typescript"
    )
    packed = sorted(dest.glob("*.tgz"))
    if proc.returncode != 0 or len(packed) != 1:
        return None, f"npm pack failed (exit {proc.returncode}, {len(packed)} tarballs)"
    return packed[0], "npm pack clients/typescript"


def check_edition_boundary(
    work: Path,
    out_file: Path = BOUNDARY_FILE,
    *,
    npm_tarball: Path | None = None,
    studio_dist: Path | None = None,
    require_js_artifacts: bool = False,
) -> Receipt:
    """Fail when closed-edition code would cross into the open-source edition (P28 step 9).

    The OSS wheel and sdist must hold no enterprise member or listed closed module, no OSS
    source may import the closed edition, every listed closed module must have left the OSS
    tree, and no closed module may be off plan. No OSS artifact (wheel and sdist members, the
    npm tarball of the TypeScript client, the built Studio bundle) may contain a closed-edition
    client route string (PKG-03). With `require_js_artifacts` (the gate run), a missing npm
    tarball or Studio bundle fails the step instead of being skipped.
    """
    start = time.monotonic()
    paths, source = _boundary_artifacts(work)
    artifacts = {p.name: archive_members(p) for p in paths}
    route_strings: dict[str, dict[str, list[str]]] = {
        p.name: closed_route_strings_in_archive(p) for p in paths
    }
    js_missing: list[str] = []
    if npm_tarball is not None and npm_tarball.is_file():
        route_strings[npm_tarball.name] = closed_route_strings_in_archive(npm_tarball)
    elif require_js_artifacts:
        js_missing.append("npm tarball")
    studio_files = (
        [f for f in studio_dist.rglob("*") if f.is_file()]
        if studio_dist is not None and studio_dist.is_dir()
        else []
    )
    if studio_dist is not None and any(f.suffix == ".js" for f in studio_files):
        route_strings["studio-web/dist"] = closed_route_strings_in_dir(studio_dist)
    elif require_js_artifacts:
        js_missing.append("Studio bundle (studio-web/dist with .js files)")
    report = build_boundary_report(
        artifacts, scan_source_imports(), closed_modules_status(), source, route_strings
    )
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    line = boundary_summary_line(report)
    print(line, flush=True)
    s = report["summary"]
    leaks = [
        f"{name}:{file} {segments}"
        for name, files in report["closed_client_route_strings"].items()
        for file, segments in files.items()
    ]
    inv: tuple[Invariant, ...] = (
        Invariant("OSS wheel and sdist listed", any(p.suffix == ".whl" for p in paths), source),
        Invariant("boundary report written", out_file.is_file(), str(out_file.name)),
        Invariant(
            "no enterprise member in OSS artifacts",
            s["enterprise_members"] == 0,
            str(s["enterprise_members"]),
        ),
        Invariant(
            "no closed module shipped in OSS artifacts",
            s["closed_modules_shipped_in_artifacts"] == 0,
            str(s["closed_modules_shipped_in_artifacts"]),
        ),
        Invariant(
            "no OSS source imports the closed edition",
            s["enterprise_imports_in_oss_sources"] == 0,
            str(s["enterprise_imports_in_oss_sources"]),
        ),
        Invariant(
            "every listed closed module has left the OSS tree",
            s["closed_modules_still_in_oss"] == 0,
            str(s["closed_modules_still_in_oss_by_step"]),
        ),
        Invariant(
            "no closed module off plan",
            not s["closed_modules_off_plan"],
            str(s["closed_modules_off_plan"]),
        ),
        Invariant(
            "no closed client route string in OSS artifacts",
            not leaks,
            "; ".join(leaks[:10]) or f"{s['route_string_artifacts_scanned']} artifacts scanned",
        ),
    )
    if require_js_artifacts:
        inv += (
            Invariant(
                "npm tarball and Studio bundle scanned",
                not js_missing,
                ", ".join(js_missing) or "npm tarball, studio-web/dist",
            ),
        )
    ok = all(i.held for i in inv)
    return Receipt(
        "edition-boundary",
        "passed" if ok else "failed",
        "list uv build --all-packages wheel/sdist members; scan OSS sources for enterprise "
        "imports; check the closed-module list; scan wheel/sdist members, the npm tarball and "
        "studio-web/dist for closed-edition client route strings",
        0 if ok else 1,
        time.monotonic() - start,
        inv,
        {"mode": "enforced", "enforced_from": BOUNDARY_ENFORCED_FROM, **report["summary"]},
        reason="" if ok else "; ".join(f"{i.name}: {i.detail}" for i in inv if not i.held),
    )


def check_edition_boundary_with_js(work: Path) -> Receipt:
    """The gate's edition-boundary step: Python artifacts plus the npm tarball and Studio bundle
    (runs after the TypeScript client and Studio steps have built them)."""
    tarball, _ = pack_ts_client(work)
    return check_edition_boundary(
        work,
        npm_tarball=tarball,
        studio_dist=ROOT / "studio-web" / "dist",
        require_js_artifacts=True,
    )


REQUIRED = [
    "python-tests",
    "python-lint",
    "python-types",
    "keyless-examples",
    "reference-scenario",
    "paired-context-selection",
    "python-external-consumer",
    "agents-standalone-consumer",
    "connectors-standalone-consumer",
    *(check.check_id for check in PACKAGE_CHECKS),
    "service-readiness-and-load",
    "ts-external-consumer",
    "ts-client",
    "studio-web-unit-and-build",
    "studio-route-assets",
    "edition-boundary",
    "studio-browser-e2e",
]

EXCLUDED = [
    Receipt(
        "live-model-quality-and-billing",
        "unmeasured",
        "",
        None,
        0.0,
        reason="needs separately authorized real-provider access; fakes prove integration only",
    ),
    Receipt(
        "cache-only-savings",
        "unmeasured",
        "",
        None,
        0.0,
        reason="needs provider-reported cache counters; fake fixtures cannot produce cache hits",
    ),
    Receipt(
        "competitor-comparison",
        "unmeasured",
        "",
        None,
        0.0,
        reason=(
            "competitor comparison in experiments/benchmark measures control-plane scenarios "
            "with fake models only (S1-S3); no quality/latency comparison"
        ),
    ),
    Receipt(
        "production-scale-and-customer-demand",
        "unmeasured",
        "",
        None,
        0.0,
        reason="single-machine local checks only; no pilot sessions have taken place",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    receipts: list[Receipt] = []
    with tempfile.TemporaryDirectory(prefix="nexura-gate-") as tmp:
        steps: list[Callable[[], Receipt | list[Receipt]]] = [
            check_python_tests,
            check_ruff,
            check_mypy,
            check_examples,
            check_reference_scenario,
            check_paired_selection,
            lambda: check_external_consumers(Path(tmp)),
            lambda: check_agents_standalone(Path(tmp)),
            # Reuses the same build: runtime alone, then runtime plus nexura-connectors.
            lambda: check_connectors_standalone(Path(tmp)),
            # Same build: each split package (stage 4b) without, then with, its wheel.
            *(
                (lambda check=check: check_package_standalone(Path(tmp), check))
                for check in PACKAGE_CHECKS
            ),
            check_ts_client,
            check_studio_unit,
            check_studio_assets,
            # Reuses the wheel/sdist built by the external-consumer step, packs the TypeScript
            # client and reads the Studio bundle the two steps above built.
            lambda: check_edition_boundary_with_js(Path(tmp)),
        ]
        if not args.skip_browser:
            steps.append(check_browser)
        for step in steps:
            result = step()
            for r in result if isinstance(result, list) else [result]:
                print(f"[{r.status:>8}] {r.check_id} ({r.duration_s:.1f}s)", flush=True)
                receipts.append(r)

    if args.skip_browser:
        receipts.append(
            Receipt(
                "studio-browser-e2e",
                "skipped",
                "npx playwright test",
                None,
                0.0,
                reason="--skip-browser",
            )
        )
    receipts += EXCLUDED
    write_receipts(OUT / "receipts.json", receipts)
    summary = summarize(receipts, REQUIRED, environment_profile())
    excluded = "\n".join(f"- **{r.check_id}**: {r.reason}" for r in EXCLUDED)
    report_only = ""
    if BOUNDARY_FILE.is_file():
        boundary = json.loads(BOUNDARY_FILE.read_text(encoding="utf-8"))
        report_only = (
            "\n## Edition boundary\n\n"
            f"- **edition-boundary** (required since {BOUNDARY_ENFORCED_FROM}): "
            f"{boundary_summary_line(boundary)}; details in `{BOUNDARY_FILE.name}`.\n"
        )
    (OUT / "READINESS.md").write_text(
        summary.to_markdown()
        + "\n## Explicitly excluded (unmeasured)\n\n"
        + excluded
        + "\n"
        + report_only,
        encoding="utf-8",
    )
    summary.assert_consistent()
    print(f"Ready: {summary.ready}. Receipts: {OUT / 'receipts.json'}")
    return 0 if summary.ready else 1


if __name__ == "__main__":
    sys.exit(main())
