# Run Nexura on a server (single operator)

By default `nexura-runtime serve` is for this machine only: it listens on `127.0.0.1`,
answers only loopback peers and needs no login. Remote single-operator mode lets **one
person** run the open-source service on a remote host or VM and use Studio and the REST API
over the network. One operator token protects every request.

This mode is not multi-user. Everyone who has the token is the same local owner (role
`admin`). The token carries the full authority of the operating-system user that runs the
service: whoever holds it can run workflows and the handlers registered on the host, use and
change credentials, register MCP servers and integrations, and read every run. Treat it like
that user's SSH key. Accounts, roles, SSO, organisations and per-user audit are in the Nexura
enterprise edition. A reverse proxy does not add them.

## How it works

- `nexura-runtime serve --auth token` installs the open-source `OperatorTokenAuthProvider`
  (`nexura_runtime.service.api.operator_token`) through the existing `AuthProvider` seam.
- Every HTTP and WebSocket request needs `Authorization: Bearer <operator token>`. The only
  exceptions are `/healthz` and `/readyz`. The API docs (`/docs`, `/openapi.json`) and
  extension routes need the token too.
- Webhook deliveries are the one exception to the operator token. Each webhook endpoint gets
  its own delivery token, and `POST /api/v1/triggers/webhooks/{id}/deliver` accepts only that
  token. See [Webhooks](#webhooks).
- `/readyz` returns only a status in this mode, with or without the token. Probe details are
  not served on a public route, so it cannot be used to test a guessed token. A wrong bearer
  sent to `/readyz` counts as a failed attempt; a probe that sends none is not counted.
- A missing, malformed or wrong token gets the same answer: `401` `UNAUTHENTICATED` with
  `WWW-Authenticate: Bearer`. The answer does not say whether a token was sent or how close it
  was.
- After 10 failed attempts from one address within 60 seconds, that address gets `429`
  `TOO_MANY_AUTH_FAILURES` with `Retry-After` for wrong or missing tokens. The right token
  always gets through, also from a blocked address, and clears that address's failures, so
  the block never locks the operator out. For the same reason it does not limit guessing: a
  blocked address still tells a right token (200) from a wrong one (429). The block only
  marks the address and limits log noise and load. Resistance to guessing comes entirely from
  the token's randomness, so use a generated token (`secrets.token_urlsafe(32)`, see below):
  the server cannot measure the strength of a token a person made up.
- A request from another machine that did not arrive over HTTPS is refused with `403`
  `PLAIN_HTTP_REFUSED` before any token is read. Behind a TLS reverse proxy on the same host
  the service learns the scheme from `X-Forwarded-Proto`. `--insecure-plain-http` /
  `allow_plain_http=True` turns this off. This also covers an app that embeds
  `create_api_app()` and binds it itself.
- The service keeps only the SHA-256 hash of the token in memory and compares hashes with
  `hmac.compare_digest`. It never prints or logs the token. A rejected attempt is logged with
  the peer address and `missing` or `invalid`, never the value.
- The caller is the same local owner as on loopback. In this mode the loopback-client rule,
  the per-session `X-Nexura-Session` token and the JSON-only rule are switched off. Browsers
  never send an `Authorization` header by themselves, so another web page cannot use your
  session. Clients send only the bearer. `GET /api/v1/local-session` answers `404` to a
  signed-in client; a request to it without any bearer (an older client's probe) is refused
  but not counted as a failed attempt.

## 1. Create the token

Generate the token, do not make one up:

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))" > /srv/nexura/operator.token
chmod 600 /srv/nexura/operator.token
```

The token must be at least 43 characters. The server cannot measure real entropy, but it
refuses obvious non-random tokens at startup: one kind of character, a repeated piece, a long
`abcd` or `aaaa` run, words repeated, or a low estimate from character frequencies. Every
refusal prints the command above. `secrets.token_hex(32)` output is accepted too.

Give the token to the service in one of two ways, never both:

- **Prefer the token file:** `--token-file PATH` (or `NEXURA_OPERATOR_TOKEN_FILE=PATH`). The
  service opens the file once without following a symbolic link and checks that same open file
  before reading it. On Linux and macOS the file must be a regular file owned by the user that
  runs the service, with no group or other permissions (`chmod 600` or `400`); otherwise the
  service refuses to start. A UTF-8 byte-order mark and a trailing newline are ignored. On
  Windows, keep the file in a folder only you can read.
- `NEXURA_OPERATOR_TOKEN`. `nexura-runtime serve` removes the variable from its environment
  after reading it, so MCP servers and other child processes do not inherit it. The process's
  initial environment stays readable in `/proc/<pid>/environ` (and `ps eww`, `systemctl show`
  for a unit's `Environment=`) by the same user and root for as long as the service runs. Use
  the file in production. The Python API (`OperatorTokenAuthProvider.from_environment`) reads
  the variable and does not remove it. If the variable is set but `--auth token` is not given,
  `serve` prints a warning.

There is no command-line option for the token itself, because the command line shows in `ps`.
To rotate the token, write a new one and restart the service. The old token stops working at
the restart.

## 2. Start the service on loopback

Keep the service on `127.0.0.1` and let a TLS reverse proxy on the same host face the network:

```bash
nexura-runtime serve --host 127.0.0.1 --port 8765 \
  --db /srv/nexura/data/nexura_service.sqlite3 \
  --auth token --token-file /srv/nexura/operator.token
