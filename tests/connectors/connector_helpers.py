"""Shared helpers for connector tests.

Everything here is offline: HTTP goes through ``httpx.MockTransport`` and database/mail
clients are in-memory fakes. No test in this directory touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from nexura_agents.contracts.context import CredentialReference, ExecutionContext
from nexura_agents.contracts.result import ModuleResult

TOKEN = "tok_SuperSecretValue_12345678"


class FakeResolver:
    """Fake CredentialResolver (mock secrets store) mapping credential ids to values."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets

    def resolve(self, ref: CredentialReference | str) -> str | None:
        """Return the stored value for the credential id, or None."""
        key = ref if isinstance(ref, str) else ref.credential_id
        return self.secrets.get(key)


def make_context(
    secrets: dict[str, str] | None = None, *, approval_granted: bool = False
) -> ExecutionContext:
    """Execution context whose resolver knows ``secrets`` (default: id 'cred' -> TOKEN).

    ``approval_granted`` is the operator path: it stands in for the engine's human approval.
    """
    resolver = FakeResolver({"cred": TOKEN} if secrets is None else secrets)
    return ExecutionContext(
        run_id="run-1",
        node_id="node-1",
        credential_resolver=resolver,
        approval_granted=approval_granted,
    )


@dataclass
class Recorder:
    """MockTransport wrapper that records requests and serves queued handlers."""

    handler: Callable[[httpx.Request], httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self._serve)

    def _serve(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def last(self) -> httpx.Request:
        """The most recent request."""
        return self.requests[-1]

    def body(self, index: int = -1) -> Any:
        """JSON body of a recorded request."""
        return json.loads(self.requests[index].content)


def json_response(payload: Any, status: int = 200, **headers: str) -> httpx.Response:
    """Build a JSON response."""
    return httpx.Response(status, json=payload, headers=headers)


class Sleeps:
    """No-op async sleep that records requested delays (keeps retry tests instant)."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record the delay instead of waiting."""
        self.delays.append(seconds)


async def invoke(
    module: Any,
    operation: str,
    params: dict[str, Any] | None = None,
    *,
    cfg: dict[str, Any] | None = None,
    approved: bool | None = None,
    secrets: dict[str, str] | None = None,
) -> ModuleResult:
    """Run ``module.execute`` with the default credential reference 'cred'."""
    config = {"credential_ref": "cred", **(cfg or {})}
    inputs: dict[str, Any] = {"operation": operation, "params": params or {}}
    if approved is not None:
        inputs["approved"] = approved
    ctx = make_context(secrets, approval_granted=approved is True)
    return await module.execute(ctx, inputs, config)


@dataclass
class Spec:
    """Describes one HTTP connector for the shared negative suite."""

    factory: Callable[[httpx.AsyncBaseTransport, Sleeps], Any]
    read_op: str
    read_params: dict[str, Any]
    read_body: Any
    write_op: str
    write_params: dict[str, Any]
    write_body: Any
    cfg: dict[str, Any] = field(default_factory=dict)
    #: When True the read response is not JSON (e.g. S3 XML) and given as raw bytes
    read_raw: bytes | None = None
    #: Secret value stored under credential id "cred" (S3 needs "id:secret")
    secret: str = TOKEN


CASES = [
    "missing_credential",
    "literal_token_as_credential",
    "unresolvable_credential",
    "write_needs_approval",
    "write_needs_approval_true_only",
    "ssrf_metadata_ip",
    "ssrf_loopback",
    "ssrf_http_scheme",
    "oversized_content_length",
    "oversized_streamed",
    "unauthorized_401",
    "forbidden_403",
    "retry_429_then_ok",
    "retry_5xx_then_ok",
    "exhausted_429",
    "exhausted_5xx",
    "write_5xx_not_retried",
    "write_timeout_indeterminate",
    "read_timeout_retried_then_fails",
    "redirect_refused",
    "token_not_in_error",
    "unknown_operation",
    "unknown_param_rejected",
]


class Negatives:
    """Shared negative-path assertions every HTTP connector must satisfy."""

    async def check(self, case: str, s: Spec) -> None:
        """Run one named case against the connector described by ``s``."""
        await getattr(self, f"case_{case}")(s)

    def _read_response(self, s: Spec) -> httpx.Response:
        if s.read_raw is not None:
            return httpx.Response(200, content=s.read_raw)
        return json_response(s.read_body)

    def _mod(self, s: Spec, handler: Callable[[httpx.Request], httpx.Response]) -> Any:
        rec = Recorder(handler)
        sleeps = Sleeps()
        return s.factory(rec.transport, sleeps), rec, sleeps

    async def _read(self, s: Spec, module: Any, **kw: Any) -> ModuleResult:
        kw.setdefault("secrets", {"cred": s.secret})
        return await invoke(module, s.read_op, s.read_params, cfg=s.cfg | kw.pop("cfg", {}), **kw)

    async def _write(self, s: Spec, module: Any, **kw: Any) -> ModuleResult:
        kw.setdefault("approved", True)
        kw.setdefault("secrets", {"cred": s.secret})
        return await invoke(module, s.write_op, s.write_params, cfg=s.cfg | kw.pop("cfg", {}), **kw)

    async def case_missing_credential(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        res = await module.execute(
            make_context({"cred": s.secret}),
            {"operation": s.read_op, "params": s.read_params},
            dict(s.cfg),
        )
        assert res.status == "failure" and res.error_code == "MISSING_CREDENTIAL"
        assert rec.requests == []

    async def case_literal_token_as_credential(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        for literal in ("ghp_" + "a" * 30, "xoxb-123-456", "https://x.example/hook"):
            res = await module.execute(
                make_context({literal: "v"}),
                {"operation": s.read_op, "params": s.read_params},
                {**s.cfg, "credential_ref": literal},
            )
            assert res.error_code == "MISSING_CREDENTIAL", literal
            assert literal not in (res.error_message or "")
        assert rec.requests == []

    async def case_unresolvable_credential(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        res = await self._read(s, module, secrets={})
        assert res.error_code == "MISSING_CREDENTIAL" and rec.requests == []

    async def case_write_needs_approval(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: json_response(s.write_body))
        for approved in (None, False):
            res = await self._write(s, module, approved=approved)
            assert res.status == "failure" and res.error_code == "APPROVAL_REQUIRED"
        assert rec.requests == [], "a write reached the network without approval"

    async def case_write_needs_approval_true_only(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: json_response(s.write_body))
        res = await invoke(
            module,
            s.write_op,
            s.write_params,
            cfg={"credential_ref": "cred", **s.cfg},
            secrets={"cred": s.secret},
        )
        assert res.error_code == "APPROVAL_REQUIRED"
        inputs = {"operation": s.write_op, "params": s.write_params, "approved": True}
        cfg = {"credential_ref": "cred", **s.cfg}
        # the input flag alone grants nothing
        res = await module.execute(make_context({"cred": s.secret}), inputs, cfg)
        assert res.error_code == "APPROVAL_REQUIRED" and rec.requests == []
        res = await module.execute(
            make_context({"cred": s.secret}, approval_granted=True), inputs, cfg
        )
        assert res.status == "success", res.error_message
        assert len(rec.requests) == 1

    async def case_ssrf_metadata_ip(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        for base in ("https://169.254.169.254", "https://[fd00:ec2::254]", "https://169.254.1.1"):
            res = await self._read(s, module, cfg={"base_url": base})
            assert res.error_code == "SSRF_BLOCKED", base
        # an allow-list entry must never unlock the metadata address
        res = await self._read(
            s,
            module,
            cfg={"base_url": "https://169.254.169.254", "allowed_hosts": ["169.254.169.254"]},
        )
        assert res.error_code == "SSRF_BLOCKED"
        assert rec.requests == []

    async def case_ssrf_loopback(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        for base in (
            "https://127.0.0.1",
            "https://10.0.0.5",
            "https://192.168.1.1",
            "https://[::1]",
        ):
            res = await self._read(s, module, cfg={"base_url": base})
            assert res.error_code == "SSRF_BLOCKED", base
        assert rec.requests == []

    async def case_ssrf_http_scheme(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        res = await self._read(s, module, cfg={"base_url": "http://api.example.com"})
        assert res.error_code == "SSRF_BLOCKED" and rec.requests == []

    async def case_oversized_content_length(self, s: Spec) -> None:
        module, _, _ = self._mod(s, lambda r: httpx.Response(200, content=b"x" * 5000))
        res = await self._read(s, module, cfg={"max_response_bytes": 1000})
        assert res.error_code == "RESPONSE_TOO_LARGE"

    async def case_oversized_streamed(self, s: Spec) -> None:
        async def chunks() -> Any:
            for _ in range(50):
                yield b"y" * 100

        module, _, _ = self._mod(s, lambda r: httpx.Response(200, content=chunks()))
        res = await self._read(s, module, cfg={"max_response_bytes": 1000})
        assert res.error_code == "RESPONSE_TOO_LARGE"

    async def case_unauthorized_401(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: httpx.Response(401, text=f"bad token {TOKEN}"))
        res = await self._read(s, module)
        assert res.error_code == "AUTH_FAILED" and len(rec.requests) == 1
        assert TOKEN not in (res.error_message or "")

    async def case_forbidden_403(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: httpx.Response(403, text="nope"))
        res = await self._read(s, module)
        assert res.error_code == "AUTH_FAILED" and len(rec.requests) == 1

    async def case_retry_429_then_ok(self, s: Spec) -> None:
        state = {"n": 0}

        def handler(r: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "2"})
            return self._read_response(s)

        module, rec, sleeps = self._mod(s, handler)
        res = await self._read(s, module)
        assert res.status == "success", res.error_message
        assert len(rec.requests) == 2
        if module.build_http({}, "").rate_profile:
            assert 2.0 <= sum(sleeps.delays) <= 2.2
        else:
            assert sleeps.delays == [2.0]

    async def case_retry_5xx_then_ok(self, s: Spec) -> None:
        state = {"n": 0}

        def handler(r: httpx.Request) -> httpx.Response:
            state["n"] += 1
            return httpx.Response(503) if state["n"] <= 2 else self._read_response(s)

        module, rec, _ = self._mod(s, handler)
        res = await self._read(s, module)
        assert res.status == "success", res.error_message
        assert len(rec.requests) == 3

    async def case_exhausted_429(self, s: Spec) -> None:
        module, rec, sleeps = self._mod(
            s, lambda r: httpx.Response(429, headers={"retry-after": "99"})
        )
        res = await self._read(s, module, cfg={"max_retries": 2})
        assert res.error_code == "RATE_LIMITED" and res.metadata["retryable"] is True
        if module.build_http({}, "").rate_profile:
            assert len(rec.requests) == 1 and not sleeps.delays
            assert res.metadata["retry_after_seconds"] >= 99
        else:
            assert len(rec.requests) == 3
        assert all(d <= 5.0 for d in sleeps.delays), "Retry-After must be capped"

    async def case_exhausted_5xx(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: httpx.Response(500, text=f"boom {TOKEN}"))
        res = await self._read(s, module, cfg={"max_retries": 1})
        assert res.error_code == "UPSTREAM_ERROR" and len(rec.requests) == 2
        assert TOKEN not in (res.error_message or "")

    async def case_write_5xx_not_retried(self, s: Spec) -> None:
        module, rec, sleeps = self._mod(s, lambda r: httpx.Response(502, text="bad gateway"))
        res = await self._write(s, module)
        if module.build_http({}, "").indeterminate_status:
            assert res.error_code == "INDETERMINATE_EFFECT" and res.status == "indeterminate"
        else:
            assert res.error_code == "UPSTREAM_ERROR"
        assert len(rec.requests) == 1 and sleeps.delays == []

    async def case_write_timeout_indeterminate(self, s: Spec) -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=r)

        module, rec, _ = self._mod(s, handler)
        res = await self._write(s, module)
        assert res.status == "indeterminate" and len(rec.requests) == 1

    async def case_read_timeout_retried_then_fails(self, s: Spec) -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=r)

        module, rec, _ = self._mod(s, handler)
        res = await self._read(s, module, cfg={"max_retries": 2})
        assert res.error_code == "TIMEOUT" and len(rec.requests) == 3

    async def case_redirect_refused(self, s: Spec) -> None:
        module, rec, _ = self._mod(
            s, lambda r: httpx.Response(302, headers={"location": "https://127.0.0.1/steal"})
        )
        res = await self._read(s, module)
        assert res.status == "failure" and len(rec.requests) == 1
        assert not any(r.url.host == "127.0.0.1" for r in rec.requests)

    async def case_token_not_in_error(self, s: Spec) -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                text=f"invalid: Authorization: Bearer {TOKEN} / {r.headers.get('authorization')}",
            )

        module, _, _ = self._mod(s, handler)
        res = await self._read(s, module)
        assert res.status == "failure"
        text = f"{res.error_message} {res.metadata}"
        assert TOKEN not in text
        assert TOKEN[4:] not in text

    async def case_unknown_operation(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        from nexura_agents.contracts.errors import SchemaValidationError

        with pytest.raises(SchemaValidationError):
            await module.execute(
                make_context({"cred": s.secret}), {"operation": "drop_everything"}, dict(s.cfg)
            )
        assert rec.requests == []

    async def case_unknown_param_rejected(self, s: Spec) -> None:
        module, rec, _ = self._mod(s, lambda r: self._read_response(s))
        res = await self._read(s, module)  # baseline works
        assert res.status == "success", res.error_message
        n = len(rec.requests)
        res = await invoke(
            module,
            s.read_op,
            {**s.read_params, "surprise": 1},
            cfg={**s.cfg},
        )
        assert res.error_code == "INVALID_PARAMS" and len(rec.requests) == n
