# Nexura TypeScript client

HTTP client for the local single-user Nexura Service API only. Requires a running Nexura service and a
JavaScript environment with `fetch` (for example Node.js 18 or later).

## Install

```sh
npm install @liceron/nexura-sdk
```

## Minimal example

Start the local service with `nexura-runtime serve`, then:

```ts
import { NexuraClient } from "@liceron/nexura-sdk";

const client = new NexuraClient({
  baseUrl: "http://127.0.0.1:8765",
  workspaceId: "default",
});
const workflows = await client.workflows.list();
console.log(workflows);
```

## Status and limits

Alpha (0.4). Node 24 is the tested version; other fetch-capable environments
are not covered by that verification. This is an API client, with no standalone agent runtime.
The open-source service is single user, this machine only. Its session token stops other web pages,
not local programs that can read it; it adds no shared-service authentication.

The local session token is fetched automatically when a state-changing request
needs it, and refreshed once after a token refusal (for example after a server
restart). This is enabled by default with `autoSessionToken: true`. The server
allows token retrieval from its own browser origin or local non-browser clients.
You can also provide `sessionToken` explicitly.

## Documentation and licence

See the [main docs](https://github.com/Liceron/nexura/blob/main/docs/README.md), [Service and Studio](https://github.com/Liceron/nexura/blob/main/docs/studio-and-service.md)
and [API reference](https://github.com/Liceron/nexura/blob/main/docs/api-reference.md). Apache-2.0, Copyright (c) 2026 Liceron;
see [LICENSE](https://github.com/Liceron/nexura/blob/main/clients/typescript/LICENSE).