```

The rules:

| Command | Result |
| --- | --- |
| `serve` | Loopback only, no token (unchanged default) |
| `serve --host 0.0.0.0` | Refused at startup (exit 2), as before |
| `serve --auth token` | Token required, listening on `127.0.0.1`; network requests must arrive over HTTPS (a proxy) |
| `serve --auth token --host 0.0.0.0` | Refused: the token would cross the network over plain HTTP |
| `serve --auth token --host 10.0.0.5 --insecure-plain-http` | Allowed, with a loud warning |
| `serve --auth token --insecure-allow-unauthenticated-network` | Refused: contradictory |

The service does not do TLS. Use `--insecure-plain-http` only when TLS ends somewhere else
in front of the port without setting `X-Forwarded-Proto`, such as a VPN like WireGuard or
Tailscale. In Python, the same switches are
`create_api_app(auth_provider=OperatorTokenAuthProvider(token), listen_host=...,
allow_plain_http=...)`.

A systemd unit:

```ini
[Unit]
Description=Nexura service (remote single-operator mode)
After=network-online.target

[Service]
User=nexura
WorkingDirectory=/srv/nexura
ExecStart=/srv/nexura/.venv/bin/nexura-runtime serve --host 127.0.0.1 --port 8765 \
  --db /srv/nexura/data/nexura_service.sqlite3 --auth token \
  --token-file /srv/nexura/operator.token
Restart=on-failure
KillMode=control-group

