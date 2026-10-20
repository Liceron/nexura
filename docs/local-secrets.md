# Credentials and the local encrypted secrets file

Workflows and connectors never carry a secret in their config. They name a credential
(`credential_ref`, `CredentialReference(credential_id=...)`) and the `CredentialResolver` in the
module's `ExecutionContext` resolves it inside the process, for the outbound call only. Model
providers read their own API key variables (see [Models and providers](providers.md)). This page covers where those
values come from in the open-source edition:

- **Environment-variable references.** A reference that names an `environment_variable` falls
  back to that variable when the workspace has no stored credential of that id.
- **In-memory credentials.** Without a master key, credentials created through
  `POST /api/v1/credentials` (or Studio's Credentials page) live in process memory.
- **The local encrypted secrets file.** With `NEXURA_MASTER_KEY` set, the same credentials are
  stored envelope encrypted in the service's SQLite file and survive a restart.

Organisations, the masked `/api/v1/secrets` admin API and master-key rotation are part of the
closed edition (`nexura-enterprise`, moved there in EDITIONS_PLAN step 5); the open-source
service answers 404 on `/api/v1/organisations` and `/api/v1/secrets`. Everything on this page
works without it.

## Resolving a credential

```python
import os
import tempfile
from pathlib import Path

from nexura_agents.contracts import CredentialReference
from nexura_runtime.credentials import ScopedCredentialStore
from nexura_runtime.secrets import (
    EncryptedSecretStore,
    StaticMasterKeyProvider,
    generate_master_key,
)

with tempfile.TemporaryDirectory() as tmp:
    key = StaticMasterKeyProvider.from_text(generate_master_key())
    db = Path(tmp) / "service.sqlite3"
    store = ScopedCredentialStore(EncryptedSecretStore(key, db))
    meta = store.store("default", "github", "github_token", "ghp_example_0123456789abcdef")
    print(meta.masked_hint)  # bullets plus the last four characters
    assert b"ghp_example" not in db.read_bytes()  # encrypted at rest

    # After a restart, the same key opens the file again.
    reopened = ScopedCredentialStore(EncryptedSecretStore(key, db))
    resolver = reopened.for_workspace("default")  # a CredentialResolver for modules
    assert resolver.resolve("github") == "ghp_example_0123456789abcdef"

    # An environment-variable reference, with nothing stored under that id.
    os.environ["NEXURA_DOCS_SLACK_TOKEN"] = "xoxb-example-token-value"
    ref = CredentialReference(
        credential_id="slack",
        credential_type="slack_bot_token",
        environment_variable="NEXURA_DOCS_SLACK_TOKEN",
    )
    assert resolver.resolve(ref) == "xoxb-example-token-value"
```

`ScopedCredentialStore` always enforces workspace isolation (a reference to another
workspace is refused), revocation (a revoked credential fails closed) and redaction (a
`SecretValue` prints as `***REDACTED***`). Connectors also check the stored credential type
against the type they need. See [Connectors](connectors.md).

## The service

`create_api_app` installs the credential store once, at startup:

| Profile | `NEXURA_MASTER_KEY` | Credentials are kept |
| --- | --- | --- |
| `local-owner` | unset | in memory, gone on restart |
| `local-owner` | set | encrypted in the service's SQLite file (`nexura_secrets` table, file mode 0600) |
| `authenticated-shared-service` | unset | the service refuses to start (`MasterKeyMissingError`) |
| `authenticated-shared-service` | set | encrypted in the service's SQLite file |

The only way to run the shared profile without a key is the loudly named
`NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS=I_UNDERSTAND_THIS_IS_UNSAFE` opt-out, which keeps
credentials in memory only. `app.state.secret_store` is the `EncryptedSecretStore` (or `None`)
and `app.state.credential_store` the `ScopedCredentialStore` in front of it.

Manage credentials with `GET/POST /api/v1/credentials`, `GET/DELETE /api/v1/credentials/{id}`,
`POST /api/v1/credentials/{id}/verify` and `/revoke`. Responses carry metadata and a masked
hint only, never the value. Listing and reading need the `edit` permission.

## Encryption

Each secret gets its own random 256-bit data key (AES-256-GCM). The data key is wrapped by the
master key, and both ciphertexts are bound to the workspace and the credential id, so a row
cannot be moved to another id or workspace. Starting with a different key than the one that
protects stored secrets fails closed (`MasterKeyMismatchError`); the error never contains secret
material.

Master key. `NEXURA_MASTER_KEY` holds 32 random bytes as url-safe base64; create one with
`nexura_runtime.secrets.generate_master_key()`. Key text is decoded strictly: only the url-safe
alphabet (`A-Z a-z 0-9 - _`) with optional trailing `=`, exactly 32 bytes; standard `+` and `/`,
whitespace inside the text and any other character are rejected with `MasterKeyInvalidError`,
which never echoes the text. Keep the key outside the database directory and back it up: without
it the stored credentials cannot be read.

The store uses the `cryptography` package, a core dependency of `nexura-runtime` since 0.4.0
(the `secrets` extra still exists so old install commands work, but adds nothing). If it cannot
be imported, which means a broken install, the store raises `SecretsUnavailableError`.

## Plugging in another backend

`ScopedCredentialStore(secret_store=...)` accepts any `CredentialBackend` (the protocol in
`nexura_runtime.credentials.backends`); `EncryptedSecretStore` is one. The master key is any
`MasterKeyProvider` (`key_id`, `wrap`, `unwrap`), so a key held elsewhere can wrap the data keys
instead of `NEXURA_MASTER_KEY`. See [Extending the service and Studio](extensions.md).

## Limits

There is no master-key rotation in the open-source edition. The API never returns a stored
value, so to change the key, start with the new key on a fresh database and create the
credentials again from their sources (the closed edition re-wraps them in place). No external
review has been done.
