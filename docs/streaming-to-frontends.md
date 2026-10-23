# Streaming to frontends

The runtime service offers three SSE representations on the same endpoints:

| Representation | Query | Alternative `Accept` |
| --- | --- | --- |
| Native Nexura (default) | `stream_format=native` | `text/event-stream` |
| AG-UI | `stream_format=ag-ui` | `application/vnd.ag-ui+json` |
| AI SDK UI messages | `stream_format=ai-sdk` | `application/vnd.vercel.ai.ui-message-stream+json` |

Use `POST /api/v1/sessions/{session_id}/chat/stream` for chat and
`GET /api/v1/runs/{run_id}/events?live=true` for a workflow. Request bodies,
authentication, session persistence, workspace checks and approval decisions use
the existing API. See [Service and Studio](studio-and-service.md) and
[approval gates](approvals.md). These are output adapters, not implementations of
the AG-UI `RunAgentInput` or AI SDK chat request API.

An explicit query parameter takes precedence over `Accept`. Recognized media
ranges respect `q` weights (first wins ties); `q=0` disables that range. Malformed
weights are ignored. If no recognized, acceptable range remains, the service
falls back to native, including `q=0`, malformed weights, unsupported Accept
types, absent Accept and wildcards. Invalid `stream_format` returns 400
before execution. Responses always have `Content-Type: text/event-stream` and
`Vary: Accept`. The vendor media types in this table and
`x-ag-ui-protocol-version` are **Nexura-defined selectors/metadata**, not media
types or headers specified by AG-UI or Vercel. Stock AG-UI `HttpAgent` sends
`Accept: text/event-stream`; use **`?stream_format=ag-ui`** with that client.
Protobuf (`application/vnd.ag-ui.event+proto`) is unsupported and falls back to
native; this service does not produce AG-UI protobuf. Native wire framing remains
unchanged. The output-guardrail holding rule below applies to all formats.

## Protocol pins

