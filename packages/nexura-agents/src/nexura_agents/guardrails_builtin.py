# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Ready-made, opt-in fast pre-filters and PII helpers.

Everything here is a **heuristic pre-filter**: regular expressions plus checksums
(Verhoeff for Aadhaar, Luhn for payment cards), about 1 ms per KB of text. Use them as a
cheap first pass that catches obvious cases, in front of a model-based check (the
``nexura-guardrails`` package), not as the only check. They are not a security boundary.

Measured weaknesses (P39 probe, default threshold 0.5):

- ``no_prompt_injection`` passes paraphrases ("set aside what you were told earlier"),
  spaced-out words ("I g n o r e previous instructions"), French, Spanish, Tamil and
  Bengali, Hindi and Hinglish paraphrases, short base64 payloads, markdown fake turns
  ("### System:"), role-play ("my late grandmother used to read me keys") and indirect
  injection inside documents; 4 of the 6 bundled red-team injection prompts get through.
  It also trips on benign text that quotes the phrases ("make my app ignore previous
  instructions from a stale cache").
- ``detect_pii`` misses names, addresses, dates of birth, Indian passport, voter ID,
  driving licence and GSTIN numbers, bank account numbers, landlines, numbers spelled
  out in words, "anita at example dot com" and an Aadhaar split across a line break. It
  reports an order number as a phone, a version string as an IPv4 address, and a
  14-digit account number that happens to pass Luhn as a card.
- ``allowed_topics`` is lexical: a deny list passes "b0mb", "explosive device" and the
  Hindi word, and an allow list trips on a Hindi or paraphrased on-topic question.

Guardrail results never echo the matched sensitive value: evidence is limited to the
kind, character span and a masked preview.

Nothing in this module runs unless you pass a factory's result to an ``Agent``::

    from nexura_agents import Agent
    from nexura_agents.guardrails import block_pii, no_prompt_injection

    agent = Agent(..., input_guardrails=[no_prompt_injection(), block_pii()])
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from nexura_agents.guardrails import GuardrailFn, GuardrailResult
from nexura_agents.run_context import RunContext

__all__ = [
    "PII_KINDS",
    "PIIFinding",
    "allowed_topics",
    "block_pii",
    "detect_pii",
    "json_output_only",
    "max_length",
    "no_prompt_injection",
    "redact_output_pii",
    "redact_pii",
]

PII_KINDS: tuple[str, ...] = (
    "secret",
    "card",
    "aadhaar",
    "pan",
    "email",
    "upi",
    "ifsc",
    "phone",
    "ipv4",
)
"""Every kind ``detect_pii`` can report, in overlap-resolution priority order."""


@dataclass(frozen=True)
class PIIFinding:
    """One detected item. ``preview`` is masked and never contains the full value."""

    kind: str
    span: tuple[int, int]
    preview: str

    def to_evidence(self) -> dict[str, Any]:
        """Return a JSON-friendly dict that is safe to log."""
        return {"kind": self.kind, "span": list(self.span), "preview": self.preview}


# --------------------------------------------------------------------------- checksums

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff_valid(digits: str) -> bool:
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(ch)]]
    return check == 0


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# --------------------------------------------------------------------------- PII patterns
# Every pattern is anchored with look-behinds so a scan only starts at token boundaries,
# and every repetition is bounded or over a single character class: matching is linear.

_UPI_HANDLES = (
    "okaxis|okhdfcbank|oksbi|okicici|ybl|ibl|axl|apl|paytm|upi|sbi|hdfcbank|icici|axisbank|"
    "pnb|boi|cnrb|barodampay|kotak|yesbank|indus|federal|aubank|idfcfirst|postbank|"
    "ptsbi|pthdfc|ptyes|ptaxis|airtel|jio|fbl|rbl|dbs|waicici|wahdfcbank|wasbi|waaxis|freecharge"
)

