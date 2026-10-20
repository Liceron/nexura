# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Redact credentials before they reach logs, results, models or shareable files.

* :func:`redact_headers`: credential-bearing HTTP headers.
* :func:`redact_secret_text`: credential-looking substrings of free text (labelled pairs in
  JSON / Python-repr / ``key=value`` form, credential query parameters, URL user-info and
  well-known token formats). This is the single implementation of the product's free-text
  secret redaction: ``nexura_runtime.observability.run_diagnostics`` re-exports it for run
  events, exports and diagnostics, and repro bundles (``nexura_agents.repro``) apply it to
  every string they store.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "api-subscription-key",
    }
)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``headers`` with credential-like values replaced by ``[REDACTED]``."""
    safe: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in SENSITIVE_HEADERS or "token" in lowered or "secret" in lowered:
            safe[key] = "[REDACTED]"
        else:
            safe[key] = value
    return safe


_SECRET_NAME = (
    r"api[_\-]?key|x-api-key|password|passwd|passphrase|pwd|secret|client[_\-]?secret"
    r"|(?:access|refresh|id|auth|session|bearer)[_\-]?token|token|cookie|set-cookie"
    r"|proxy-authorization|authorization|private[_\-]?key"
)
# Query parameters that carry a credential on their own, including names too generic for the
# free-text ``name=value`` rule (Gemini ``?key=AIza...``, signed-URL signatures).
_SECRET_QUERY_PARAM = (
    r"key|api[_\-]?key|apikey|token|access[_\-]?token|refresh[_\-]?token|id[_\-]?token"
    r"|auth[_\-]?token|auth|client[_\-]?secret|secret|password|passwd|pwd|sig|signature"
    r"|x-amz-signature|x-amz-security-token|x-amz-credential|x-goog-signature"
    r"|x-goog-credential"
)
_VALUE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private key blocks (complete, or truncated without the END line).
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|$)"
        ),
        "[REDACTED]",
    ),
    # Authorization-style header values: Bearer/Basic/Token/Digest/... credentials.
    (
        re.compile(r"(?i)\b(Bearer|Basic|Digest|Token)\s+[A-Za-z0-9._~+/=\-]{4,}"),
        r"\1 [REDACTED]",
    ),
    (re.compile(r"(?i)(://[^/\s:@]*:)[^@\s/]+(@)"), r"\1[REDACTED]\2"),
    # Well-known token formats, independent of field names.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[REDACTED]"),
    (re.compile(r"\bey[A-Za-z0-9_\-]{5,}\.ey[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*"), "[REDACTED]"),
)
_TEXT_RULES = _VALUE_RULES + (
    # JSON / Python-repr quoted pairs: "password": "x", 'api_key': 'x'.
    (
        re.compile(
            r"""(?i)(["'](?:[\w.\-]*?(?:"""
            + _SECRET_NAME
            + r""")[\w.\-]*)["']\s*:\s*)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,{}\[\]]+)"""
        ),
        r'\1"[REDACTED]"',
    ),
    # Credential query parameters: ?key=..., &access_token=..., ?X-Amz-Signature=...
    (
        re.compile(r"(?i)([?&;](?:" + _SECRET_QUERY_PARAM + r")=)[^&#\s\"'<>]+"),
        r"\1[REDACTED]",
    ),
    # key=value / key: value (the value may be a quoted string or end at whitespace).
    (
        re.compile(
            r"(?i)\b((?:[\w\-]*?(?:" + _SECRET_NAME + r")[\w\-]*)\s*[=:]\s*)(?:Bearer\s+|Basic\s+)?"
            r"""(?:"[^"]*"|'[^']*'|[^\s,;&"']+)"""
        ),
        r"\1[REDACTED]",
    ),
)


def redact_secret_text(value: str) -> str:
    """Scrub credential-looking substrings (value patterns) from free text, without truncating."""
    for pattern, repl in _TEXT_RULES:
        value = pattern.sub(repl, value)
    return value


def redact_secret_values(value: str) -> str:
    """Redact detected secret values without treating a field name as evidence.

    Approval views use this narrower detector so ordinary action arguments such as
    ``password_policy`` or ``token_count`` remain visible to the reviewer.
    """
    for pattern, repl in _VALUE_RULES:
        value = pattern.sub(repl, value)
    return value
