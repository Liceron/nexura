# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Evidence receipts and the local readiness summary built from them.

A receipt records one executed check: the command, its exit code, measured values
and the invariants it asserted. The summary is derived only from receipts, so a
check that was not run, was skipped, or claims success without evidence cannot
appear as passed.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

ReceiptStatus = Literal["passed", "failed", "skipped", "unmeasured"]


class EvidenceError(Exception):
    """Raised when receipts are missing, inconsistent or contradict their claims."""


@dataclass(frozen=True)
class Invariant:
    """One asserted property and whether it held."""

    name: str
    held: bool
    detail: str = ""


@dataclass(frozen=True)
class Receipt:
    """Raw result of executing one check."""

    check_id: str
    status: ReceiptStatus
    command: str
    exit_code: int | None
    duration_s: float
    invariants: tuple[Invariant, ...] = ()
    measurements: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["invariants"] = [asdict(i) for i in self.invariants]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Receipt:
        return cls(
            check_id=data["check_id"],
            status=data["status"],
            command=data["command"],
            exit_code=data["exit_code"],
            duration_s=float(data["duration_s"]),
            invariants=tuple(Invariant(**i) for i in data.get("invariants", [])),
            measurements=dict(data.get("measurements", {})),
            reason=data.get("reason", ""),
        )


def validate_receipt(receipt: Receipt) -> list[str]:
    """Return every reason this receipt cannot be trusted as stated."""
    problems: list[str] = []
    cid = receipt.check_id
    if receipt.status == "passed":
        if receipt.exit_code != 0:
            problems.append(f"{cid}: marked passed with exit code {receipt.exit_code}")
        if not receipt.command:
            problems.append(f"{cid}: marked passed without an executed command")
        if not receipt.invariants:
            problems.append(f"{cid}: marked passed without any asserted invariant")
        failed = [i.name for i in receipt.invariants if not i.held]
        if failed:
            problems.append(f"{cid}: marked passed but invariants failed: {failed}")
    elif receipt.status in ("skipped", "unmeasured") and not receipt.reason:
        problems.append(f"{cid}: {receipt.status} without a stated reason")
    return problems


def cost_per_success(total_cost_usd: Decimal | None, successes: int) -> Decimal | None:
    """Total attempted cost divided by successful tasks; undefined when none succeed."""
    if total_cost_usd is None or successes <= 0:
        return None
    return total_cost_usd / successes


@dataclass(frozen=True)
class ReadinessSummary:
    """Derived view of a receipt set against the required checks."""

    required: tuple[str, ...]
    receipts: tuple[Receipt, ...]
    environment: dict[str, str]

    @property
    def by_id(self) -> dict[str, Receipt]:
        return {r.check_id: r for r in self.receipts}

    @property
    def missing(self) -> list[str]:
        return [cid for cid in self.required if cid not in self.by_id]

    @property
    def problems(self) -> list[str]:
        found = [p for r in self.receipts for p in validate_receipt(r)]
        ids = [r.check_id for r in self.receipts]
        found += [
            f"{cid}: duplicate receipt" for cid in sorted({i for i in ids if ids.count(i) > 1})
        ]
        return found

    @property
    def unverified(self) -> list[str]:
        """Required checks that exist but did not pass (failed, skipped or unmeasured)."""
        return [
            cid for cid in self.required if cid in self.by_id and self.by_id[cid].status != "passed"
        ]

    @property
    def ready(self) -> bool:
        return not self.missing and not self.problems and not self.unverified

    def assert_consistent(self) -> None:
        """Raise when the evidence itself is untrustworthy (not merely failing)."""
        issues = self.problems + [f"{cid}: required receipt missing" for cid in self.missing]
        if issues:
            raise EvidenceError("; ".join(issues))

    def to_markdown(self) -> str:
        lines = [
            "# Local readiness matrix",
            "",
            "Generated from raw receipts; edit the receipts' sources, not this file.",
            "",
            "| Environment | Value |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in sorted(self.environment.items())],
            "",
            f"**Overall: {'ready' if self.ready else 'NOT ready'}** "
            f"({sum(r.status == 'passed' for r in self.receipts)} passed, "
            f"{len(self.unverified)} required not passing, {len(self.missing)} missing)",
            "",
            "| Check | Status | Exit | Duration (s) | Invariants | Notes |",
            "|---|---|---|---|---|---|",
        ]
        for cid in self.required:
            r = self.by_id.get(cid)
            if r is None:
                lines.append(f"| {cid} | MISSING | – | – | – | no receipt |")
                continue
            held = sum(i.held for i in r.invariants)
            note = r.reason or ", ".join(f"{k}={v}" for k, v in r.measurements.items())
            lines.append(
                f"| {cid} | {r.status} | {r.exit_code if r.exit_code is not None else '–'} "
                f"| {r.duration_s:.1f} | {held}/{len(r.invariants)} | {note} |"
            )
        if self.problems:
            lines += ["", "## Evidence problems", "", *[f"- {p}" for p in self.problems]]
        return "\n".join(lines) + "\n"


def environment_profile() -> dict[str, str]:
    from nexura_runtime import __version__

    return {
        "nexura_runtime": __version__,
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
    }


def summarize(
    receipts: list[Receipt], required: list[str], environment: dict[str, str] | None = None
) -> ReadinessSummary:
    return ReadinessSummary(
        required=tuple(required),
        receipts=tuple(receipts),
        environment=environment if environment is not None else environment_profile(),
    )


def write_receipts(path: Path, receipts: list[Receipt]) -> None:
    payload = {"schema_version": 1, "receipts": [r.to_dict() for r in receipts]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_receipts(path: Path) -> list[Receipt]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise EvidenceError(f"Unsupported receipt schema version {data.get('schema_version')!r}")
    return [Receipt.from_dict(item) for item in data["receipts"]]
