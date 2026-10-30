# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""In-tree ``ServiceExtension``s: none at the moment.

``create_api_app`` configures the list returned by :func:`builtin_extensions` with
``in_tree=True``, at the point where the in-tree extensions used to be mounted. The seam stays so
that a feature shipped in this package can plug in the same way as any other extension.

Publishing (publication routes and the public chat gateway) moved to the closed edition in
EDITIONS_PLAN step 4. Organisations and the hosted secrets admin API moved in step 5. The local
encrypted secrets file stays open source and is installed by ``create_api_app`` itself
(``nexura_runtime.service.api.local_secrets``).
"""

from __future__ import annotations

from nexura_runtime.service.extensions import ServiceExtension


def builtin_extensions() -> list[ServiceExtension]:
    """The in-tree extensions, in the order ``create_api_app`` configures them (none)."""
    return []


#: Names reserved by the in-tree extensions.
BUILTIN_EXTENSION_NAMES = frozenset(ext.name for ext in builtin_extensions())

__all__ = [
    "BUILTIN_EXTENSION_NAMES",
    "builtin_extensions",
]
