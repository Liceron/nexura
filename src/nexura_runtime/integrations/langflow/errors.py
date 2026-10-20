# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Errors raised by the Langflow flow importer. All derive from ``LangflowImportError``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .flow import ImportReport


class LangflowImportError(ValueError):
    """Base class for every importer failure."""


class FlowFormatError(LangflowImportError):
    """The input is not a valid saved-flow document; ``problems`` lists every defect found."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        self.problems = list(problems or [])
        detail = "".join(f"\n  - {p}" for p in self.problems)
        super().__init__(f"{message}{detail}")


class FlowTooLargeError(FlowFormatError):
    """The input exceeds a configured size limit (bytes, nodes or edges)."""


class UnsupportedFlowError(LangflowImportError):
    """The flow needs components or wiring the importer does not support."""

    def __init__(self, message: str, report: ImportReport | None = None) -> None:
        self.report = report
        super().__init__(message)


class FlowBuildError(LangflowImportError):
    """The imported flow cannot be turned into a runnable object."""


class TweakRefusedError(LangflowImportError):
    """One or more tweaks were refused; ``refused`` holds ``(key, reason)`` for all of them."""

    def __init__(self, refused: list[tuple[str, str]]) -> None:
        self.refused = list(refused)
        detail = "".join(f"\n  - {key}: {reason}" for key, reason in self.refused)
        super().__init__(f"{len(self.refused)} tweak(s) refused:{detail}")


class MissingVariablesError(LangflowImportError):
    """The flow references global variables (``load_from_db``) the caller did not supply."""

    def __init__(self, names: list[str]) -> None:
        self.names = sorted(set(names))
        super().__init__(
            "Missing variables (pass them via variables={...}): " + ", ".join(self.names)
        )


class ModelMappingError(LangflowImportError):
    """A Langflow model/provider has no SDK mapping and no caller-supplied model."""


class ComponentRefusedError(LangflowImportError):
    """An extended (V3) component refused an operation: size cap, path, SQL policy, loop bound."""
