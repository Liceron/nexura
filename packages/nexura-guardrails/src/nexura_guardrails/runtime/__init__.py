# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workflow glue: the ``guardrails.check`` module (``GuardrailCheckModule``).

It needs only ``nexura_agents`` (``BaseModule`` lives in ``nexura_agents.contracts``), so it
imports without ``nexura-runtime``; the runtime registers it when this package is installed.
"""

from __future__ import annotations

from nexura_guardrails.runtime.module import (
    GUARDRAIL_CHECK_MODULE_ID,
    GuardrailCheckModule,
    node_tool_info,
)

__all__ = ["GUARDRAIL_CHECK_MODULE_ID", "GuardrailCheckModule", "node_tool_info"]
