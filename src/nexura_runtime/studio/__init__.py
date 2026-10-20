# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Studio support shared with the REST service: run store, built-in modules, traces, security.

The Studio launcher (the ``nexura-studio`` command, its FastAPI app and web page) is the
separate ``nexura-studio`` package (import ``nexura_studio``), which depends on this runtime:

    pip install nexura-studio

``create_app`` here builds that app when the package is installed and otherwise raises
``MissingDependencyError`` naming ``pip install nexura-studio``.
"""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Create the Studio FastAPI app from the ``nexura-studio`` package (see its ``create_app``)."""
    from nexura_runtime.modules.optional_packages import STUDIO_PACKAGE, require_package

    studio = require_package(STUDIO_PACKAGE, "Nexura Studio")
    return studio.create_app(*args, **kwargs)


__all__ = ["create_app"]
