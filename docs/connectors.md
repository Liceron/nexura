# Connectors

The 15 integrations ship as their own package, `nexura-connectors` (import `nexura_connectors`),
which depends on `nexura-runtime`:

```bash
pip install nexura-connectors              # every connector; Postgres needs a driver:
pip install "nexura-connectors[postgres]"  # + asyncpg
```

When it is installed, the runtime imports it and every connector is registered on
`default_registry` under `nexura.connectors.<name>` (importing `nexura_connectors` yourself does
the same). Without it, `nexura-runtime` still imports and runs: the `nexura.connectors.*` module
ids stay listed but unavailable, and a workflow that uses one is refused with
"provided by the 'nexura-connectors' package, which is not installed. Install it with:
pip install nexura-connectors". In this repository it is the workspace member
`packages/nexura-connectors`, installed by `uv sync`.

## What was already there

| Need | Existing module | Decision |
| --- | --- | --- |
| HTTP request | `nexura.http.request` | reused as is (its destination policy is the model for the connector guard) |
| SQL | `nexura.sql.query` / `schema` / `execute` | kept; the Postgres and MySQL connectors add a driver protocol and credential-by-reference |
| Email, WhatsApp channels | `integrations/channels` (outbox) | kept; SMTP send and IMAP read are new |
| The other 11 (webhook out, Slack, Drive, Sheets, S3, GitHub, Notion, Jira, Airtable, Discord, Teams) | none | built here |

## Operations

`W` operations write to an external system and are never retried. They fail with
`APPROVAL_REQUIRED` unless the execution context carries `approval_granted=True`.
The `approved` input is still accepted for compatibility but **grants nothing**: inputs come
from the flow spec or from model tool-call arguments, so they cannot approve themselves.
Who sets the context flag:

