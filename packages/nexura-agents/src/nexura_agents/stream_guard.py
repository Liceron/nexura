# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""How streamed reply text meets output guardrails (``Agent(stream_guard=...)``).

With ``stream_text=True`` the runner emits ``TEXT_DELTA`` events while the model writes.
When the active agent has output guardrails (its own ``output_guardrails``, or guardrails
that the run's hooks contribute through ``AgentHooks.output_guardrails``: an inherited or
process-default policy), unchecked text must not reach the consumer. The modes:

- ``"auto"`` (default): ``"hold"`` when there are output guardrails, otherwise text streams
  as it is generated.
- ``"hold"``: no ``TEXT_DELTA`` for the reply. The checked text arrives in
  ``FINAL_OUTPUT`` after every output guardrail passed (rewrites applied).
- ``"chunked"``: opt-in. The raw reply is checked in windows of ``chunk_chars`` new
  characters by every output guardrail; checked text is shown only after it passed
  (rewritten when a guardrail rewrote it). The raw end of each window (at least
  ``overlap_chars`` visible characters, and everything from the first change a rewrite made
  near the end) is held back and checked again, raw, with the next window, so a value that
  spans two windows is always seen whole. The whole raw answer is checked again at the end;
  the rest is shown only after that. After ``max_windows`` windows the rest of the turn is
  held until that final check. Agents with ``output_type`` use ``"hold"``.
- ``"passthrough"``: today's behaviour (show text, then check the final answer). Only for an
  agent's own guardrails: guardrails contributed by hooks (a policy) always hold.

A trip emits ``GUARDRAIL_DECISION``, ``GUARDRAIL_TRIPPED`` (no content) and ``RUN_FAILED``;
the blocked window is never shown.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, get_args

from nexura_agents.errors import SDKValidationError

__all__ = ["MIN_OVERLAP_CHARS", "StreamGuard", "StreamGuardMode", "visible_length"]

StreamGuardMode = Literal["auto", "hold", "chunked", "passthrough"]
_MODES: tuple[str, ...] = get_args(StreamGuardMode)

#: The smallest ``overlap_chars`` accepted: a card number, an IBAN or a short API key fits.
MIN_OVERLAP_CHARS = 32

# Characters that are invisible or merge into their neighbour: format (zero-width, bidi,
# tag characters), combining marks, controls, surrogates and private use.
_NO_WIDTH_CATEGORIES = frozenset({"Cf", "Mn", "Me", "Cc", "Cs", "Co"})


def _weight(ch: str, previous: str | None) -> int:
    """How many visible characters ``ch`` adds after NFKC folding; a whitespace run counts
    once (``previous`` is the character after it, scanning from the end)."""
    if ch.isspace():
        return 0 if previous is not None and previous.isspace() else 1
    if unicodedata.category(ch) in _NO_WIDTH_CATEGORIES:
        return 0
    return max(len(unicodedata.normalize("NFKC", ch)), 1)


def visible_length(text: str) -> int:
    """Visible characters in ``text`` after NFKC, without invisible or zero-width characters
    and with each whitespace run counted once (how the hold-back is measured)."""
    total, after = 0, None
    for ch in reversed(text):
        total += _weight(ch, after)
        after = ch
    return total


def _tail_for(text: str, visible: int) -> int:
    """How many raw characters at the end of ``text`` hold ``visible`` visible characters."""
    if visible <= 0:
        return 0
    seen, after = 0, None
    for i in range(len(text) - 1, -1, -1):
        seen += _weight(text[i], after)
        after = text[i]
        if seen >= visible:
            # Keep invisible characters right before the held part with it.
            j = i
            while j > 0 and _weight(text[j - 1], text[j]) == 0 and not text[j - 1].isspace():
                j -= 1
            return len(text) - j
    return len(text)


