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
`max_retries` (default 2, maximum 3) with `Retry-After` capped at 5 seconds.

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
