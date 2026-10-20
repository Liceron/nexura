"""web_fetch_tool: offline tests with httpx.MockTransport and patched DNS."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, ToolCall
from nexura_agents.network.policy import DestinationPolicy
from nexura_agents.web_tools import web_fetch_tool


def _dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> list[str]:
    seen: list[str] = []

    def fake(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        seen.append(host)
        if host not in mapping:
            raise socket.gaierror("no such host")
        ip = mapping[host]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return seen


def _run(tool: Any, url: str) -> dict[str, Any]:
    result = asyncio.run(tool.arun(url=url))
    assert isinstance(result, dict)
    return result


def _mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _page(
    body: bytes | str, ctype: str = "text/html; charset=utf-8", status: int = 200
) -> httpx.MockTransport:
    return _mock(lambda r: httpx.Response(status, content=body, headers={"content-type": ctype}))


def test_html_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<script>alert(1)</script><h1>Hello</h1><p>one   two\n three</p></body></html>"
    )
    out = _run(web_fetch_tool(transport=_page(html)), "https://example.com/")
    assert out["text"] == "T\nHello\none two three"  # the title is kept, scripts/styles are not
    assert out["truncated"] is False and out["status"] == 200


def test_plain_json_markdown_returned_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    for ctype in ("text/plain", "application/json", "text/markdown"):
        out = _run(web_fetch_tool(transport=_page('{"a": 1}', ctype)), "https://example.com/")
        assert out["text"] == '{"a": 1}'


def test_size_cap_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    out = _run(
        web_fetch_tool(max_bytes=100, transport=_page("x" * 5000, "text/plain")),
        "https://example.com/",
    )
    assert out["truncated"] is True and len(out["text"]) == 100


def test_content_type_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    out = _run(web_fetch_tool(transport=_page(b"\x89PNG", "image/png")), "https://example.com/")
    assert out["error"] == "refused" and "image/png" in out["message"]


def test_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    out = _run(web_fetch_tool(transport=_mock(slow)), "https://example.com/")
    assert out["error"] == "timeout"


def test_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    out = _run(web_fetch_tool(transport=_page("nope", "text/plain", 404)), "https://example.com/")
    assert out["error"] == "refused" and "404" in out["message"]


def test_redirect_followed_and_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"a.example": "93.184.216.34", "b.example": "93.184.216.35"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"location": "https://b.example/x"})
        return httpx.Response(200, text="done", headers={"content-type": "text/plain"})

    out = _run(web_fetch_tool(transport=_mock(handler)), "https://a.example/")
    assert out["text"] == "done" and out["url"] == "https://b.example/x"


@pytest.mark.parametrize("target", ["http://169.254.169.254/latest", "http://10.0.0.5/", "/ok"])
def test_redirect_to_private_ip_refused(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    _dns(monkeypatch, {"a.example": "93.184.216.34"})
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        if request.url.path == "/ok":
            return httpx.Response(200, text="fine", headers={"content-type": "text/plain"})
        return httpx.Response(302, headers={"location": target})

    out = _run(web_fetch_tool(transport=_mock(handler)), "https://a.example/")
    if target == "/ok":
        assert out["text"] == "fine"
    else:
        assert out["error"] == "refused"
        assert hits == ["a.example"]  # the private host was never contacted


def test_too_many_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"a.example": "93.184.216.34"})
    loop = _mock(lambda r: httpx.Response(302, headers={"location": "https://a.example/"}))
    out = _run(web_fetch_tool(transport=loop, max_redirects=3), "https://a.example/")
    assert out["error"] == "refused" and "redirects" in out["message"]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://0x7f000001/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://192.168.1.1/",
        "http://[fd00:ec2::254]/",
        "http://100.100.100.200/",
    ],
)
def test_private_destinations_refused(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _dns(monkeypatch, {"localhost": "127.0.0.1"})

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be contacted")

    out = _run(web_fetch_tool(transport=_mock(never)), url)
    assert out["error"] == "refused"


def test_dns_resolving_to_private_ip_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"evil.example": "10.1.2.3"})
    out = _run(web_fetch_tool(transport=_page("x", "text/plain")), "https://evil.example/")
    assert out["error"] == "refused" and "Private" in out["message"]


def test_dns_mixed_answers_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:7f00:1", port, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    out = _run(web_fetch_tool(transport=_page("x", "text/plain")), "https://mixed.example/")
    assert out["error"] == "refused"


def test_userinfo_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    tool = web_fetch_tool(transport=_page("x", "text/plain"))
    for url in ("https://user:pw@example.com/", "https://user@example.com/"):
        out = _run(tool, url)
        assert out["error"] == "refused" and "pw" not in out["message"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "gopher://x/", "x"])
def test_non_http_scheme_refused(url: str) -> None:
    out = _run(web_fetch_tool(transport=_page("x", "text/plain")), url)
    assert out["error"] == "refused"


def test_allow_list_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"intranet.test": "10.0.0.9"})
    transport = _page("internal", "text/plain")
    assert _run(web_fetch_tool(transport=transport), "http://intranet.test/")["error"] == "refused"
    allowed = DestinationPolicy(allowed_hosts=["intranet.test"])
    out = _run(web_fetch_tool(policy=allowed, transport=transport), "http://intranet.test/")
    assert out["text"] == "internal"
    # The allow-list does not unlock userinfo.
    bad = _run(web_fetch_tool(policy=allowed, transport=transport), "http://a@intranet.test/")
    assert bad["error"] == "refused"


def test_errors_do_not_leak_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-token-123", request=request)

    out = _run(web_fetch_tool(transport=_mock(boom)), "https://example.com/")
    assert out["error"] == "request_failed" and "secret" not in str(out)


def test_tool_properties_and_agent_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    tool = web_fetch_tool(name="fetch_page", transport=_page("<p>hi</p>"))
    assert tool.name == "fetch_page" and tool.idempotent is True
    model = FakeModel(
        responses=[
            [ToolCall(id="1", name="fetch_page", arguments={"url": "https://example.com"})],
            "ok",
        ]
    )
    result = Agent(name="W", instructions="Fetch.", model=model, tools=[tool]).run("go")
    assert result.output == "ok"
    assert '"text": "hi"' in [m for m in result.messages if m.role == "tool"][0].content


def test_bad_limits_rejected() -> None:
    with pytest.raises(ValueError):
        web_fetch_tool(max_bytes=0)


# Review findings (F47 adversarial review), each reproduced before the fix.


@pytest.mark.parametrize("host", ["a..b", "x" * 64 + ".com"])
def test_hosts_that_dns_cannot_encode_are_refused_not_raised(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    def fake(name: str, *args: Any, **kwargs: Any) -> Any:
        raise UnicodeError("encoding with 'idna' codec failed (label empty or too long)")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    out = _run(web_fetch_tool(transport=_page("x")), f"http://{host}/")
    assert out["error"] == "refused"


@pytest.mark.parametrize("url", ["http://93.184.216.34/a\x01b", "http://93.184.216.34/a b"])
def test_control_characters_in_the_url_are_refused(url: str) -> None:
    out = _run(web_fetch_tool(transport=_page("x")), url)
    assert out["error"] == "refused"


def test_an_undecodable_charset_falls_back_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    page = _page("hello", ctype="text/plain; charset=undefined")
    assert _run(web_fetch_tool(transport=page), "https://example.com/")["text"] == "hello"


def test_html_without_a_closing_head_keeps_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    html = "<html><head><title>T</title><body><p>Hello body</p></body></html>"
    out = _run(web_fetch_tool(transport=_page(html)), "https://example.com/")
    assert "Hello body" in out["text"]


def test_a_body_of_exactly_max_bytes_is_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    tool = web_fetch_tool(max_bytes=5, transport=_page("12345", ctype="text/plain"))
    out = _run(tool, "https://example.com/")
    assert (out["text"], out["truncated"]) == ("12345", False)
    tool = web_fetch_tool(max_bytes=5, transport=_page("123456", ctype="text/plain"))
    out = _run(tool, "https://example.com/")
    assert (out["text"], out["truncated"]) == ("12345", True)


def test_allowed_domains_restricts_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(
        monkeypatch,
        {
            "example.com": "93.184.216.34",
            "docs.example.com": "93.184.216.34",
            "other.example.org": "93.184.216.34",
            "badexample.com": "93.184.216.34",
            "inside.example.com": "10.0.0.5",
        },
    )
    tool = web_fetch_tool(allowed_domains=["example.com"], transport=_page("ok", "text/plain"))
    assert _run(tool, "https://example.com/")["text"] == "ok"
    assert _run(tool, "https://docs.example.com/")["text"] == "ok"
    assert _run(tool, "https://other.example.org/")["error"] == "refused"
    assert _run(tool, "https://badexample.com/")["error"] == "refused"  # not a subdomain
    # The address checks still apply inside an allowed domain.
    assert _run(tool, "https://inside.example.com/")["error"] == "refused"


def test_a_redirect_off_the_allowed_domains_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"example.com": "93.184.216.34", "evil.test": "93.184.216.34"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://evil.test/"})
        return httpx.Response(200, text="leak", headers={"content-type": "text/plain"})

    tool = web_fetch_tool(allowed_domains=["example.com"], transport=_mock(handler))
    assert _run(tool, "https://example.com/")["error"] == "refused"


def test_empty_allowed_domain_is_rejected_at_build_time() -> None:
    with pytest.raises(ValueError, match="allowed_domains"):
        web_fetch_tool(allowed_domains=[""])


def test_exported_from_the_package() -> None:
    import nexura_agents

    assert nexura_agents.web_fetch_tool is web_fetch_tool
