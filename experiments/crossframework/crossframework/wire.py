"""Human-readable refusal followed by the machine envelope in one MCP text block."""

from __future__ import annotations

import json
from typing import Any


class NotExecuted(Exception):
    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = {**outcome, "executed": False}
        status = outcome["status"]
        descriptions = {
            "pending_approval": "The refund is awaiting operator approval.",
            "blocked": "The refund is blocked by policy.",
            "rejected": "The reviewer rejected the refund.",
            "approval_refused": "The approval does not authorize this refund.",
            "expired": "The approval expired before dispatch.",
            "invalid": "The refund arguments are invalid.",
            "error": "The refund stopped before dispatch.",
        }
        prior = outcome.get("prior_phase")
        if status == "outcome_unknown":
            # Never claim that a possibly half-run action moved no money.
            sentence = (
                "This retry did not dispatch. The prior outcome is unknown; "
                "money may have moved. Do not retry under a new request id."
            )
        elif status == "request_id_conflict" and prior == "completed":
            sentence = (
                "This call did not dispatch. This request_id already completed a refund "
                "with different arguments; money has moved for it. Check that refund "
                "before requesting another."
            )
        elif status == "request_id_conflict" and prior == "dispatching":
            sentence = (
                "This call did not dispatch. This request_id belongs to a refund with "
                "different arguments whose outcome is unknown; money may have moved. "
                "Do not retry under a new request id."
            )
        elif status == "request_id_conflict":
            sentence = (
                "This call did not dispatch. This request_id belongs to a refund with "
                "different arguments that has not run; no money has moved for it. "
                "Repeat its exact arguments, or use a new request_id for a different refund."
            )
        else:
            sentence = descriptions[status] + " No money has moved."
        super().__init__("NOT EXECUTED: " + sentence + "\n" + json.dumps(self.outcome))
