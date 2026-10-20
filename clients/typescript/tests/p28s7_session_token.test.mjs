// P28 step 7 review R1: the client sends the local server's per-session token on mutating
// requests and reads it again (once) when the server refuses a missing or stale one.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NexuraClient, SESSION_HEADER } from '../dist/index.js';

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function withFetch(handler, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  return fn().finally(() => {
    globalThis.fetch = original;
  });
}

test('a configured token goes on mutating requests only', async () => {
  const calls = [];
  await withFetch(async (url, options) => {
    calls.push({ url: String(url), options });
    return json({ ok: true });
  }, async () => {
    const client = new NexuraClient({ baseUrl: 'http://svc', sessionToken: 'tok-1' });
    await client.workflows.list();
    await client.credentials.revoke('k1');
    assert.equal(SESSION_HEADER, 'X-Nexura-Session');
    assert.equal(calls[0].options.headers[SESSION_HEADER], undefined);
    assert.equal(calls[1].options.method, 'POST');
    assert.equal(calls[1].options.headers[SESSION_HEADER], 'tok-1');
    assert.equal(client.withWorkspace('other').sessionToken, 'tok-1');
  });
});

test('a session-token refusal reads the token from the server and retries once', async () => {
  const calls = [];
  let serverToken = 'fresh-token';
  await withFetch(async (url, options) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith('/api/v1/local-session')) return json({ token: serverToken, header: SESSION_HEADER });
    if (options.headers[SESSION_HEADER] !== serverToken) {
      return json({ detail: { code: 'SESSION_TOKEN_REQUIRED', message: 'need token' } }, 403);
    }
    return json({ session_id: 's1' }, 201);
  }, async () => {
    const client = new NexuraClient({ baseUrl: 'http://svc' });
    const created = await client.sessions.create({ title: 't' });
    assert.equal(created.session_id, 's1');
    assert.deepEqual(
      calls.map((c) => [c.options.method || 'GET', new URL(c.url).pathname]),
      [['POST', '/api/v1/sessions'], ['GET', '/api/v1/local-session'], ['POST', '/api/v1/sessions']]
    );
    assert.equal(client.sessionToken, 'fresh-token');
  });
});

test('other 403s, GETs and autoSessionToken:false are not retried', async () => {
  let tokenReads = 0;
  await withFetch(async (url) => {
    if (String(url).endsWith('/local-session')) {
      tokenReads += 1;
      return json({ token: 'x' });
    }
    if (String(url).includes('/workflows')) return json({ detail: { code: 'SESSION_TOKEN_INVALID', message: 'x' } }, 403);
    return json({ detail: { code: 'PERMISSION_DENIED', message: 'no' } }, 403);
  }, async () => {
    const client = new NexuraClient({ baseUrl: 'http://svc' });
    await assert.rejects(client.credentials.revoke('k1'), (e) => e.code === 'PERMISSION_DENIED');
    await assert.rejects(client.workflows.list(), (e) => e.code === 'SESSION_TOKEN_INVALID');
    const manual = new NexuraClient({ baseUrl: 'http://svc', autoSessionToken: false });
    await assert.rejects(manual.workflows.delete('wf'), (e) => e.code === 'SESSION_TOKEN_INVALID');
    assert.equal(tokenReads, 0);
  });
});

test('chatStream retries after a session-token refusal', async () => {
  const seen = [];
  await withFetch(async (url, options) => {
    seen.push(new URL(String(url)).pathname);
    if (String(url).endsWith('/local-session')) return json({ token: 'chat-token' });
    if (options.headers[SESSION_HEADER] !== 'chat-token') {
      return json({ detail: { code: 'SESSION_TOKEN_INVALID', message: 'stale' } }, 403);
    }
    const body = 'event: chat_completed\ndata: {"reply":"ok"}\n\n';
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }, async () => {
    const client = new NexuraClient({ baseUrl: 'http://svc', sessionToken: 'old' });
    const events = [];
    for await (const ev of client.sessions.chatStream('s1', { message: 'hi' })) events.push(ev);
    assert.equal(seen.length, 3);
    assert.equal(seen[1], '/api/v1/local-session');
    assert.ok(events.length >= 1);
  });
});