@dataclass(frozen=True)
class StreamGuard:
    """Streaming settings for an agent with output guardrails (see the module docstring).

    ``chunk_chars``: new characters per checked window (``"chunked"``). ``overlap_chars``:
    visible characters (after NFKC, without invisible characters) held back from each window
    and checked again with the next one; at least :data:`MIN_OVERLAP_CHARS` (32), default 120.
    Make it at least as long as the longest value a guardrail must see whole.
    ``max_windows``: window checks per model turn (default 64); after that the rest of the
    turn is held until the check of the whole answer, so guardrail calls stay bounded.
    """

    mode: StreamGuardMode = "auto"
    chunk_chars: int = 400
    overlap_chars: int = 120
    max_windows: int = 64

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise SDKValidationError(
                f"stream_guard mode must be one of {', '.join(_MODES)}, got {self.mode!r}."
            )
        for name, low in (
            ("chunk_chars", 1),
            ("overlap_chars", MIN_OVERLAP_CHARS),
            ("max_windows", 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < low:
                raise SDKValidationError(f"{name} must be an integer >= {low}, got {value!r}.")


def coerce_stream_guard(value: Any) -> StreamGuard:
    """``Agent(stream_guard=...)``: a mode name or a :class:`StreamGuard`."""
    if isinstance(value, StreamGuard):
        return value
    if isinstance(value, str):
        return StreamGuard(mode=value)  # type: ignore[arg-type]  # validated in __post_init__
    raise SDKValidationError(
        "stream_guard must be 'auto', 'hold', 'chunked', 'passthrough' or a StreamGuard, "
        f"got {type(value).__name__}."
    )


def resolve_mode(
    guard: StreamGuard, *, has_checks: bool, from_hooks: bool, typed: bool
) -> Literal["hold", "chunked", "passthrough"]:
    """The mode a model turn uses. No output guardrails: text streams as before."""
    if not has_checks:
        return "passthrough"
    mode = guard.mode
    if mode == "passthrough":
        # An agent may opt its own guardrails out; a policy's guardrails always hold.
        return "hold" if from_hooks else "passthrough"
    if mode == "chunked":
        return "hold" if typed else "chunked"
    return "hold"  # "auto" and "hold"


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


class TextGate:
    """The raw reply text of one model turn and how much of it is shown (``hold`` /
    ``chunked``).

    ``raw`` is everything the model wrote. ``raw[:done]`` is covered by ``shown`` (checked
    text, possibly rewritten); ``raw[done:]`` is not shown yet and is always checked raw.
    ``checked_to`` is how far windows have reached.
    """

    def __init__(self, mode: Literal["hold", "chunked"], guard: StreamGuard) -> None:
        self.mode = mode
        self.guard = guard
        self.raw = ""
        self.done = 0
        self.checked_to = 0
        self.windows = 0
        self.shown: list[str] = []

    @property
    def held(self) -> bool:
        """The turn produced reply text (shown or not)."""
        return bool(self.raw)

    @property
    def exhausted(self) -> bool:
        """No more window checks this turn (``max_windows``): the rest is held."""
        return self.windows >= self.guard.max_windows

    def shown_text(self) -> str:
        return "".join(self.shown)

    def add(self, text: str) -> None:
        self.raw += text

    def next_window(self) -> tuple[int, int] | None:
        """``(start, end)`` raw offsets of the next window to check: the held-back raw text
        plus ``chunk_chars`` new characters, once that many arrived."""
        if self.mode != "chunked" or self.exhausted:
            return None
        if len(self.raw) - self.checked_to < self.guard.chunk_chars:
            return None
        return self.done, self.checked_to + self.guard.chunk_chars

    def accept(self, start: int, end: int, checked: str) -> str:
        """``checked`` is what the guardrails made of ``raw[start:end]``; returns the text
        to show now.

        The end of the window is held back raw: at least ``overlap_chars`` visible
        characters, and, when a rewrite changed anything there (a match cut by the window
        end), everything from the window's first change on. Only text before the held part
        is shown."""
        window = self.raw[start:end]
        self.windows += 1
        self.checked_to = end
        hold = _tail_for(window, self.guard.overlap_chars)
        same_end = _common_suffix(window, checked)
        if same_end >= hold:
            # The held part is unchanged: show the checked text before it.
            show = checked[: len(checked) - hold]
            cut = len(window) - hold
        else:
            # A rewrite reached into the held part: show only unchanged raw text before the
            # first change, and check the rest again with the next window.
            cut = min(_common_prefix(window, checked), len(window) - hold)
            show = window[:cut]
        self.done = start + cut
        if show:
            self.shown.append(show)
        return show