- The workflow engine pauses before a node whose module declares write operations when the
  operation it will receive (from the node input mapping and run state) is a write, or cannot
  be determined (conservative), and sets the flag only after a human approves the resume.
  Read operations are not paused. Map and parallel steps pause if the module has any write.
  An approval is bound to the inputs the approver saw: if `resume(..., state_update=...)`
  changes anything a module node reads (its resolved `input_mapping` inputs, so the operation
  and params, or a mapped step's items), the node is gated again and the new inputs need a
  fresh approval; the flag is only set for a node whose inputs are unchanged. The same
  binding applies to every approval-gated node kind (see [Workflows](workflows.md#approval-gates)),
  the approval expires (`approval_ttl`, enforced when it is claimed and again before the node
  runs), and answering an `interrupt()` from a node does not set the flag for a node that
  was never approved.
  Python code that builds an `ExecutionContext(approval_granted=True)` itself is trusted
  and bypasses this gate, so keep such code out of reach of model or flow input. One approval of
  a mapped write step covers all items: `pending_approval.details` lists `item_count`, the
  resolved `items` and an `items_digest`, and a list longer than
  `WorkflowEngine(max_map_writes_per_approval=50)` is refused before it pauses. The flag is
  reset after every node.
- `module_tool` (agent tools) requires the agent runner's tool approval for write calls and
  sets the flag only for a call a human approved. A model cannot approve its own call.
- The same rule covers every module with external side effects, not only connectors: a
  manifest declares `write_operations` (keyed on the input named by `write_operation_key`,
  for example `method` for `nexura.http.request`) or `always_write=True`. That covers
  `nexura.http.request` (POST, PUT, DELETE, PATCH, OPTIONS), `nexura.channel.email`
  (`send_email`), `nexura.channel.whatsapp` (`send_message`), `nexura.sql.execute` and every
  OpenAPI operation module that is not GET or HEAD. Their `approved` input grants nothing.
  A dry run is still a write call: `dry_run` is an input a model or flow author can set, so
  `send_message` and `send_email` with `dry_run: true` need the same approval.
- Operators calling a connector directly in Python build
  `ExecutionContext(..., approval_granted=True)`. The module manifest lists
  `write_operations`, and `GET /api/v1/modules` reports `requires_approval` and
  `write_operations` for every connector with writes (Studio's Approval Gate badge). Reads retry (429, 5xx, timeouts) up to
`max_retries` (default 2, maximum 3). GitHub, Slack, Drive and Sheets honour the full
`Retry-After`/reset delay in sleep intervals of at most five seconds; other connectors
retain their five-second delay cap. See the provider sections below.

| Connector (`nexura.connectors.`) | Operations | Credential type (by reference) | Limits |
| --- | --- | --- | --- |
| `webhook` | `send` W | `webhook_url` (or a non-secret `url` param) | SSRF policy, forbidden headers dropped |
| `slack` | `post_message` W, `list_channels`, `read_history` | `slack_bot_token` | `limit` at most 200, `max_pages` |
| `smtp` | `send_message` W | `smtp_login` (`user:password`) | 50 recipients, 200 KB body, header injection refused |
| `imap` | `list_messages` | `imap_login` (`user:password`) | mailbox opened read-only, 50 messages, 1 MB headers |
| `gdrive` | `list_files`, `get_file`, `download_file`, `upload_text` W | `google_oauth_token` | page size 100, 1 MB upload |
| `gsheets` | `read_range`, `append_rows` W, `update_range` W | `google_oauth_token` | 1000 rows per write |
| `postgres` | `query`, `execute` W | `database_dsn` | one statement, read-only queries, 5000 rows |
| `mysql` | `query`, `execute` W | `database_dsn` | as Postgres; driver is an optional extra |
| `s3` | `list_objects`, `get_object`, `put_object` W, `delete_object` W | `s3_access_key` (`id:secret`) | SigV4, 5 MB upload, 1000 keys per page |
| `github` | `get_repo`, `list_issues`, `list_pulls`, `create_issue` W, `create_comment` W | `github_token` | page and size caps |
| `notion` | `query_database`, `get_page`, `create_page` W, `append_blocks` W | `notion_token` | `max_pages` |
| `jira` | `search`, `get_issue`, `create_issue` W, `add_comment` W | `jira_basic` (`email:token`) | `base_url` required |
| `airtable` | `list_records`, `get_record`, `create_records` W, `update_records` W | `airtable_token` | batch and page caps |
| `discord` | `send_message` W | `discord_webhook_url` | 2000 characters, mentions disabled, vendor hosts only |
| `teams` | `send_message` W | `teams_webhook_url` | vendor hosts only |

Every connector takes the same config keys: `credential_ref` (an id in the secrets store, never a
token), `base_url`, `timeout_seconds` (clamped to 0.1-120), `max_response_bytes` (default 2 MB,
hard cap 10 MB), `max_retries` (0-3) and `max_pages` (default 3, hard cap 10). These limits are
clamped when the connector runs, even if config schema validation was skipped.

`base_url` is honoured only when its host is the connector's default API host or is on the
operator allow-list; otherwise the call fails with `SSRF_BLOCKED` before any credential is sent.
The operator allow-list is supplied outside flow config: the connector constructor
(`GitHubConnector(allowed_hosts=[...])`), the execution context
(`context.attributes["connector_allowed_hosts"]`) and the `NEXURA_CONNECTOR_ALLOWED_HOSTS`
environment variable (comma separated). A flow-config `allowed_hosts` key is ignored. Hosts on the
operator list (Jira site, S3-compatible endpoint, GitHub Enterprise, webhook vendors) may also reach
RFC1918 and loopback addresses, never the blocked ranges below.

Every operation that returns `next_cursor` accepts an input named `cursor` to resume from it. The
older names (`offset`, `page_token`, `start_cursor`, `continuation_token`, Jira `start_at`) remain
as aliases; passing both with different values is an error. GitHub cursors are absolute URLs and
are only followed when they stay on the API origin and this repository's list endpoint.

## Refreshable authentication

GitHub, Slack, Google Drive and Google Sheets still accept a plain PAT, bot token or
access token in the existing secret store. To enable refresh, store a **JSON secret**
under the same credential type and pass only its `credential_ref` in flow config:

```json
{
  "auth_type": "oauth2",
  "access_token": "example-access-token-not-real",
  "expires_at": 1800000000,
  "refresh_token": "example-refresh-token-not-real",
  "client_id": "example-client-id",
  "client_secret": "example-client-secret-not-real"
}
```

`expires_at` is a Unix timestamp in seconds. Missing expiry or access token triggers
refresh; a 30-second safety margin avoids sending a nearly expired token. Each page
and each read retry after backoff re-resolves the credential and checks expiry.
Refresh uses a form-encoded POST to the
provider endpoint, tracks `expires_in`, preserves an omitted refresh token, and stores
rotated refresh tokens before sending an API operation. Providers that return
`refresh_token_expires_in` also update `refresh_expires_at`; an expired refresh token
requires re-authorization. A GET/HEAD rejected with HTTP 401 forces one refresh and
one replay, even when the access token has not expired. This recovery happens at most
once per invocation; static tokens and writes are never refreshed/replayed on a 401.

Use `WorkspaceCredentialResolver(ScopedCredentialStore(...), workspace_id)` with the
existing secret backend (including the encrypted backend for persistent storage).
The default `ScopedCredentialStore()` is memory-only. Credential type, provider and
description remain intact. Refreshable records must not use environment fallbacks.
A custom resolver can implement synchronous `update_secret(ref, secret)` to atomically
replace its stored JSON. Without a writable store/hook, refresh fails before consuming
the refresh token. Plain tokens and unexpired records still work with read-only resolvers.

Concurrent callers sharing a store, workspace and credential serialise refreshes with
a process-wide thread lock, including callers on different threads/event loops and
Drive and Sheets sharing a Google credential. Within one event loop callers join one
refresh task. Cancellation
of one waiter does not cancel the shared exchange. This is **not a distributed lock**:
use a single refresh owner for a rotating credential across worker processes. Token
exchange POSTs are never retried automatically. Token-endpoint timeouts, transport
failures and 5xx return retryable `UPSTREAM_UNAVAILABLE`; retry later, and re-authorize
if an uncertain exchange rotated the credential. Rejected exchanges and persistence
failures return `AUTH_FAILED`. Store checks prevent refresh from restoring a credential
changed during the exchange, but are not an atomic cross-process compare-and-swap:
independent store instances and external credential writers need an exclusive owner.
Token-endpoint throttling returns `RATE_LIMITED` without retrying the exchange.
An in-flight refresh does not overwrite a revoked/deleted or replaced stored credential.

Refresh endpoints are fixed by default. A secret record's optional `token_url` can
point at an enterprise OAuth endpoint only on an operator-allowed host; HTTPS, DNS
pinning, response caps and no-redirect rules still apply. Token endpoints cannot have
userinfo, query strings or fragments. Client secrets and tokens stay in the credential
record and request body/Authorization header; they are never emitted to connector logs.
Known original and refreshed values are scrubbed from outputs and errors. Provider
error bodies are omitted in favour of status-specific, actionable error messages.
The credential fields that are scrubbed as known values are the secret ones (access and
refresh tokens, client secrets, JWTs and private keys); auth type, client ID, token type
and scope words are not treated as secrets. As for every connector, outputs also pass
through the generic pattern redaction (bearer tokens, API keys, `password=` pairs, card
numbers and similar), so text matching those patterns is masked.

## GitHub: auth, pagination and rate limits

- Auth: plain `github_token` credentials support PATs and existing installation tokens.
  OAuth records refresh at `https://github.com/login/oauth/access_token`; only GitHub
  token types that actually issue refresh tokens can use this mode.
- GitHub App mode stores `{"auth_type":"github_app","installation_id":123}` as the
  secret. Supply `GitHubConnector(app_jwt_provider=...)` with a trusted synchronous
  callable returning a freshly signed App JWT (run via `asyncio.to_thread`), or store
  a short-lived `app_jwt` for one initial exchange in the
  record. The connector exchanges it at `/app/installations/{id}/access_tokens`,
  persists the installation token and Unix expiry, and renews it when needed. Expiry
  responses with an offset are honoured; a naive `expires_at` is interpreted as UTC.
  The consumed JWT is removed from the persisted record. JWT signing/key management
  stays with the application; automatic renewal needs a provider returning fresh JWTs.
  Enterprise API bases use the existing operator allow-list.
- Pagination: `list_issues` and `list_pulls` follow Link `rel="next"`, with
  `per_page` 1–100 and optional `max_items` 1–10000. Every followed URL must match
  the API origin and either `/repos/{owner}/{repo}/{kind}` or GitHub's documented
  `/repositories/{numeric_id}/{kind}` form, including enterprise path prefixes.
  Relative upstream links are resolved on the current page. Caller cursors must be
  absolute URLs or returned opaque cursors. Other paths and origins are refused;
  if a later page/link fails, the failure result retains fetched items in
  `outputs.data` with `truncated:true`. Partial cursors bind the repository, kind and
  `state`; changing them requires a new listing.
- Rate limits: reads retry 429, primary limits (`X-RateLimit-Remaining: 0` with
  `X-RateLimit-Reset`), and 403 secondary/abuse limits. Numeric or HTTP-date
  `Retry-After` is honoured within the total call budget; secondary limits without
  timing return `RATE_LIMITED` immediately with advice to wait at least 60 seconds.
  The later of Retry-After and primary reset wins; an already elapsed reset uses a
  small jittered wait (0.1–0.55 seconds). Other 403s return `AUTH_FAILED`
  with scope/resource/installation guidance. Issue/comment writes explicitly declare
  `non_idempotent`, require approval and are sent once; GitHub has no supported
  idempotency key for these endpoints.

## Slack: auth, pagination and rate limits

- Auth: plain `slack_bot_token` credentials keep working. OAuth records with token
  rotation refresh at `https://slack.com/api/oauth.v2.access`; rotated refresh tokens
  are persisted before another Slack request.
- Pagination: `list_channels` and `read_history` follow `response_metadata.next_cursor`
  through empty as well as full pages. `limit` is the page size (1–200), and
  `max_items` optionally limits total returned items (1–10000). Slack can impose
  smaller page sizes and stricter quotas on some app types. A `has_more` response
  without a usable cursor is explicitly truncated; it cannot be resumed by cursor.
- Rate limits: read retries honour Slack 429/Retry-After and HTTP-200
  `ok:false,error:ratelimited`. Invalid/expired/revoked auth maps to `AUTH_FAILED`;
  membership, channel and scope errors give actionable messages. `post_message`
  accepts a UUID `client_msg_id`; when omitted, an available workflow step idempotency
  key becomes a stable UUID. No new random ID is invented on recovery. This field
  is forwarded as a correlation ID and provides no de-duplication guarantee: the operation
  explicitly stays `non_idempotent`, requires approval and never auto-retries.

## Google Drive: auth, pagination and rate limits

- Auth: plain `google_oauth_token` or OAuth records; refresh endpoint
  `https://oauth2.googleapis.com/token`. Drive and Sheets may share one stored Google
  credential with appropriate scopes and one in-process refresh flight.
- Pagination: `list_files` follows `nextPageToken`, including empty pages, with
  `page_size` 1–100 and optional `max_items` 1–10000. `page_token` remains a cursor
  alias. Drive's `incompleteSearch` yields `truncated:true,incomplete_search:true`;
  narrow the search/corpus rather than treating that result as complete.
- Rate limits: reads retry 429 and 403 `rateLimitExceeded`/`userRateLimitExceeded`,
  honouring Retry-After or using jittered exponential backoff. Permission failures
  remain `AUTH_FAILED`. `upload_text` accepts `file_id` **reserved by Drive's
  `files.generateIds` API** and sends it in create metadata. Arbitrary UUIDs are not
  valid pre-generated Drive IDs. Reserve and persist the ID in application code before
  submitting an approved create; a repeated create may return 409, which requires
  reconciliation. The operation also supports creates without such an ID, so it
  explicitly remains `non_idempotent`, requires approval and never auto-retries.
  Multipart uploads reject content or metadata containing the multipart boundary
  before sending HTTP. Binary downloads are not JSON-parsed to inspect rate limits.

## Google Sheets: auth, pagination and rate limits

- Auth: the same Google OAuth record/refresh contract as Drive; use Sheets scopes.
- Pagination: `values.get` (`read_range`) has **no pageToken API**. Read the requested
  A1 range in one bounded response. Optional `max_items` (1–10000) limits returned
  rows and sets `truncated:true` when rows were omitted. There is no synthetic Sheets
  next cursor; request a narrower/subsequent A1 range explicitly. Responses beyond
  `max_response_bytes` fail with `RESPONSE_TOO_LARGE` rather than returning partial JSON.
- Rate limits: reads retry 429 and Google 403 rate-limit reasons with Retry-After or
  jittered exponential backoff. `append_rows` and `update_range` have explicit
  `non_idempotent` effect classes and approval gates. Neither has a supported
  idempotency key. USER_ENTERED values can include formulas, so even range updates
  retain the conservative class and never auto-retry.

### Item caps and retry boundaries

GitHub, Slack and Drive keep the existing `max_pages` default 3/hard cap 10. Either
page or item cap produces `truncated:true` when more items remain. `next_cursor`
can be a native provider cursor or an opaque `nexura-page-v1:` cursor retaining a
partial page and its offset/page size. Pass it unchanged with the **same operation,
resource and filters**. Resuming a partial page re-fetches that page; listings are
not snapshots, so concurrent upstream insertions/deletions can affect results.
Malformed responses and invalid cursors fail explicitly. Check `truncated` even
when no `next_cursor` exists (Sheets row caps or Drive incomplete searches).
Repeated page links/cursors stop before fetching the repeated page and return
`truncated:true,pagination_loop:true`. Restart or investigate the listing when this
flag is present; a retained Slack cursor is diagnostic, not a safe continuation.

Only idempotent GET/HEAD reads retry automatically. The four connectors use jittered
exponential fallback delays and bounded retry counts. Each invocation shares a total
budget across pages, HTTP requests, refresh waits and backoff: the smaller of configured
`timeout_seconds` and the manifest's 30 seconds. Server delays include nonnegative
jitter (up to 10%, capped at 3 extra seconds). A wait that cannot fit the remaining
budget returns retryable `RATE_LIMITED` immediately with `metadata.retry_after_seconds`,
so the caller can schedule the read later without an early retry. Shorter waits use
cancellable intervals of at most 5 seconds.
Writes, including writes carrying provider-specific keys, are never retried on
429, 403 throttling, 5xx or transport failures. A lost write response, HTTP 408 or
HTTP 5xx is `INDETERMINATE_EFFECT`; inspect the remote effect before authorizing
another attempt. The module-level manifest stays `non_idempotent_write`; every
operation explicitly declares `pure` for reads or `non_idempotent` for writes in
`OPS` and manifest display examples. These per-operation declarations are informational;
the framework's module effect policy remains conservative for the whole connector.
The existing workflow `Idempotency-Key`
header remains for compatibility; sending this header does not assert provider support
or change the effect class. Slack and Drive use the native fields described above.

All of this is verified with local MockTransport fixtures, not live provider accounts.

## Safety rules

- Credentials are resolved from `credential_ref` through the execution context. A literal token or
  URL in `credential_ref` is refused.
- A credential whose stored type differs from the connector's credential type (for example an
  `openai_api_key` used by `github`) is refused, using the secrets store's metadata. A resolver
  that exposes no metadata cannot be type-checked.
- Every outbound URL goes through `DestinationPolicy`: https only, private, loopback and link-local
  addresses blocked, the cloud metadata range never allow-listable, redirects never followed, and
  credentials are never sent to a different origin than the configured base.
- Destination addresses must be globally routable. Link-local, metadata (169.254.0.0/16,
  fd00:ec2::254, 100.100.100.200), CGNAT (100.64.0.0/10), 0.0.0.0/8, multicast, reserved and other
  non-global ranges are blocked, and IPv4 embedded in IPv4-mapped, NAT64, 6to4, Teredo or
  IPv4-compatible IPv6 addresses is checked. `allowed_hosts` still resolves the host; it may only
  open RFC1918 and loopback ranges, never the blocked ranges above.
- DNS rebinding: HTTP connections use a pinned network backend that resolves the host once,
  validates every address with the same policy and connects to a validated address, so there is
  no time-of-check gap between the policy check and the socket. This relies on httpx's private
  `AsyncHTTPTransport._pool` attribute (no public hook exists) and is skipped when a transport is
  injected for tests. SMTP, IMAP and Postgres resolve the host once, validate every address and
  connect to the first validated IP, while TLS still verifies the certificate against the
  original host name (`PinnedHostnameSSLContext`; asyncpg gets the IP as `host=` plus a context
  bound to the name when the URL asks for `sslmode=verify-ca` or `verify-full`; other sslmodes
  never verified the name). That context reads the same inputs as asyncpg's own: `sslrootcert`
  (required for the verify modes, as in libpq; there is no system-CA fallback), `sslcrl`
  (revocation checking), `sslcert`, `sslkey` and `sslpassword`, from the DSN, then
  `PGSSLROOTCERT`, `PGSSLCRL`, `PGSSLCERT` and `PGSSLKEY`, then `~/.postgresql/`. An unreadable
  file fails the connection. Only the first validated address is tried (no failover). The MySQL
  `driver_factory` is called as `factory(dsn, pinned_ip=..., server_name=...)`: a custom driver
  must connect to `pinned_ip` and verify TLS against `server_name`. A legacy `factory(dsn)`
  still works but resolves the name itself, so that injected driver keeps the time-of-check gap.
- Database URLs (Postgres, MySQL) must name exactly one TCP host: multi-host URLs, `host`,
  `hostaddr` or socket query parameters and unix-socket hosts are refused. SQL is validated on a
  tokenised copy (string literals, quoted identifiers and dollar-quoting are data) and the
  original text, exactly as submitted, is what the driver executes.
- SQL `query` is a read (no approval), so it may call only an allow-list of known side-effect-free
  functions (`READ_FUNCTION_ALLOWLIST` in `connectors/sql.py`, per dialect: aggregates, window,
  string, math, date/time, JSON, array and cast/type names; Postgres adds e.g. `gen_random_uuid`,
  MySQL adds e.g. `unix_timestamp`; clock reads such as `clock_timestamp` and `UTC_TIMESTAMP`
  count as reads, and MySQL `TRUNCATE(x, d)` is a function while `TRUNCATE TABLE` is a write).
  Any other call (an identifier, quoted identifier or schema-qualified name followed by `(`) is
  refused. A name followed by `(` is skipped only when the grammar position makes it a keyword:
  reserved words such as `IN`, `EXISTS`, `VALUES`, `ANY`, `ALL`, `FILTER (` or `OVER (` right after
  a call's closing parenthesis, `WITHIN GROUP (`, `PARTITION BY`/`ORDER BY (`, `SIMILAR TO (`, and
  `IS`, `ESCAPE`, `LIKE`, `JOIN` after an operand. `SELECT filter('x')` or `SELECT over(1)` is a
  function call and is refused with `INVALID_PARAMS` telling the caller to use `execute`, which
  needs approval. Only `pg_catalog.`-qualified calls are accepted on Postgres. The limit: an
  allow-list cannot know that a user-defined function is pure, so those (and `dblink_exec`,
  `pg_notify`, `nextval`, `sleep`, `get_lock`, ...) always need `execute` plus approval. CTE
  column lists other than directly after `WITH` or `AS` are refused conservatively.
- Queries run in a read-only transaction: asyncpg uses `transaction(readonly=True)`; the
  connector also passes `read_only_sql` (`BEGIN READ ONLY` for Postgres,
  `START TRANSACTION READ ONLY` for MySQL) to `SqlDriver.fetch`, which a MySQL driver must issue
  before the query. The in-memory fake records it in `read_only_begins`.
- Request bodies are capped at 262,144 bytes of JSON (`MAX_REQUEST_BYTES`) for the webhook
  `payload` and Airtable `records`.
- Responses are streamed and cut off at the size cap. Errors and traces redact the secret and
  derived forms (base64, URL path tokens, password part).
- Unknown parameters are rejected; path segments are validated and quoted.

## Drivers

Postgres uses `asyncpg` (the `nexura-connectors[postgres]` extra; in `uv.lock`). `smtplib`, `imaplib` and the S3 SigV4 signer use the
standard library. No MySQL driver is installed: `MySqlConnector` talks to the `SqlDriver`
protocol, and without an injected `driver_factory` it fails with `MISSING_DEPENDENCY`. Install an
async MySQL driver as an optional extra and pass `driver_factory=`.

## Runnable offline example

```python
import asyncio

import httpx
from nexura_agents.contracts.context import CredentialReference, ExecutionContext

from nexura_connectors import SlackConnector


class Secrets:
    """Stand-in for the secrets store: credential id -> value."""

    def resolve(self, ref: CredentialReference | str) -> str | None:
        return {"slack-prod": "xoxb-example-not-real"}.get(
            ref if isinstance(ref, str) else ref.credential_id
        )


def fake_slack(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.0"})


async def main() -> None:
    slack = SlackConnector(transport=httpx.MockTransport(fake_slack))  # mock transport
    context = ExecutionContext(run_id="r", node_id="n", credential_resolver=Secrets())
    config = {"credential_ref": "slack-prod"}
    post = {"operation": "post_message", "params": {"channel": "C1", "text": "hi"}}

    blocked = await slack.execute(context, {**post, "approved": True}, config)
    assert blocked.error_code == "APPROVAL_REQUIRED"  # the input flag grants nothing

    operator = ExecutionContext(
        run_id="r", node_id="n", credential_resolver=Secrets(), approval_granted=True
    )
    sent = await slack.execute(operator, post, config)
    assert sent.outputs["data"] == {"channel": "C1", "ts": "1.0"}


asyncio.run(main())
```

## In Studio

The Module Registry drawer lists each connector operation with a marker, "reads" or "writes - needs
approval", and the credential type the connector needs. In the node inspector the `credential_ref` field
is a picker of the workspace's credential references of that type (id and description only; secret values
are never fetched or shown). When none exist it says "none of type X" and links to Credentials. The
`operation` input is a select of the connector's operations with the same markers. Register a credential of
the connector's exact type (for example through `POST /api/v1/credentials`); the type is checked again at run
time.
The operation select stores `{"$literal": "<operation>"}` in `input_mapping` (see docs/workflows.md); a
stored unknown operation is marked "unknown operation, treated as a write (writes - needs approval)" and an unknown
`credential_ref` shows "unknown reference (hidden)" without echoing the value. The Credentials form also
offers the credential types that registered modules require.

## Package overview

**15 SaaS connectors as Nexura workflow modules, with writes that wait for a human approval.**

```bash
pip install nexura-connectors              # every connector (installs nexura-runtime too)
pip install "nexura-connectors[postgres]"  # + asyncpg, the Postgres connector's driver
```

Webhook (outbound), Slack, email send (SMTP) and read (IMAP), Google Drive, Google Sheets,
Postgres, MySQL, S3-compatible storage, GitHub, Notion, Jira, Airtable, Discord and Microsoft
Teams. Each is one module, `nexura.connectors.<name>`, with a table of operations.

- **Registered automatically.** When this package is installed, `nexura-runtime` imports it and
  registers every connector on its module registry, so workflows and Studio can use them.
  Without it, the runtime still works and reports each `nexura.connectors.*` module as
  unavailable with the message "Install it with: pip install nexura-connectors".
- **Writes need a human approval.** Write operations fail with `APPROVAL_REQUIRED` unless the
  execution context carries `approval_granted=True`, which the workflow engine sets only after a
  person approves. An `approved: true` input grants nothing.
- **Credentials by reference.** `credential_ref` names an entry in the secrets store; a literal
  token is refused, and a credential of another type is refused.
- **Destination checks.** https only, private and metadata addresses blocked, DNS pinned to the
  validated address, `base_url` only to the vendor host or an operator allow-list.

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Every connector is
> tested offline only, against mock HTTP transports and in-memory database and mail fakes; none
> has been checked against the live service. The full guide is `docs/connectors.md` in the
> repository.

## Registered with the runtime

```python
from nexura_runtime.modules import default_registry

connectors = [
    m for m in default_registry.list_modules() if m["module_id"].startswith("nexura.connectors.")
]
assert len(connectors) == 15 and all(m["is_available"] for m in connectors)
print(sorted(m["module_id"] for m in connectors))
```
