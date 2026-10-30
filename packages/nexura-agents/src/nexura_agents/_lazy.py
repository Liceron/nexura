# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Deferred stdlib module proxy used to keep ``import nexura_agents`` fast."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


class LazyModule:
    """Proxy that imports the named module on first attribute access.

    Attribute reads, writes and deletes are forwarded to the real module, so
    monkeypatching through the proxy behaves like patching the module itself.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def _module(self) -> ModuleType:
        return importlib.import_module(object.__getattribute__(self, "_name"))

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._module(), attr)

    def __setattr__(self, attr: str, value: Any) -> None:
        setattr(self._module(), attr, value)

    def __delattr__(self, attr: str) -> None:
        delattr(self._module(), attr)

    def __dir__(self) -> list[str]:
        return dir(self._module())
