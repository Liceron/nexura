# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Keep Guardrails AI's usage telemetry off unless the user chose it.

Guardrails AI 0.11 sends usage spans to its own servers when ``enable_metrics`` is true, and
true is its default when ``~/.guardrailsrc`` does not set it. Every ``Guard`` reloads that file
(``RC.load``), so changing ``settings.rc`` once is undone by the next ``Guard``. This module
wraps ``RC.load`` so an *unset* preference loads as ``enable_metrics=False``. An explicit
``enable_metrics`` or ``no_metrics`` line in ``~/.guardrailsrc`` is respected, and
``NEXURA_GUARDRAILS_AI_TELEMETRY=on`` keeps Guardrails' own default.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from guardrails.classes.rc import RC  # type: ignore[import-untyped]
from guardrails.settings import settings  # type: ignore[import-untyped]

_PATCHED = "_nexura_telemetry_default_off"


def opted_in() -> bool:
    """True when ``NEXURA_GUARDRAILS_AI_TELEMETRY`` asks to keep Guardrails' default."""
    value = os.environ.get("NEXURA_GUARDRAILS_AI_TELEMETRY", "")
    return value.strip().lower() in ("on", "1", "true", "yes")


def rc_sets_metrics() -> bool:
    """True when ``~/.guardrailsrc`` has an explicit ``enable_metrics`` or ``no_metrics`` line."""
    try:
        lines = (Path.home() / ".guardrailsrc").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    keys = {line.split("=", 1)[0].strip() for line in lines if "=" in line}
    return bool(keys & {"enable_metrics", "no_metrics"})


def install() -> None:
    """Wrap ``RC.load`` once and correct the settings already loaded."""
    if opted_in():
        return
    if not getattr(RC, _PATCHED, False):
        original = RC.load.__func__

        def load(cls: Any, logger: logging.Logger | None = None) -> Any:
            rc = original(cls, logger)
            if not opted_in() and not rc_sets_metrics():
                rc.enable_metrics = False
            return rc

        RC.load = classmethod(load)
        setattr(RC, _PATCHED, True)
    if not rc_sets_metrics():
        settings.rc.enable_metrics = False