- **AG-UI protocol 1.0**, the lifecycle/text/tool/state/custom subset documented
  by [AG-UI events](https://docs.ag-ui.com/concepts/events). The upstream
  [core manifest](https://github.com/ag-ui-protocol/ag-ui/blob/main/sdks/typescript/packages/core/package.json)
  identifies `@ag-ui/core` **1.0.3** and `agui.protocolVersion: "1.0"` at inspection.
  Nexura sends `x-ag-ui-protocol-version: 1.0`. It uses legacy `RUN_FINISHED`
  without `outcome`; pause meaning is carried by the custom event and
  `result.nexura.paused` below.
- **Vercel AI SDK 5.0.0**, `UIMessageChunk` SSE protocol, wire header
  **`x-vercel-ai-ui-message-stream: v1`**. Checked against the current
  [AI SDK stream documentation](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)
  and the immutable
  [5.0.0 chunk schema](https://github.com/vercel/ai/blob/ai%405.0.0/packages/ai/src/ui-message-stream/ui-message-chunks.ts).
  Current docs describe newer SDKs too; Nexura deliberately implements this
  pinned subset. It does not use the older numeric-prefix data protocol.

No AG-UI or JavaScript SDK dependency is needed on the Python server. The adapters
were implemented from the public wire specifications; no upstream source was copied.
Verification uses local FakeModels and mock/in-process transports, not live providers
or browser SDK compatibility testing.

## AG-UI example

Send the normal Nexura chat body. Agents without output guardrails stream text
as native `TEXT_DELTA` events arrive. For an active agent with output guardrails,
the source holds model text, emits no deltas, and publishes only guardrail-checked
`FINAL_OUTPUT` (or the saved reply). Rewrites replace the unchecked answer before
it is published; a tripwire publishes no blocked text, only an error. This rule
also applies to native Studio chat and to guarded handoff targets. Workflow traces
already omit raw deltas and expose checked final output. Tool results retain the
native visibility policy; output guardrails are not a general tool-content scrubber.

```javascript
const response = await fetch(
  '/api/v1/sessions/my-session/chat/stream?stream_format=ag-ui',
  {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Nexura-Session': sessionToken, // existing local service session token
    },
    body: JSON.stringify({ message: 'calculate 25 * 4' }), // local fake demo
  },
);
if (!response.ok) throw new Error(`HTTP ${response.status}`);

// Feed parsed SSE data objects to your AG-UI event handler.
// Use a streaming SSE parser; a network chunk can split a frame or JSON string.
for await (const event of parseSSEData(response.body)) {
  if (event.type === 'TEXT_MESSAGE_CONTENT') appendText(event.messageId, event.delta);
  if (event.type === 'CUSTOM' && event.name === 'nexura.approval') showApproval(event.value);
}
```

`parseSSEData`, `appendText` and `showApproval` above are application helpers.
The output follows this shape (each `data:` line is followed by a blank line):

```text
data: {"type":"RUN_STARTED","threadId":"my-session","runId":"chat-..."}

data: {"type":"TEXT_MESSAGE_START","messageId":"chat-...:text:1","role":"assistant"}

data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"chat-...:text:1","delta":"Hello"}

data: {"type":"TEXT_MESSAGE_END","messageId":"chat-...:text:1"}

data: {"type":"RUN_FINISHED","threadId":"my-session","runId":"chat-..."}
```

Chat uses the session ID for `threadId` and a new transport run ID per request;
the public native chat envelope does not expose the Runner's internal run ID.
Workflow `threadId` and `runId` both use the stored workflow run ID. Message and
tool IDs are scoped to the transport and to agent executions to avoid collisions.

## AI SDK example

With `ai@5.0.0`, translate the outgoing request to Nexura's chat body using
`DefaultChatTransport`. For example, in a React app using its compatible
`@ai-sdk/react` 2.x package:

```javascript
import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport } from 'ai';

export function useNexuraChat(sessionToken) {
  return useChat({
    transport: new DefaultChatTransport({
      api: '/api/v1/sessions/my-session/chat/stream?stream_format=ai-sdk',
      headers: { 'X-Nexura-Session': sessionToken },
      prepareSendMessagesRequest({ messages }) {
        const last = messages[messages.length - 1];
        const message = last.parts
          .filter(part => part.type === 'text')
          .map(part => part.text).join('');
        return { body: { message } }; // server owns the persisted transcript
      },
    }),
    onData(part) {
      if (part.type === 'data-nexura-approval') showApproval(part.data);
    },
  });
}
```

Call `const chat = useNexuraChat(sessionToken)` in your component and render text
parts from `chat.messages`; dynamic tool parts contain tool names,
available input and output. `sessionToken` and `showApproval` are application
values. This output stream is also consumable using AI SDK's
`readUIMessageStream` and an SSE/`UIMessageChunk` parser. A normal completed
response ends with `finish` and `data: [DONE]`. Failures, including malformed
source frames, source exceptions and EOF without a terminal event, emit an
`error` chunk, `finish`, then `[DONE]`; exception details are withheld.
Explicit cancellation emits `data-nexura-cancelled`, `abort`, `finish`, `[DONE]`.
A client disconnect cannot receive a guaranteed terminator.

## Event mapping and approval boundaries

| Public Nexura event | AG-UI | AI SDK 5 |
| --- | --- | --- |
| First frame / run start | `RUN_STARTED` | `start` |
| `TEXT_DELTA` | `TEXT_MESSAGE_START/CONTENT/END` | `text-start/delta/end` |
| `FINAL_OUTPUT` without deltas for that model turn | Text message | Text part |
| `TOOL_STARTED` | `TOOL_CALL_START/ARGS/END` | `tool-input-start/delta/available` |
| `TOOL_COMPLETED` for an observed call | `TOOL_CALL_RESULT` | `tool-output-available` |
| Workflow initial/input/final state | `STATE_SNAPSHOT` | `data-nexura-state-snapshot` |
| Node `state_update` | Dropped; await next authoritative snapshot | Same |
| `RUN_PENDING_APPROVAL` / `RUN_PAUSED` | `CUSTOM: nexura.approval` | `data-nexura-approval` |
| Input/effect pause | `CUSTOM: nexura.paused` | `data-nexura-paused` |
| Run success | `RUN_FINISHED` | `finish` |
| Root failure/rejection | `RUN_ERROR` | `error`, `finish` |
| Explicit cancellation | `CUSTOM: nexura.cancelled`, `RUN_FINISHED` | Cancellation data, `abort`, `finish` |

State synchronization uses snapshots only. A node's `state_update` is a raw write;
reducers such as `append` may change its meaning. Sending it as JSON Patch would
overwrite reduced state incorrectly, so it is omitted. The next node-input or
final snapshot supplies authoritative reduced state. A partial replay beginning
with a raw update waits for such a snapshot instead of inventing a baseline.
Workflow final snapshots include only the final state already present in native
SSE. Agent event wrappers (`AGENT_EVENT`) expose supported text/tool events; a
child's completion, failure or pause does not finish its parent workflow.

Approval custom payloads contain only `reason` plus any public `approval_id`,
`interrupt_id`, `effect_id` and `node_id`. **The notice** contains no arguments or
interruption transcript. Calls that later run or are refused appear as ordinary
tool events with the arguments the native stream shows. In particular, chat's
refused approval arguments can appear in `TOOL_CALL_ARGS`/`tool-input-available`.
Raw `TOOL_CALL_DELTA`, partial structured output, model
metadata, arbitrary tool custom payloads and unknown/internal events are dropped.
Only `TOOL_STARTED` arguments already visible in the native public stream can
become standard tool input events. Adapters never read the Runner, checkpoint,
approval record or database directly; existing route redaction remains authoritative.
They do not add a new general content scrubber or promise to detect arbitrary secrets
the native stream itself exposes.

A workflow pause emits its custom notice immediately. A finite replay ending at
that pause adds `RUN_FINISHED.result.nexura` with `paused: "approval"` (or
`"input"`/`"effect_unknown"`) and available public identifiers. It also retains
the custom notice. This signals the observation boundary,
**not approval or successful tool execution**. A live response retains native
polling behavior and can continue when the workflow resumes. Full replay reads
past historical pauses to later events; it does not stop at the first approval.
Use the existing approval/input/effect routes to continue execution. The chat
playground automatically refuses approval-gated tools and
continues its turn; its approval notice is informational and does not end that
chat response. Adapted chat finishes only after `chat_completed`; late response
assembly or persistence exceptions become sanitized terminal stream errors rather
than a truncated success. This guarantee applies to the adapters; native error
framing is unchanged. A disconnected chat closes its source generator and
retains the existing persistence semantics.

## Replay and stream closure

Workflow `cursor` and `Last-Event-ID` retain their native meanings. Each source
event may produce several protocol events; only its final mapped frame carries
the native SSE ID, so reconnecting after a complete group can advance the cursor.
Source events that produce no output do not advance the visible cursor. A reconnect creates
a fresh protocol envelope and fresh local text/tool state; it cannot reconstruct
an earlier partial message, unobserved tool start or missing snapshot. Use full
replay (`cursor=0`, no `Last-Event-ID`) to rebuild frontend state. This is not AI
SDK resumable-stream persistence or AG-UI thread synchronization.

A finite workflow replay, an empty replay after a cursor, or the live poll timeout
can end without a terminal run event. These emit `CUSTOM: nexura.stream-closed`
or `data-nexura-stream-closed`, with `{"terminal": false}`, followed by a terminal
protocol error (`RUN_ERROR` with `STREAM_INCOMPLETE`, or AI SDK `error`, `finish`,
`[DONE]`). The error describes an incomplete observation, not a failed workflow;
reconnect to continue observing. AG-UI always ends with `RUN_FINISHED` or
`RUN_ERROR` while connected. A finite replay ending at an approval pause is the
documented paused finish above, not an incomplete observation. Terminal runs produce exactly one terminal sequence;
duplicate native completion/stream-close frames are not forwarded.
