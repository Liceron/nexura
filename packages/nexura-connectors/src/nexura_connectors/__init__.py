# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Connectors pack: 15 integrations as SDK modules with approval-gated writes.

Distributed as ``nexura-connectors`` (it depends on ``nexura-runtime``). Importing this package
registers every connector on ``default_registry`` (with the real network transport); the
runtime imports it when it is installed (``nexura_runtime.modules.optional_packages``).
Credentials are always references into the secrets store.
"""

from __future__ import annotations

from nexura_agents.contracts.base import BaseModule

from nexura_connectors._core import (
    ConnectorHttp,
    ConnectorModule,
    Op,
    StrictParams,
    redact,
)
from nexura_connectors.airtable import AirtableConnector
from nexura_connectors.errors import (
    ApprovalRequiredError,
    AuthFailedError,
    ConnectorDependencyError,
    ConnectorError,
    InvalidParamsError,
    MissingCredentialError,
    RateLimitedError,
    ResponseTooLargeError,
    SsrfBlockedError,
    UpstreamError,
)
from nexura_connectors.gdrive import GoogleDriveConnector
from nexura_connectors.github import GitHubConnector
from nexura_connectors.gsheets import GoogleSheetsConnector
from nexura_connectors.jira import JiraConnector
from nexura_connectors.mail import (
    FakeImapClient,
    FakeSmtpClient,
    ImapConnector,
    SmtpConnector,
)
from nexura_connectors.notion import NotionConnector
from nexura_connectors.s3 import S3Connector, sigv4_headers
from nexura_connectors.slack import SlackConnector
from nexura_connectors.sql import (
    InMemorySqlDriver,
    MySqlConnector,
    PostgresConnector,
    SqlDriver,
    SqlRows,
)
from nexura_connectors.webhooks import (
    DiscordConnector,
    TeamsConnector,
    WebhookConnector,
)
from nexura_runtime.modules.registry import VersionedModuleRegistry, default_registry

CONNECTOR_CLASSES: tuple[type[ConnectorModule], ...] = (
    WebhookConnector,
    SlackConnector,
    SmtpConnector,
    ImapConnector,
    GoogleDriveConnector,
    GoogleSheetsConnector,
    PostgresConnector,
    MySqlConnector,
    S3Connector,
    GitHubConnector,
    NotionConnector,
    JiraConnector,
    AirtableConnector,
    DiscordConnector,
    TeamsConnector,
)


def register_connectors(registry: VersionedModuleRegistry) -> list[str]:
    """Register every connector on ``registry`` (skipping ones already present)."""
    registered: list[str] = []
    for cls in CONNECTOR_CLASSES:
        manifest = cls.build_manifest()
        try:
            registry.get_entry(manifest.module_id, manifest.version)
        except Exception:
            module: BaseModule = cls()
            registry.register(module)
            registered.append(manifest.module_id)
    return registered


register_connectors(default_registry)

__all__ = [
    "AirtableConnector",
    "ApprovalRequiredError",
    "AuthFailedError",
    "CONNECTOR_CLASSES",
    "ConnectorDependencyError",
    "ConnectorError",
    "ConnectorHttp",
    "ConnectorModule",
    "DiscordConnector",
    "FakeImapClient",
    "FakeSmtpClient",
    "GitHubConnector",
    "GoogleDriveConnector",
    "GoogleSheetsConnector",
    "ImapConnector",
    "InMemorySqlDriver",
    "InvalidParamsError",
    "JiraConnector",
    "MissingCredentialError",
    "MySqlConnector",
    "NotionConnector",
    "Op",
    "PostgresConnector",
    "RateLimitedError",
    "ResponseTooLargeError",
    "S3Connector",
    "SlackConnector",
    "SmtpConnector",
    "SqlDriver",
    "SqlRows",
    "SsrfBlockedError",
    "StrictParams",
    "TeamsConnector",
    "UpstreamError",
    "WebhookConnector",
    "redact",
    "register_connectors",
    "sigv4_headers",
]
