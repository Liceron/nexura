# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Inspection and detection of protected invariant context elements.

Guarantees:
- Instructions, corrections, negations, IDs, amounts, approval arguments, and
  required evidence are flagged as protected invariants.
- Unreferenced fields are never deemed automatically irrelevant.
"""

from __future__ import annotations

import re
from typing import Any

from nexura_agents.context.models import ContextItem, ContextPolicy

_NEGATION_REGEX = re.compile(
    r"\b(do\s+not|don't|never|cannot|can't|must\s+not|mustn't|reject|rejected|"
    r"denied|deny|prohibit|prohibited|forbidden|exclude|excluded|refrain|stop)\b",
    re.IGNORECASE,
)

_CORRECTION_REGEX = re.compile(
    r"\b(wait|actually|scratch that|correction|instead of|revised instruction|"
    r"changed my mind|never mind|disregard|ignore previous|updated request)\b",
    re.IGNORECASE,
)

_UUID_REGEX = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

_ID_PREFIX_REGEX = re.compile(
    r"\b(?:call|tool|node|run|user|doc|chunk|account|acc|txn|tx|app|req|wf)_[A-Za-z0-9_-]+\b",
    re.IGNORECASE,
)

_CURRENCY_SYMBOL_REGEX = re.compile(r"[$€£¥₹]\s*\d+(?:,\d{3})*(?:\.\d+)?")

_CURRENCY_CODE_REGEX = re.compile(
    r"\b\d+(?:,\d{3})*(?:\.\d+)?\s*(?:USD|EUR|GBP|INR|JPY|CAD|AUD|cents|dollars|rupees|euros)\b",
    re.IGNORECASE,
)


def _extract_text(content: Any) -> str:
    """Extract plain string representation from various message or payload types."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return " ".join(f"{k}: {v}" for k, v in content.items())
    if hasattr(content, "content") and isinstance(content.content, str):
        return content.content
    if hasattr(content, "text_content") and isinstance(content.text_content, str):
        return content.text_content
    return str(content)


def inspect_item_invariants(item: ContextItem, policy: ContextPolicy) -> tuple[bool, list[str]]:
    """Inspect an item for protected invariants according to the configured policy.

    Returns (is_protected, list_of_reasons).
    """
    reasons: list[str] = []
    text = _extract_text(item.content)

    # 1. System instructions
    if policy.protect_instructions and (
        item.role == "system" or item.item_type in ("system_prompt", "instruction")
    ):
        reasons.append("protected_system_instruction")

    # 2. Changed user intent / corrections
    if policy.protect_corrections and _CORRECTION_REGEX.search(text):
        reasons.append("protected_correction")

    # 3. Negations
    if policy.protect_negations and _NEGATION_REGEX.search(text):
        reasons.append("protected_negation")

    # 4. Identifiers (UUIDs, tool IDs, transaction IDs)
    if policy.protect_identifiers and (
        item.tool_call_id
        or _UUID_REGEX.search(text)
        or _ID_PREFIX_REGEX.search(text)
        or (
            isinstance(item.content, dict)
            and any(k.endswith("_id") or k == "id" for k in item.content)
        )
    ):
        reasons.append("protected_identifier")

    # 5. Financial amounts
    if policy.protect_financial_amounts and (
        _CURRENCY_SYMBOL_REGEX.search(text) or _CURRENCY_CODE_REGEX.search(text)
    ):
        reasons.append("protected_financial_amount")

    # 6. Approval arguments and gated effects
    if policy.protect_approval_arguments and (
        item.item_type == "approval"
        or (
            isinstance(item.content, dict)
            and any(
                k in item.content for k in ("approval_id", "state_update", "action", "effect_id")
            )
        )
    ):
        reasons.append("protected_approval_args")

    # 7. Tool associations
    if policy.protect_tool_associations and (
        item.tool_call_id
        or item.role == "tool"
        or item.item_type == "tool_definition"
        or (hasattr(item.content, "tool_calls") and item.content.tool_calls)
    ):
        reasons.append("protected_tool_association")

    # 8. Required evidence
    if policy.protect_required_evidence and (
        item.is_required_evidence or item.metadata.get("required_evidence", False)
    ):
        reasons.append("protected_required_evidence")

    is_protected = len(reasons) > 0
    return is_protected, reasons