_RE_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?){0,6}\.[A-Za-z]{2,24}(?![A-Za-z0-9\-])"
)
_RE_UPI = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._\-]{2,64}@(?:"
    + _UPI_HANDLES
    + r")(?![A-Za-z0-9\-]|\.[A-Za-z0-9])",
    re.IGNORECASE,
)
# Boundaries: no adjacent alphanumerics and no dash joining the match to an alphanumeric
# segment (so pieces of UUIDs / hex ids such as ``e6941491-8044-4488-847c`` are not PII).
_LB = r"(?<![A-Za-z0-9])(?<![A-Za-z0-9]-)"
_LA = r"(?![A-Za-z0-9])(?!-[A-Za-z0-9])"
_RE_AADHAAR = re.compile(_LB + r"(?<!\d[ \-])[2-9]\d{3}([ \-]?)\d{4}\1\d{4}" + _LA + r"(?![ \-]\d)")
_RE_PAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z](?![A-Za-z0-9])", re.IGNORECASE
)
_RE_IFSC = re.compile(r"(?<![A-Za-z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Za-z0-9])")
_RE_CARD = re.compile(_LB + r"(?<!\d[ \-])\d(?:[\s\-]{0,2}\d){12,18}" + _LA)
_RE_PHONE_IN = re.compile(
    r"(?<![\w+#])(?<![A-Za-z0-9]-)(?<!\d[ \-])(?:(?:\+91|0091)[ \-]?|91[ \-]?|0)?"
    r"[6-9]\d{4}[ \-]?\d{5}(?!\d)(?!-[A-Za-z0-9])"
)
_RE_PHONE_E164 = re.compile(r"(?<![\w+])\+[1-9](?:[ \-.()]{0,2}\d){7,14}(?!\d)")
_RE_PHONE_US = re.compile(
    r"(?<![\w+#])(?<![A-Za-z0-9]-)\(\d{3}\)[ \-.]?\d{3}[ \-.]\d{4}(?!\d)(?!-[A-Za-z0-9])"
)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_RE_IPV4 = re.compile(r"(?<![\d.])(?:" + _OCTET + r"\.){3}" + _OCTET + r"(?!\d|\.\d)")
_RE_SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_\-]{20,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{36,}"
    r"|github_pat_[A-Za-z0-9_]{40,}"
    r"|AIza[0-9A-Za-z_\-]{35}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|[sr]k_live_[A-Za-z0-9]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r")(?![A-Za-z0-9])"
    r"|-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----"
)


def _mask(kind: str, value: str) -> str:
    if kind == "secret":
        return "*" * 8
    keep = 2
    alnum_total = sum(ch.isalnum() for ch in value)
    seen = 0
    out: list[str] = []
    for ch in value:
        if ch.isalnum():
            seen += 1
            out.append(ch if seen > alnum_total - keep and alnum_total > 4 else "*")
        else:
            out.append(ch)
    return "".join(out)


def _fold_for_pii(text: str) -> tuple[str, list[int] | None]:
    """NFKC-fold ``text``, drop hidden characters and map any Unicode decimal digit to ASCII.

    Returns the folded text and, per folded character, the index of the original
    character it came from (``None`` when the text is plain ASCII and nothing changed).
    """
    if text.isascii():
        return text, None
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if _RE_HIDDEN_STRIP.match(ch):
            continue
        for c in unicodedata.normalize("NFKC", ch):
            d = unicodedata.decimal(c, None)
            out.append(str(d) if d is not None else c)
            idx.append(i)
    return "".join(out), idx


def _candidates(text: str) -> Iterable[tuple[str, int, int]]:
    for m in _RE_SECRET.finditer(text):
        yield "secret", m.start(), m.end()
    for m in _RE_CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            yield "card", m.start(), m.end()
    for m in _RE_AADHAAR.finditer(text):
        if _verhoeff_valid(re.sub(r"\D", "", m.group())):
            yield "aadhaar", m.start(), m.end()
    for m in _RE_PAN.finditer(text):
        yield "pan", m.start(), m.end()
    for m in _RE_EMAIL.finditer(text):
        yield "email", m.start(), m.end()
    for m in _RE_UPI.finditer(text):
        yield "upi", m.start(), m.end()
    for m in _RE_IFSC.finditer(text):
        yield "ifsc", m.start(), m.end()
    for m in _RE_PHONE_IN.finditer(text):
        yield "phone", m.start(), m.end()
    for m in _RE_PHONE_E164.finditer(text):
        yield "phone", m.start(), m.end()
    for m in _RE_PHONE_US.finditer(text):
        yield "phone", m.start(), m.end()
    for m in _RE_IPV4.finditer(text):
        yield "ipv4", m.start(), m.end()


