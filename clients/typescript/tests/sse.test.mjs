import test from "node:test";
import assert from "node:assert/strict";
import { NexuraClient, readSse } from "../dist/index.js";

function bodyFrom(chunks) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(iterable) {
  const out = [];
  for await (const item of iterable) out.push(item);
  return out;
}

test("readSse keeps id and event when an event is split across chunks", async () => {
  const messages = await collect(
    readSse(bodyFrom(["id: 7\nev", "ent: NODE_STARTED\nda", 'ta: {"node_id":"n1"}\n', "\n"]))
  );
  assert.deepEqual(messages, [{ id: "7", event: "NODE_STARTED", data: '{"node_id":"n1"}' }]);
});

test("readSse handles CRLF, comments, multi-line data and drops an unterminated event", async () => {
  const messages = await collect(
    readSse(bodyFrom([": keep-alive\r\n", "id: 1\r\ndata: a\r\ndata: b\r\n\r\n", "id: 2\ndata: tail"]))
  );
  assert.deepEqual(messages, [
    { id: "1", event: "message", data: "a\nb" },
  ]);
});

test("chatStream yields ordered run events then the completed response", async () => {
  const original = globalThis.fetch;
  let seen;
  globalThis.fetch = async (url, options) => {
    seen = { url: String(url), options };
    return new Response(
      bodyFrom([
        'id: 1\nevent: run_event\ndata: {"seq":1,"type":"RUN_STARTED","timestamp":1,"data":{}}\n\n',
        'id: 2\nevent: run_event\ndata: {"seq":2,"type":"TOOL_STARTED","timestamp":2,"data":{"tool_name":"calculate"}}\n\n',
        'id: 3\nevent: chat_completed\ndata: {"reply":"42","delivery":"final_only","revision":2}\n\n',
      ]),
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    );
  };
  try {
    const client = new NexuraClient({ baseUrl: "http://svc", workspaceId: "ws-a" });
    const events = await collect(client.sessions.chatStream("s1", { message: "6*7" }));
    assert.equal(seen.url, "http://svc/api/v1/sessions/s1/chat/stream");
    assert.equal(seen.options.method, "POST");
    assert.equal(seen.options.headers["X-Workspace-ID"], "ws-a");
    assert.deepEqual(
      events.map((e) => [e.kind, e.seq]),
      [["run_event", 1], ["run_event", 2], ["chat_completed", 3]]
    );
    assert.equal(events[1].data.tool_name, "calculate");
    assert.equal(events[2].response.reply, "42");
  } finally {
    globalThis.fetch = original;
  }
});

test("chatStream surfaces chat_failed and HTTP errors with codes", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () =>
      new Response(bodyFrom(['id: 1\nevent: chat_failed\ndata: {"code":"ModelTimeoutError","message":"slow"}\n\n']));
    const client = new NexuraClient({ baseUrl: "http://svc" });
    const [failed] = await collect(client.sessions.chatStream("s1", { message: "x" }));
    assert.deepEqual(failed, { kind: "chat_failed", seq: 1, code: "ModelTimeoutError", message: "slow", persisted: false });

    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: { code: "CROSS_WORKSPACE_ACCESS_DENIED", message: "no" } }), {
        status: 403,
      });
    await assert.rejects(collect(client.sessions.chatStream("s1", { message: "x" })), (err) => {
      assert.equal(err.statusCode, 403);
      assert.equal(err.code, "CROSS_WORKSPACE_ACCESS_DENIED");
      return true;
    });
  } finally {
    globalThis.fetch = original;
  }
});
