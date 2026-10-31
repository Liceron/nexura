# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Features shipped in separate packages, loaded when those packages are installed.

Each package below depends on this runtime; the runtime never depends on them. This is the
only place in ``nexura_runtime`` that imports them, so a missing package always produces the
same message: "... is provided by the '<package>' package, which is not installed. Install it
with: pip install <package>".

- ``nexura-connectors`` (``nexura_connectors``): the 15 SaaS connectors
  (``nexura.connectors.*``). Importing it registers every connector on ``default_registry``.
  Without it, the connector module ids stay registered as unavailable, so workflows and Studio
  report "pip install nexura-connectors" instead of an unknown module or an ``ImportError``.
- ``nexura-postgres`` (``nexura_postgres``): PostgreSQL storage (``get_storage_repository``
  with a ``postgresql://`` URI) and the pgvector store.
- ``nexura-qdrant`` (``nexura_qdrant``): the Qdrant vector store.
- ``nexura-vectorstores`` (``nexura_vectorstores``): HTTP adapters for Chroma, Pinecone,
  Weaviate, Milvus, Elasticsearch and OpenSearch.
- ``nexura-sandbox`` (``nexura_sandbox``): the bubblewrap, gVisor and WASI sandbox backends
  (``build_backend``); without it they probe as unavailable and Monty stays the default.
- ``nexura-studio`` (``nexura_studio``): the ``nexura-studio`` launcher and its web page.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import NamedTuple

from nexura_agents.contracts import MissingDependencyError, ModuleManifest

from nexura_runtime.modules.registry import VersionedModuleRegistry


class OptionalPackage(NamedTuple):
    """A separately installed package: its distribution name and its import name."""

    distribution: str
    import_name: str


POSTGRES_PACKAGE = OptionalPackage("nexura-postgres", "nexura_postgres")
QDRANT_PACKAGE = OptionalPackage("nexura-qdrant", "nexura_qdrant")
VECTORSTORES_PACKAGE = OptionalPackage("nexura-vectorstores", "nexura_vectorstores")
SANDBOX_PACKAGE = OptionalPackage("nexura-sandbox", "nexura_sandbox")
STUDIO_PACKAGE = OptionalPackage("nexura-studio", "nexura_studio")


def package_missing_message(feature: str, package: OptionalPackage) -> str:
    """The message for ``feature`` when ``package`` is not installed."""
    return (
        f"{feature} is provided by the '{package.distribution}' package, which is not "
        f"installed. Install it with: pip install {package.distribution}"
    )


def find_package(package: OptionalPackage) -> ModuleType | None:
    """Import ``package`` if it is installed, else return None.

    Only the package itself being absent counts as "not installed": an ``ImportError`` raised
    from inside an installed package (a broken install) propagates.
    """
    try:
        return importlib.import_module(package.import_name)
    except ModuleNotFoundError as exc:
        if exc.name != package.import_name:
            raise
        return None


def require_package(package: OptionalPackage, feature: str) -> ModuleType:
    """Import ``package`` or raise ``MissingDependencyError`` naming ``pip install <package>``."""
    module = find_package(package)
    if module is None:
        raise MissingDependencyError(
            package.distribution, message=package_missing_message(feature, package)
        )
    return module


CONNECTORS_PACKAGE = "nexura-connectors"
CONNECTORS_IMPORT = "nexura_connectors"
#: Version every connector manifest declares (``ConnectorModule.build_manifest``).
CONNECTORS_MODULE_VERSION = "1.0.0"
#: module id -> title of each connector in ``nexura-connectors`` (same titles as its manifests).
CONNECTOR_MODULES: dict[str, str] = {
    "nexura.connectors.webhook": "Webhook (outbound)",
    "nexura.connectors.slack": "Slack",
    "nexura.connectors.smtp": "Email (SMTP send)",
    "nexura.connectors.imap": "Email (IMAP read)",
    "nexura.connectors.gdrive": "Google Drive",
    "nexura.connectors.gsheets": "Google Sheets",
    "nexura.connectors.postgres": "Postgres",
    "nexura.connectors.mysql": "MySQL",
    "nexura.connectors.s3": "S3-compatible storage",
    "nexura.connectors.github": "GitHub",
    "nexura.connectors.notion": "Notion",
    "nexura.connectors.jira": "Jira",
    "nexura.connectors.airtable": "Airtable",
    "nexura.connectors.discord": "Discord (webhook)",
    "nexura.connectors.teams": "Microsoft Teams (incoming webhook)",
}


def connector_placeholder_manifest(module_id: str) -> ModuleManifest:
    """Manifest of a connector whose package is not installed."""
    return ModuleManifest(
        schema_version=1,
        module_id=module_id,
        version=CONNECTORS_MODULE_VERSION,
        kind="integration",
        title=CONNECTOR_MODULES[module_id],
        description=(
            f"Provided by the {CONNECTORS_PACKAGE} package, which is not installed. "
            f"Install it with: pip install {CONNECTORS_PACKAGE}"
        ),
        capabilities={"network": True, "streaming": False},
    )


def load_connectors(registry: VersionedModuleRegistry) -> bool:
    """Import ``nexura_connectors`` (it registers on ``default_registry``) if installed.

    Returns True when the package is importable. Otherwise registers each connector id on
    ``registry`` as unavailable and returns False. An ``ImportError`` raised from inside an
    installed ``nexura_connectors`` is not hidden.
    """
    try:
        import nexura_connectors  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != CONNECTORS_IMPORT:
            raise
    else:
        return True
    for module_id in CONNECTOR_MODULES:
        if not registry.has(module_id, CONNECTORS_MODULE_VERSION):
            registry.register_unavailable(
                connector_placeholder_manifest(module_id), CONNECTORS_PACKAGE
            )
    return False
