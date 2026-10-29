# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""The service's credential store: env-var references plus the local encrypted secrets file.

``create_api_app`` calls :func:`install_local_credential_store` once, before any router or
extension is configured. The result is ``app.state.credential_store`` (a
``ScopedCredentialStore``) that ``/api/v1/credentials``, templates, the builder and runs use:

* With ``NEXURA_MASTER_KEY`` set, credentials are kept in the ``nexura_secrets`` table of the
  service's SQLite file, envelope encrypted (``nexura_runtime.secrets.EncryptedSecretStore``),
  and survive a restart. In Postgres mode (``database_url``, MVP WP5) the same table, with the
  same envelope format, is in the shared database (``nexura_postgres.PostgresSecretStore``). A
  different key than the one that protects stored secrets fails closed with
  ``MasterKeyMismatchError``.
* Without it, in local mode, credentials live in process memory (as before).
* Credential references that name an ``environment_variable`` fall back to that variable.
* In the ``authenticated-shared-service`` profile the service refuses to start without a master
  key (``MasterKeyMissingError``) unless the loud
  ``NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS=I_UNDERSTAND_THIS_IS_UNSAFE`` opt-out is set.

This wiring used to be part of the organisations mount. Organisations, the hosted secrets
admin API and master-key rotation moved to the closed edition (EDITIONS_PLAN step 5); the
local encrypted file stays in the open-source edition (founder decision 2026-10-18).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nexura_runtime.credentials.store import ScopedCredentialStore


def install_local_credential_store(
    state: Any, *, profile: str, db_path: str | Path | None, stores: Any = None
) -> ScopedCredentialStore:
    """Set ``state.credential_store``, ``state.secret_store`` and ``state.master_key_provider``.

    ``state.secret_store`` is the ``EncryptedSecretStore`` (or ``None`` when no master key is
    configured); ``state.master_key_provider`` is the provider read from ``NEXURA_MASTER_KEY``.
    ``stores`` is the app's ``ServiceStores``: with a Postgres ``database_url`` the secrets are a
    ``PostgresSecretStore`` on its shared database; otherwise the SQLite file at ``db_path``.
    """
    from nexura_runtime.secrets.keys import master_key_provider_from_env

    provider = master_key_provider_from_env(required=profile == "authenticated-shared-service")
    state.master_key_provider = provider
    state.secret_store = None
    if provider is None:
        if stores is not None and stores.database_url:
            # Shared database, but no key to encrypt credentials with: they stay in this
            # process (per instance, lost on restart). The shared profile never gets here.
            logging.getLogger("nexura_runtime.service").warning(
                "Postgres mode without NEXURA_MASTER_KEY: credentials are kept in this process "
                "only (not shared with other instances, lost on restart). Set NEXURA_MASTER_KEY "
                "to keep them encrypted in the shared database"
            )
        store = ScopedCredentialStore()
    else:
        if stores is not None and stores.database_url:
            state.secret_store = stores.secrets(provider)
        else:
            from nexura_runtime.secrets.store import EncryptedSecretStore

            state.secret_store = EncryptedSecretStore(provider, db_path)
        store = ScopedCredentialStore(state.secret_store)
    state.credential_store = store
    return store


__all__ = ["install_local_credential_store"]