def _check_kinds(kinds: Iterable[str] | None) -> frozenset[str]:
    if kinds is None:
        return frozenset(PII_KINDS)
    if isinstance(kinds, str):
        kinds = [kinds]
    chosen = frozenset(kinds)
    unknown = chosen - set(PII_KINDS)
    if unknown:
        raise ValueError(f"Unknown PII kinds {sorted(unknown)}; choose from {list(PII_KINDS)}.")
    return chosen


def detect_pii(text: str, kinds: Iterable[str] | None = None) -> list[PIIFinding]:
    """Find personal data and secrets in ``text`` (a fast heuristic pre-filter, see the
    module docstring for what it misses).

    Kinds: ``email``, ``phone`` (Indian mobile with optional +91/91/0 prefix, or
    E.164 with a leading ``+``), ``aadhaar`` (12 digits, not starting with 0 or 1,
    Verhoeff checksum valid), ``pan`` (AAAAA9999A with a valid 4th-letter entity code),
    ``card`` (13-19 digits, Luhn valid, space/dash separators allowed), ``ifsc``,
    ``upi`` (``name@handle`` for common bank handles), ``ipv4`` and ``secret`` (common
    API-key prefixes, JWTs and private-key headers). Overlaps resolve in ``PII_KINDS``
    order, so a Luhn-valid card is never also reported as a phone number. Findings are
    sorted by position and hold a masked preview, never the full value.

    Text is NFKC-folded first (fullwidth forms, Unicode digits such as Devanagari, hidden
    zero-width characters) with spans mapped back to the original text. Cards tolerate up
    to two whitespace/dash separators. Matches glued to alphanumerics or joined by a dash
    to an alphanumeric segment (UUID / hex fragments) are ignored. Checksums are only a
    filter: about 1 in 10 random 12-digit ids passes Verhoeff (Aadhaar) and about 1 in 10
    random 13-19 digit ids passes Luhn (card), so long numeric ids can false-positive.
    """
    wanted = _check_kinds(kinds)
    folded, idx = _fold_for_pii(text)
    priority = {k: i for i, k in enumerate(PII_KINDS)}
    cands = sorted(
        (c for c in _candidates(folded) if c[0] in wanted),
        key=lambda c: (priority[c[0]], c[1]),
    )
    taken: list[tuple[int, int]] = []
    accepted: list[tuple[str, int, int]] = []
    for kind, start, end in cands:
        if any(start < e and s < end for s, e in taken):
            continue
        taken.append((start, end))
        accepted.append((kind, start, end))
    accepted.sort(key=lambda c: c[1])
    if idx is not None:
        accepted = [(k, idx[s], idx[e - 1] + 1) for k, s, e in accepted]
    return [PIIFinding(k, (s, e), _mask(k, text[s:e])) for k, s, e in accepted]


def redact_pii(
    text: str, kinds: Iterable[str] | None = None, mask: str = "[REDACTED:{kind}]"
) -> str:
    """Replace detected PII in ``text``. ``{kind}`` in ``mask`` becomes the finding kind."""
    out: list[str] = []
    pos = 0
    for f in detect_pii(text, kinds):
        out.append(text[pos : f.span[0]])
        out.append(mask.replace("{kind}", f.kind))
        pos = f.span[1]
    out.append(text[pos:])
    return "".join(out)


# --------------------------------------------------------------------------- helpers


def _as_text(value: Any) -> str:
    """Turn a guardrail value (text or a typed output object) into scannable text."""
    if isinstance(value, str):
        return value
    dump_json = getattr(value, "model_dump_json", None)
    if callable(dump_json):
        return str(dump_json())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _named(fn: Callable[[RunContext[Any], Any], GuardrailResult], name: str) -> GuardrailFn:
    fn.__name__ = name
    fn.__qualname__ = name
    return fn


# --------------------------------------------------------------------------- PII guardrails


