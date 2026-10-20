"""Audit10 docs-drift repros. Each test asserts what the docs claim; a failure shows the doc is
stale or wrong versus the code. Read-only: no network, no docker."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _cli_passes_listen_host() -> bool:
    return "listen_host=args.host" in _read("src/nexura_runtime/cli/main.py")


def test_security_model_cli_row_not_stale() -> None:
    """SECURITY_MODEL 3.1 row 1 says `nexura-runtime serve` does not yet pass --host as
    listen_host."""
    assert _cli_passes_listen_host()  # code wires it
    text = _read("SECURITY_MODEL.md")
    assert "does not yet pass `--host`" not in text, (
        "3.1 status still says the CLI path is unchecked"
    )


def test_security_model_section4_cli_bullet_not_stale() -> None:
    """SECURITY_MODEL section 4 still lists the CLI bind guard as not enforced."""
    assert _cli_passes_listen_host()
    sec4 = _read("SECURITY_MODEL.md").split("## 4.")[1].split("## Review hardening")[0]
    assert "`nexura-runtime serve` does not enforce the bind guard" not in sec4


def test_security_model_not_self_contradictory_on_cli_guard() -> None:
    """The same file claims both that the CLI is unguarded and that it exits 2 when guarded."""
    text = _read("SECURITY_MODEL.md")
    unguarded = "does not enforce the bind guard" in text or "does not yet pass `--host`" in text
    guarded = "`nexura-runtime serve` passes `--host`" in text
    assert not (unguarded and guarded), "SECURITY_MODEL both denies and asserts the CLI guard"


def test_security_model_studio_web_token_claim_stale() -> None:
    """SECURITY_MODEL section 4: 'studio-web does not yet send the Studio token'."""
    client = _read("clients/typescript/src/client.ts")
    assert "Authorization = `Bearer ${this.apiKey}`" in client
    assert "apiKey: this._authToken" in _read("studio-web/src/api/client.ts")
    assert "does not yet send the Studio token" not in _read("SECURITY_MODEL.md")


def test_studio_doc_cli_limit_paragraph_not_stale() -> None:
    """docs/studio-and-service.md says nexura-runtime serve still has to pass --host, two bullets
    after saying it refuses to start."""
    doc = _read("docs/studio-and-service.md")
    assert _cli_passes_listen_host()
    assert "`nexura-runtime serve` still has to pass its `--host`" not in doc


def test_studio_doc_composite_steps_claim_stale() -> None:
    """docs says Studio's graph builder cannot build sub-workflow nodes (P2 added them)."""
    assert (ROOT / "studio-web/src/features/builder/CompositeInspector.tsx").exists()
    doc = _read("docs/studio-and-service.md")
    assert "graph builder cannot build sub-workflow nodes" not in doc


def test_studio_doc_button_label_matches_ui() -> None:
    """docs/studio-and-service.md names the button as the UI does."""
    en = json.loads(_read("studio-web/src/i18n/en.json"))

    def find(d: object, key: str) -> str | None:
        if isinstance(d, dict):
            if key in d and isinstance(d[key], str):
                return d[key]
            for v in d.values():
                r = find(v, key)
                if r:
                    return r
        return None

    label = find(en, "describeCreate")
    assert label is not None
    assert f"**{label}**" in _read("docs/studio-and-service.md"), f"UI label is {label!r}"


def test_studio_doc_section_list_covers_new_pages() -> None:
    """'Today Studio has these sections' omits Templates and Run Traces, both in the sidebar."""
    sidebar = _read("studio-web/src/app/Sidebar.tsx")
    assert "nav.templates" in sidebar and "nav.traces" in sidebar
    para = next(
        ln
        for ln in _read("docs/studio-and-service.md").splitlines()
        if ln.startswith("Today Studio has these sections")
    ).lower()
    assert "templates" in para and "trace" in para, "section list is stale"


def test_run_trace_endpoint_is_documented() -> None:
    """GET /api/v1/runs/{id}/trace (P3) appears in no doc."""
    docs = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "docs").glob("*.md"))
    docs += _read("README.md")
    assert re.search(r"runs/\{[a-z_]+\}/trace", docs), "no doc mentions the trace endpoint"


def test_security_model_default_stdio_allowlist_matches_mcpserver() -> None:
    """MCPServer.stdio's default also admits uvx (the transport does not)."""
    from nexura_agents.mcp.toolset import MCPServer
    from nexura_agents.mcp.transports import McpCommandNotAllowedError, McpStdioTransport

    MCPServer.stdio("uvx", "some-pkg")  # accepted by default
    assert "uvx" not in McpStdioTransport.DEFAULT_ALLOWED_COMMANDS
    try:
        MCPServer.stdio("bash", "-c", "true")
    except McpCommandNotAllowedError:
        pass
    else:
        raise AssertionError("bash must stay refused")
    assert "`MCPServer.stdio` adds `uvx` to that default" in _read("SECURITY_MODEL.md")


def test_studio_web_sends_connection_token_as_bearer() -> None:
    """SECURITY_MODEL states the studio-web token behaviour; check its source."""
    ctx = _read("studio-web/src/api/context.tsx")
    assert "manager.setAuthToken(token)" in ctx
    # The field label lives in the i18n dictionaries since the Hindi pass (2026-10-20).
    assert "t('ui.serviceAuthTokenOptional')" in _read("studio-web/src/app/ConnectionIndicator.tsx")
    assert "Service Auth Token" in _read("studio-web/src/i18n/en.json")
    model = _read("SECURITY_MODEL.md")
    assert "test_studio_web_sends_connection_token_as_bearer" in model


def test_security_model_lists_stdio_allowlist() -> None:
    from nexura_agents.mcp.transports import McpStdioTransport

    assert {"python", "python3", "uv", "node", "npx"} == McpStdioTransport.DEFAULT_ALLOWED_COMMANDS
    assert "`python`, `python3`, `uv`, `node`, `npx`; `MCPServer.stdio` adds `uvx`" in _read(
        "SECURITY_MODEL.md"
    )