[Install]
WantedBy=multi-user.target
```

## 3. Put TLS and Studio in front

The service serves the API, not the Studio files. Build Studio once
(`cd studio-web && npm ci && npm run build`) and let the proxy serve `studio-web/dist` and
forward `/api`, `/healthz` and `/readyz` to the service. Studio and the API then share one
origin. Studio calls `/api` on its own origin. The service sets no cookies and sends no CORS
headers. In this mode it accepts a browser `Origin` that equals the request's own origin
(scheme aside, its host and port must equal the `Host` header), or one listed in
`NEXURA_ALLOWED_ORIGINS`. Other origins get `403` `CROSS_ORIGIN_FORBIDDEN`.

Studio holds the operator token while a tab is open, so the proxy must send the same
Content-Security-Policy and security headers that the built-in Studio server sends
(`nexura_studio.server.STUDIO_CSP`). `connect-src 'self'` stops injected script from sending
the token to another host, and `frame-ancestors 'none'` stops other sites from framing Studio.
Both examples below set them for every response.

Caddy (`/etc/caddy/Caddyfile`) gets and renews a certificate by itself and sets
`X-Forwarded-For`, `X-Forwarded-Proto` and `Host` for the service:

```text
nexura.example.com {
	encode gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
	}
	request_body {
		max_size 40MB
	}
	@service path /api/* /healthz /readyz
	handle @service {
		reverse_proxy 127.0.0.1:8765
	}
	handle {
		root * /srv/nexura/studio-web/dist
		try_files {path} /index.html
		file_server
	}
}
```

nginx. The `add_header` lines stay in the `server` block: nginx drops them for a `location`
that has an `add_header` of its own, so do not add one to a location.

```nginx
server {
    listen 80;
    server_name nexura.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name nexura.example.com;
    ssl_certificate     /etc/letsencrypt/live/nexura.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nexura.example.com/privkey.pem;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "no-referrer" always;
    client_max_body_size 40m;

    root /srv/nexura/studio-web/dist;
    location / {
        try_files $uri /index.html;
    }

    location ~ ^/(api/|healthz$|readyz$) {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $http_host;          # keeps the port, so Origin matches Host
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;        # run events and chat streams are server-sent events
        proxy_read_timeout 1h;
    }
}
```

`Host $http_host` passes the host and port the browser used. With `$host` the port is lost, so
on a listener other than 443 every Studio request with a body would be refused as
cross-origin. `proxy_http_version 1.1` and `proxy_buffering off` keep server-sent events
streaming.

**Trusted proxy.** `nexura-runtime serve` runs uvicorn, which takes the client address and
scheme from `X-Forwarded-For` and `X-Forwarded-Proto` only when the request comes from a
trusted proxy. By default only `127.0.0.1` and `::1` are trusted (uvicorn's
`FORWARDED_ALLOW_IPS` environment variable). With a proxy on the same host, the per-address
limit therefore counts real client addresses, and HTTPS requests are recognised. If the proxy
runs on another host, set `FORWARDED_ALLOW_IPS` to that proxy's address. Never set it to `*`
while other machines can reach the port, because a client could then fake its address and
scheme. A proxy that forwards without `X-Forwarded-For` (a TCP passthrough, `ssh -L`) makes
every client look like `127.0.0.1`: they then share one failure count, although the right
token still always gets through.

`Host` is not restricted in this mode unless you set `NEXURA_ALLOWED_HOSTS`, for example
`NEXURA_ALLOWED_HOSTS=nexura.example.com`. Then other `Host` names get `400` `INVALID_HOST`.

## 4. Sign in

- **Studio.** Open `https://nexura.example.com`. The service refuses Studio's first request,
  so Studio shows a sign-in card. Paste the operator token. Studio sends it as
  `Authorization: Bearer` and keeps it in this tab's `sessionStorage`, so it is gone when you
  close the tab. Studio never writes the token to `localStorage`, the URL or the console. A
  token the server refuses, at sign-in or later (after a restart with a new token), is
  removed. If you change the server URL in Connection Settings to another origin, Studio drops
  the token instead of sending it there.
- **curl.** For example:
  `curl -H "Authorization: Bearer $(cat ~/.nexura-token)" https://nexura.example.com/api/v1/workflows`.
- **Python.** Use `NexuraClient("https://nexura.example.com", api_key=token)`
  (`nexura_runtime.service.api.client`).
- **TypeScript.** Use `new NexuraClient({ baseUrl: "https://nexura.example.com", apiKey: token })`.

## Webhooks

Senders never get the operator token. Each webhook endpoint has its own **delivery token**:

- Registering a webhook (`POST /api/v1/triggers/webhooks`) in this mode returns a
  `delivery_token` next to the HMAC `secret`. Both are shown once. The service keeps only the
  SHA-256 hash of the delivery token. Delivery tokens are 32 random bytes
  (`secrets.token_urlsafe(32)`).
- A sender calls `POST /api/v1/triggers/webhooks/{endpoint_id}/deliver` with
  `Authorization: Bearer <delivery token>` and the usual v2 HMAC signature headers
  (`X-Webhook-Signature`, `X-Webhook-Timestamp`, `X-Webhook-Event-ID`). The delivery route
  accepts only that endpoint's delivery token: not the operator token, and not another
  endpoint's token. A delivery token works on no other route.
- `POST /api/v1/triggers/webhooks/{endpoint_id}/delivery-credential` (with the operator token)
  issues a new delivery token; the old one stops working at once. Use it to rotate a token, or
  to get one for a webhook registered before remote mode was turned on (such a webhook has
  no delivery token and accepts no deliveries until you issue one). Python:
  `client.triggers.rotate_delivery_token(endpoint_id)`; TypeScript:
  `client.triggers.rotateDeliveryToken(endpointId)`. Deleting the webhook deletes its token.
- In this mode legacy v1 signatures are refused, even for an endpoint registered with
  `allow_legacy_signature=True`: v1 does not cover the event id, so a captured delivery could
  be replayed under a new id.
- Every refused delivery gets the same `401` `UNAUTHENTICATED`: an unknown or inactive
  endpoint, a wrong delivery token, a bad signature, an expired timestamp, missing or
  malformed headers and a body that is not a JSON object. The answer does not reveal which
  endpoint ids exist. Each refusal counts as a failed attempt for that address. Bodies over
  1 MB are still `413`.
- Third-party services such as Stripe, GitHub or Slack cannot deliver directly. Their
  signature schemes differ from Nexura's (they sign the raw body with their own headers), and
  they do not send a bearer token. Put a small relay in front that verifies their signature
  and re-signs the event with the Nexura scheme and the delivery token.

Studio's webhook panel shows the HMAC secret and signing snippets; in this mode the snippets'
`NEXURA_API_KEY` bearer must hold the endpoint's delivery token.

On this machine without `--auth token`, webhooks behave as before: no delivery token, the
HMAC signature alone, and only loopback senders.

## What is and is not protected

Protected:

- Every route except `/healthz` and `/readyz` needs the operator token. That includes the
  API, docs and extension routes. Webhook delivery routes need their own endpoint's delivery
  token instead.
- `/readyz` gives no details in this mode and counts wrong bearers, so it is not a token
  oracle.
- Guessing is impractical only because a generated token is 32 random bytes
  (`secrets.token_urlsafe(32)`). The per-address block does not limit guessing (the right
  token always passes, so a blocked address still tells right from wrong); it only limits
  log noise and load. The server rejects obvious patterns but cannot measure the strength of
  a token a person made up, so always generate it.
- The token does not appear in logs, startup output, error bodies or child-process
  environments. Existing redaction (traces, run diagnostics, repro bundles, channel text)
  already hides `Authorization` headers and values. With `NEXURA_OPERATOR_TOKEN` it stays in
  the service's own `/proc/<pid>/environ` (see above); the token file avoids that.
- Network requests over plain HTTP are refused, at startup for a non-loopback bind and at
  request time for every non-loopback peer, unless you pass `--insecure-plain-http`.

Not protected, or not provided:

- **One identity.** The token carries the service user's full authority. Nexura has no
  read-only operator, no per-person audit trail and no token expiry. Anyone who copies the
  token is you until you rotate it and restart.
- **No TLS in the service.** HTTPS is the reverse proxy's job. Over plain HTTP, a network
  observer can read the token.
- **Rate limiting is in memory and per process.** It resets on restart. It counts failed
  attempts only, not request volume. Behind a proxy it needs the trusted-proxy setting above
  to see real addresses. An address that keeps failing stays blocked for as long as it keeps
  failing; that blocks only wrong or missing tokens, never the right one, and so it is not a
  defence against guessing.
- **Studio keeps the token in `sessionStorage`.** Any script running on the Studio origin
  could read it. The CSP above limits where such script can send it; serve only the built
  Studio from that origin.
- **The rest of the security model still applies.** Workflows, handlers, MCP servers and the
  sandbox behave as in [SECURITY_MODEL.md](../SECURITY_MODEL.md). Remote access does not add
  isolation between workspaces. Local workspaces are one owner's projects.
- **Multi-user.** Several people, roles, SSO or organisations need the Nexura enterprise
  edition.

See also [Service and Studio](studio-and-service.md) for the API itself.
