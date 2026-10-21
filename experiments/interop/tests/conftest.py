"""Shared helpers: start interop servers as local subprocesses (stdio or 127.0.0.1 HTTP).

Everything is offline. HTTP servers bind to 127.0.0.1 on a free port and are stopped
after each test module.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / "servers"
LEGACY_PYTHON = ROOT / "legacy" / ".venv" / "bin" / "python"
LEGACY_SERVER = ROOT / "legacy" / "legacy_mcp_server.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_http(
    python: str | Path, script: Path, *extra: str
) -> tuple[subprocess.Popen[bytes], str]:
    port = free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [str(python), str(script), "http", str(port), *extra],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ""
            raise RuntimeError(f"{script.name} exited early: {err[-2000:]}")
        try:
            httpx.get(base + "/", timeout=0.5)
            return proc, base
        except httpx.TransportError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"{script.name} did not start on {base}")


def stop(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="module")
def nexura_mcp_http() -> Iterator[str]:
    proc, base = start_http(sys.executable, SERVERS / "nexura_mcp_server.py")
    yield base + "/mcp"
    stop(proc)


@pytest.fixture(scope="module")
def official_mcp_http() -> Iterator[str]:
    proc, base = start_http(sys.executable, SERVERS / "official_mcp_server.py")
    yield base + "/mcp"
    stop(proc)


@pytest.fixture(scope="module")
def legacy_mcp_http() -> Iterator[str]:
    if not LEGACY_PYTHON.exists():
        pytest.skip("legacy/ environment not synced (cd legacy && uv sync)")
    proc, base = start_http(LEGACY_PYTHON, LEGACY_SERVER)
    yield base + "/mcp"
    stop(proc)


@pytest.fixture(scope="session")
def signing_keys(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Fresh EC P-256 key pairs (PEM files) for the two A2A servers."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    out: dict[str, Path] = {}
    folder = tmp_path_factory.mktemp("keys")
    for name in ("nexura", "official", "stranger"):
        key = ec.generate_private_key(ec.SECP256R1())
        private = folder / f"{name}.pem"
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        public = folder / f"{name}.pub.pem"
        public.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        out[name] = private
        out[name + ".pub"] = public
    return out


@pytest.fixture(scope="module")
def nexura_a2a(signing_keys: dict[str, Path]) -> Iterator[str]:
    proc, base = start_http(
        sys.executable, SERVERS / "nexura_a2a_server.py", str(signing_keys["nexura"])
    )
    yield base
    stop(proc)


@pytest.fixture(scope="module")
def official_a2a(signing_keys: dict[str, Path]) -> Iterator[str]:
    proc, base = start_http(
        sys.executable, SERVERS / "official_a2a_server.py", str(signing_keys["official"])
    )
    yield base
    stop(proc)