def block_pii(
    kinds: Iterable[str] | None = None, on: Literal["input", "output"] = "input"
) -> GuardrailFn:
    """Pre-filter guardrail that trips when ``detect_pii`` finds any of ``kinds`` (default: all).

    Regex and checksum only, so it has the misses listed in the module docstring.
    ``on`` labels the stage the guardrail is meant for; pass the result to
    ``input_guardrails`` or ``output_guardrails`` accordingly. Typed outputs are
    scanned as JSON. Evidence holds kinds, spans and masked previews only.
    """
    if on not in ("input", "output"):
        raise ValueError("on must be 'input' or 'output'.")
    wanted = sorted(_check_kinds(kinds))

    def block_pii(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        findings = detect_pii(_as_text(value), wanted)
        if not findings:
            return GuardrailResult.passed()
        found_kinds = sorted({f.kind for f in findings})
        return GuardrailResult.tripped(
            {
                "reason": f"{on} contains personal data or secrets: {', '.join(found_kinds)}",
                "kinds": found_kinds,
                "findings": [f.to_evidence() for f in findings],
            }
        )

    return _named(block_pii, "block_pii")


def redact_output_pii(
    kinds: Iterable[str] | None = None,
    mask: str = "[REDACTED:{kind}]",
    on_structured: Literal["trip", "pass"] = "trip",
) -> GuardrailFn:
    """Output pre-filter that rewrites text output with PII redacted instead of tripping.

    It redacts only what ``detect_pii`` finds (see its misses). It returns
    ``GuardrailResult.rewritten(...)``, which the runner applies to the final
    text before it is returned, stored in the session or emitted. The rewrite hook works
    for text output only. For an agent with ``output_type``, output containing PII
    cannot be rewritten safely, so by default the guardrail trips (``on_structured="trip"``);
    ``"pass"`` lets it through unredacted.
    """
    if on_structured not in ("trip", "pass"):
        raise ValueError("on_structured must be 'trip' or 'pass'.")
    wanted = sorted(_check_kinds(kinds))

    def redact_output_pii(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        if isinstance(value, str):
            findings = detect_pii(value, wanted)
            if not findings:
                return GuardrailResult.passed()
            return GuardrailResult.rewritten(
                redact_pii(value, wanted, mask),
                {"redacted": [f.to_evidence() for f in findings]},
            )
        findings = detect_pii(_as_text(value), wanted)
        if not findings or on_structured == "pass":
            return GuardrailResult.passed({"skipped": "structured output is not rewritten"})
        return GuardrailResult.tripped(
            {
                "reason": "structured output contains personal data and cannot be redacted",
                "kinds": sorted({f.kind for f in findings}),
                "findings": [f.to_evidence() for f in findings],
            }
        )

    return _named(redact_output_pii, "redact_output_pii")


# --------------------------------------------------------------------------- injection

_D = r"[ऀ-ॿ]*"  # Devanagari word tail (\w does not match combining marks)
_GAP = r"(?:(?:all|any|the|your|of|my|these|those)\s+){0,3}"
_PREV = r"(?:previous|prior|above|earlier|preceding|foregoing|original|initial|system)"
_THING = r"(?:instructions?|prompts?|rules|directions?|directives|guidelines|context|messages?)"
_ACT = r"(?:ignore|disregard|forget|override|bypass|discard|abandon)"
_ABOVE = r"(?:above|before|prior|earlier|preceding)"
_YOUR = r"(?:(?:all|any)\s+(?:of\s+)?)?your\s+(?:other\s+)?"

# (id, weight, pattern). Matched against text with hidden characters and nuktas removed.
_INJECTION_RULES: tuple[tuple[str, float, str], ...] = (
    ("override_en", 0.8, rf"\b{_ACT}\s+{_GAP}{_PREV}\s+{_THING}"),
    ("override_en_above", 0.8, rf"\b{_ACT}\s+{_GAP}{_THING}\s+{_ABOVE}\b"),
    (
        "override_en_bare",
        0.6,
        rf"\b{_ACT}\s+(?:{_YOUR}|(?:all|any)\s+(?:of\s+)?)(?:instructions?|prompts?|directives|guidelines)\b",
    ),
    (
        "override_en_dont_follow",
        0.7,
        rf"\b(?:do\s+not|don['\u2019]?t|never|stop)\s+(?:follow|obey|following|obeying|listen\s+to|listening\s+to)\s+(?:{_GAP}{_PREV}\s+|{_YOUR}){_THING}",
    ),
    (
        "override_en_all",
        0.8,
        rf"\b{_ACT}\s+(?:everything|anything|all)\s+(?:you\s+were\s+told|above|before|prior)",
    ),
    (
        "override_new_rules",
        0.4,  # ambiguous on its own ("New instructions: office closed"); needs corroboration
        r"\b(?:new|updated)\s+instructions?\s*(?:are|:)|\bfrom\s+now\s+on\s*,?\s+you\s+(?:will|must|are|shall)",
    ),
    (
        "exfil_en",
        0.8,
        rf"\b(?:reveal|show|print|repeat|output|display|leak|tell\s+me|give\s+me|share)\s+(?:me\s+)?{_GAP}(?:system|initial|hidden|secret|original|developer)\s+(?:prompt|instructions?|message)",
    ),
    (
        "exfil_en_what",
        0.7,
        r"\bwhat\s+(?:is|are|was|were)\s+your\s+(?:system|initial|original|hidden)\s+(?:prompt|instructions?)",
    ),
    (
        "exfil_en_your",
        0.6,
        rf"\b(?:reveal|show|print|repeat|output|display|leak|tell\s+me|give\s+me|share)\s+(?:me\s+)?{_YOUR}(?:instructions?|prompt)\b",
    ),
    ("exfil_repeat_above", 0.7, r"\brepeat\s+(?:the\s+)?(?:words|text|everything)\s+above"),
    (
        "jailbreak_dan",
        0.8,
        r"\b(?:DAN\s+mode|you\s+are\s+now\s+(?-i:DAN)\b|do\s+anything\s+now|developer\s+mode\s+(?:enabled|on|activated)|jailbreak(?:ed|ing)?\b)",
    ),
    (
        "jailbreak_unrestricted",
        0.7,
        r"\b(?:act|behave|respond)\s+(?:as|like)\s+(?:an?\s+)?(?:unrestricted|unfiltered|uncensored|evil|jailbroken)|\bwithout\s+(?:any\s+)?(?:restrictions|filters|limitations|guidelines|censorship)\b",
    ),
    (
        "roleplay_marker",
        0.3,
        r"\b(?:pretend\s+(?:that\s+)?(?:you\s+are|to\s+be|you're)|you\s+are\s+now\s+(?:an?\s+)?\w+|role-?play\s+as|stay\s+in\s+character)\b",
    ),
    (
        "chat_delimiter",
        0.6,
        r"<\|?(?:im_start|im_end|system|endoftext)\|?>|\[/?INST\]|<<\s*SYS\s*>>",
    ),
    ("fake_system_turn", 0.4, r"(?:^|\n)\s*(?:system|assistant)\s*:\s*\S"),
    # Hindi (Devanagari). Nukta (U+093C) is stripped before matching.
    (
        "override_hi",
        0.8,
        rf"(?:पिछले|पहले\s+के|ऊपर\s+के|उपर\s+के|सभी|सारे|पुराने)\s+(?:(?:सभी|सारे)\s+)?(?:निर्देश|आदेश|नियम|हिदायत){_D}\s+(?:को\s+)?(?:अनदेखा|नजरअंदाज|भूल|इग्नोर){_D}",
    ),
    ("override_hi_verb", 0.5, rf"(?:निर्देश|आदेश|नियम){_D}\s+(?:को\s+)?(?:अनदेखा|नजरअंदाज|इग्नोर)\s+कर"),
    (
        "exfil_hi",
        0.8,
        rf"(?:सिस्टम|गुप्त|छिपे)\s+(?:प्रॉम्प्ट|प्रोम्प्ट|प्रांप्ट|निर्देश|इंस्ट्रक्शन){_D}\s+(?:को\s+)?(?:बता|दिखा|छाप|प्रिंट|बोल){_D}",
    ),
    (
        "jailbreak_hi",
        0.6,
        rf"(?:बिना|बगैर)\s+(?:किसी\s+)?(?:प्रतिबंध|रोक\s*टोक|फिल्टर|सीमा){_D}|अब\s+से\s+तुम\s+(?:एक\s+)?(?:DAN|बिना)",
    ),
    # Hinglish (Latin script).
    (
        "override_hinglish",
        0.8,
        r"\b(?:pichle|pichhle|pehle\s+ke|upar\s+ke|purane|saare|sare|sabhi|sab)\s+(?:(?:sabhi|saare|sare)\s+)?(?:instructions?|nirdesh\w*|aadesh\w*|niyam|rules?)\s+(?:ko\s+)?(?:ignore|andekha|andekhi|nazarandaz|bhool|bhul)\w*",
    ),
    (
        "override_hinglish_verb",
        0.5,
        r"\b(?:instructions?|nirdesh\w*|rules?)\s+ko\s+(?:ignore|andekha|bhool)\s*(?:kar|karo|karna)?",
    ),
    (
        "exfil_hinglish",
        0.8,
        r"\b(?:system|secret|hidden)\s+prompt\s+(?:ko\s+)?(?:batao|bata\s+do|dikhao|dikha\s+do|print\s+karo|bolo)\b",
    ),
    (
        "jailbreak_hinglish",
        0.6,
        r"\bbina\s+(?:kisi\s+)?(?:restriction|rok\s*tok|filter|pabandi)\w*",
    ),
)

_ZERO_WIDTH = "​⁠﻿᠎­"  # ZWJ/ZWNJ excluded: legitimate in Indic scripts
_BIDI = "‪‫‬‭‮⁦⁧⁨⁩"
_RE_HIDDEN_STRIP = re.compile("[" + _ZERO_WIDTH + _BIDI + "‌‍\U000e0000-\U000e007f]")
_RE_ZERO_WIDTH = re.compile("[" + _ZERO_WIDTH + "]")
_RE_BIDI = re.compile("[" + _BIDI + "]")
_RE_TAGCHARS = re.compile("[\U000e0000-\U000e007f]")
_RE_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{200,}={0,2}(?![A-Za-z0-9+/=])")

_COMPILED_RULES = tuple((rid, w, re.compile(p, re.IGNORECASE)) for rid, w, p in _INJECTION_RULES)


_CONFUSABLES = {
    **dict(zip("аеорсухіјѕԁӏһԛԝкмнтѵ", "aeopcyxijsdlhqwkmhtv", strict=True)),
    **dict(zip("αβεικνορτυχηω", "abeiknoptuxnw", strict=True)),
}
_CONFUSABLES.update({c.upper(): v for c, v in list(_CONFUSABLES.items())})
_CONFUSABLE_TABLE = str.maketrans(_CONFUSABLES)


def _normalise_for_injection(text: str) -> str:
    text = unicodedata.normalize("NFKC", _RE_HIDDEN_STRIP.sub("", text))
    return text.replace("़", "").translate(_CONFUSABLE_TABLE)


def _injection_signals(text: str, extra: Sequence[re.Pattern[str]]) -> list[tuple[str, float, int]]:
    """Return ``(signal id, weight, character offset)`` for each distinct signal found."""
    signals: list[tuple[str, float, int]] = []
    body = text[1:] if text.startswith("﻿") else text  # a leading BOM is common and benign
    for rid, regex, weight in (
        ("zero_width_chars", _RE_ZERO_WIDTH, 0.5),
        ("bidi_override_chars", _RE_BIDI, 0.6),
        ("unicode_tag_chars", _RE_TAGCHARS, 0.8),
        ("long_base64_blob", _RE_BASE64, 0.5),
    ):
        m = regex.search(body)
        if m:
            signals.append((rid, weight, m.start()))
    norm = _normalise_for_injection(text)
    for rid, weight, rx in _COMPILED_RULES:
        m = rx.search(norm)
        if m:
            signals.append((rid, weight, m.start()))
    for i, rx in enumerate(extra):
        m = rx.search(norm)
        if m:
            signals.append((f"extra_pattern_{i}", 1.0, m.start()))
    return signals


def no_prompt_injection(
    extra_patterns: Sequence[str | re.Pattern[str]] = (), threshold: float = 0.5
) -> GuardrailFn:
    """Fast prompt-injection pre-filter. Trips when the summed signal score >= ``threshold``.

    Signals (each counted once): instruction-override phrases ("ignore previous
    instructions"), system-prompt exfiltration requests, jailbreak and role-play
    markers, chat-template delimiter spoofing, in English, Hindi (Devanagari) and
    Hinglish; plus hidden-text tricks: zero-width characters, bidi overrides, Unicode
    tag characters and long base64 blobs. Strong phrases score 0.5-0.8, weak role-play
    markers 0.3, each ``extra_patterns`` match 1.0. Lower ``threshold`` to be stricter.

    Paraphrases, spaced-out words, other languages (French, Spanish, Tamil, Bengali),
    Hindi and Hinglish paraphrases, short base64, markdown fake turns, role-play and
    indirect injection are missed, and benign text quoting the phrases can trip it. Put a
    model-based check behind it. Evidence lists signal ids, offsets and the score, not
    the matched text.
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive.")
    extra = tuple(re.compile(p, re.IGNORECASE) if isinstance(p, str) else p for p in extra_patterns)

    def no_prompt_injection(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        signals = _injection_signals(_as_text(value), extra)
        score = round(sum(w for _, w, _ in signals), 3)
        if score < threshold:
            return GuardrailResult.passed({"score": score})
        return GuardrailResult.tripped(
            {
                "reason": "possible prompt injection (heuristic)",
                "score": score,
                "threshold": threshold,
                "signals": [{"id": rid, "weight": w, "offset": off} for rid, w, off in signals],
            }
        )

    return _named(no_prompt_injection, "no_prompt_injection")


# --------------------------------------------------------------------------- other checks


def _estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_chars / 4 + (len(text) - ascii_chars) / 1.5)


def max_length(chars: int | None = None, tokens: int | None = None) -> GuardrailFn:
    """Trip when text is longer than ``chars`` characters or ``tokens`` estimated tokens.

    The token count is a rough estimate (about 4 ASCII characters or 1.5 non-ASCII
    characters per token), not a real tokenizer. Works for input and output.
    """
    if chars is None and tokens is None:
        raise ValueError("Give chars, tokens or both.")
    if (chars is not None and chars <= 0) or (tokens is not None and tokens <= 0):
        raise ValueError("Limits must be positive.")

    def max_length(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        text = _as_text(value)
        if chars is not None and len(text) > chars:
            return GuardrailResult.tripped(
                {
                    "reason": f"text is {len(text)} characters, limit is {chars}",
                    "length": len(text),
                    "limit_chars": chars,
                }
            )
        if tokens is not None:
            est = _estimate_tokens(text)
            if est > tokens:
                return GuardrailResult.tripped(
                    {
                        "reason": f"text is about {est} tokens, limit is {tokens}",
                        "estimated_tokens": est,
                        "limit_tokens": tokens,
                    }
                )
        return GuardrailResult.passed()

    return _named(max_length, "max_length")


def _keyword_regex(words: Iterable[str]) -> re.Pattern[str] | None:
    items = sorted({w.strip() for w in words if w and w.strip()}, key=len, reverse=True)
    if not items:
        return None
    body = "|".join(re.escape(w) for w in items)
    return re.compile(rf"(?<![\wऀ-ॿ])(?:{body})(?![\wऀ-ॿ])", re.IGNORECASE)


def allowed_topics(keywords: Iterable[str] = (), deny: Iterable[str] = ()) -> GuardrailFn:
    """Keyword topic pre-filter. This is lexical matching, not semantic understanding.

    With ``keywords``, text must contain at least one (whole word, case-insensitive) or
    the guardrail trips; empty text passes. With ``deny``, text containing any denied
    word trips. A paraphrase that avoids every keyword defeats both checks, and an
    allowed keyword can appear in an off-topic message. Measured: ``deny=["bomb"]``
    passes "b0mb", "explosive device" and the Hindi word; ``keywords=["emi"]`` trips on
    "How much is my monthly instalment?" and on the same question in Hindi.
    """
    allow_rx = _keyword_regex(keywords)
    deny_rx = _keyword_regex(deny)
    if allow_rx is None and deny_rx is None:
        raise ValueError("Give keywords, deny or both.")

    def allowed_topics(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        text = _as_text(value)
        if deny_rx is not None:
            m = deny_rx.search(text)
            if m:
                return GuardrailResult.tripped(
                    {
                        "reason": "text mentions a denied topic",
                        "denied_keyword": m.group().lower(),
                        "offset": m.start(),
                    }
                )
        if allow_rx is not None and text.strip() and not allow_rx.search(text):
            return GuardrailResult.tripped(
                {"reason": "text matches none of the allowed topic keywords"}
            )
        return GuardrailResult.passed()

    return _named(allowed_topics, "allowed_topics")


def json_output_only() -> GuardrailFn:
    """Output guardrail: text must be exactly one JSON object or array, nothing else.

    Markdown fences or prose around the JSON trip it. Already-structured output (an
    agent with ``output_type``) passes. Evidence has the parser's error position, not
    the text.
    """

    def json_output_only(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        if not isinstance(value, str):
            return GuardrailResult.passed()
        try:
            parsed = json.loads(value.strip())
        except ValueError as exc:
            pos = getattr(exc, "pos", None)
            return GuardrailResult.tripped(
                {"reason": "output is not valid JSON", "error": type(exc).__name__, "position": pos}
            )
        if not isinstance(parsed, dict | list):
            return GuardrailResult.tripped({"reason": "output is JSON but not an object or array"})
        return GuardrailResult.passed()

    return _named(json_output_only, "json_output_only")
