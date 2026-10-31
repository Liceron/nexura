# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Bidirectional integration between Nexura checks and Guardrails AI 0.11.

Guardrails AI sends usage telemetry to its own servers unless told not to. Importing this
package makes an unset preference mean *off* (see ``_telemetry``): an explicit
``enable_metrics``/``no_metrics`` line in ``~/.guardrailsrc`` is respected, and
``NEXURA_GUARDRAILS_AI_TELEMETRY=on`` keeps Guardrails' own default. Remote inference and
validators that call remote APIs are separate choices and are left as configured.
"""

from nexura_guardrails_ai import _telemetry
from nexura_guardrails_ai.bridge import HUB_CATEGORIES, GuardrailsAICheck, from_hub, guardrails_ai
from nexura_guardrails_ai.validators import as_validator, nexura_validators

# Before any Guard exists: an unset telemetry preference means off.
_telemetry.install()

__version__ = "0.7.1"
__all__ = [
    "HUB_CATEGORIES",
    "GuardrailsAICheck",
    "as_validator",
    "from_hub",
    "guardrails_ai",
    "nexura_validators",
]
