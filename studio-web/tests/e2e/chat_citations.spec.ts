import { test, expect, type Page } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

/**
 * P11 chat citation footnotes.
 *
 * Real backend path: the documents are ingested into the real local knowledge index (deterministic fake
 * embedder) and the footnote chunk lookups (GET /knowledge/documents/{id}/chunks/{n}) hit the real backend.
 *
 * Mocked part (deliberate): the citations under test are ones no real tool would emit (an unknown document,
 * a forged chunk id, a markup title), so the final `chat_completed` event of POST /sessions/{id}/chat/stream
 * is rewritten to add one tool execution carrying them. The chat run itself is real (real session, real
 * streamed run). Real citations from the playground's knowledge_search tool (P15) are covered by
 * chat_citations_real.spec.ts.
 */

let cluster: TestCluster;

test.beforeAll(async () => {
  cluster = await startTestCluster();
});

test.afterAll(async () => {
  if (cluster) await cluster.stop();
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript((backendUrl) => {
    localStorage.setItem('nexura_api_url', backendUrl);
  }, cluster.backendUrl);
});

const MARKUP_TITLE = '<img src=x onerror=window.__pwned=1><b>bold</b>';

interface Chunk {
  chunk_id: string;
  document_id: string;
  chunk_index: number;
  text: string;
}

async function ingest(page: Page, filename: string, content: string): Promise<Chunk> {
  const up = await page.request.post(`${cluster.backendUrl}/api/v1/knowledge/documents/upload`, {
    headers: cluster.apiHeaders,
    data: { filename, content },
  });
  expect(up.status()).toBe(201);
  const doc = await up.json();
  const ch = await page.request.get(
    `${cluster.backendUrl}/api/v1/knowledge/documents/${doc.document_id}/chunks/0`,
  );
  expect(ch.status()).toBe(200);
  return (await ch.json()) as Chunk;
}

/** Keep the real stream; add citations to the final chat_completed body only. */
async function injectCitations(page: Page, citations: unknown[]) {
  await page.route('**/sessions/*/chat/stream', async (route) => {
    const real = await route.fetch();
    const text = await real.text();
    const out = text
      .split('\n\n')
      .map((block) => {
        if (!block.includes('event: chat_completed')) return block;
        return block
          .split('\n')
          .map((line) => {
            if (!line.startsWith('data: ')) return line;
            const body = JSON.parse(line.slice(6));
            body.tool_executions = [
              ...(body.tool_executions ?? []),
              {
                tool: 'knowledge_search',
                arguments: { query: 'quokka' },
                output: { citations },
                timestamp: Date.now() / 1000,
              },
            ];
            return `data: ${JSON.stringify(body)}`;
          })
          .join('\n');
      })
      .join('\n\n');
    await route.fulfill({ response: real, body: out });
  });
}

test.describe('Chat citation footnotes (P11)', () => {
  test('renders footnotes, opens the cited chunk, flags forged citations, and renders markup titles as text', async ({
    page,
  }) => {
    const real = await ingest(page, 'quokka_real.txt', 'Quokka habitat notes: the real cited chunk text.');
    const markup = await ingest(page, 'quokka_markup.txt', 'Second document whose citation carries a markup title.');
    const wrongId = await ingest(page, 'quokka_forged_id.txt', 'Third document cited with a forged chunk id.');

    await injectCitations(page, [
      // 1: valid citation, matching chunk id
      { document_id: real.document_id, chunk_index: 0, chunk_id: real.chunk_id, title: 'Quokka Real Source' },
      // 2: document id that does not exist
      { document_id: 'doc_does_not_exist', chunk_index: 0, chunk_id: 'chk_nonexistent', title: 'Ghost Doc' },
      // 3: real document, markup in title
      { document_id: markup.document_id, chunk_index: 0, chunk_id: markup.chunk_id, title: MARKUP_TITLE },
      // 4: real document and index, forged chunk id
      { document_id: wrongId.document_id, chunk_index: 0, chunk_id: 'chk_forged_0000', title: 'Forged Chunk Id' },
    ]);

    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);
    await page.getByRole('button', { name: /Playground/i }).click();
    await expect(page.locator('[data-testid="chat-playground"]')).toBeVisible();

    // No footnotes before any tool output exists.
    await expect(page.locator('[data-testid="citation-footnotes"]')).toHaveCount(0);

    // Register every wait before the send so no response can slip past.
    const lookedUp = (docId: string) =>
      page.waitForResponse(
        (r) => r.url().includes(`/knowledge/documents/${docId}/chunks/0`) && r.request().method() === 'GET',
        { timeout: 15000 },
      );
    const validLookups = [lookedUp(real.document_id), lookedUp(markup.document_id)];
    const streamed = page.waitForResponse((r) => /\/sessions\/[^/]+\/chat\/stream/.test(r.url()), {
      timeout: 15000,
    });

    await page.locator('[data-testid="chat-input-textarea"]').fill('calculate 2 + 2');
    await page.locator('[data-testid="send-message-button"]').click();
    expect((await streamed).status()).toBe(200);

    const footnotes = page.locator('[data-testid="citation-footnotes"]');
    await expect(footnotes).toBeVisible();
    await expect(footnotes).toContainText('Sources');
    for (const n of [1, 2, 3, 4]) {
      await expect(page.locator(`[data-testid="citation-footnote-${n}"]`)).toBeVisible();
    }

    // Forged: unknown document and mismatched chunk id show "source unavailable", with no link.
    for (const n of [2, 4]) {
      await expect(page.locator(`[data-testid="citation-footnote-unavailable-${n}"]`)).toHaveText(
        /source unavailable/i,
      );
      await expect(page.locator(`[data-testid="citation-footnote-link-${n}"]`)).toHaveCount(0);
    }
    // Valid ones stay linked once their real lookups have answered (before that every footnote is a
    // link, so asserting earlier would pass vacuously).
    for (const r of await Promise.all(validLookups)) expect(r.status()).toBe(200);
    // Let React commit whatever those responses changed (two frames) before the negative checks.
    await page.evaluate(
      () => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))),
    );
    for (const n of [1, 3]) {
      await expect(page.locator(`[data-testid="citation-footnote-unavailable-${n}"]`)).toHaveCount(0);
      await expect(page.locator(`[data-testid="citation-footnote-link-${n}"]`)).toHaveCount(1);
    }
    await expect(page.locator('[data-testid="citation-footnote-link-1"]')).toHaveText('Quokka Real Source');

    // Markup title is plain text: no element created, no handler fired.
    const markupLink = page.locator('[data-testid="citation-footnote-link-3"]');
    await expect(markupLink).toHaveText(MARKUP_TITLE);
    await expect(footnotes.locator('img')).toHaveCount(0);
    await expect(footnotes.locator('b')).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();

    // Clicking a valid footnote opens Knowledge focused on that chunk.
    await page.locator('[data-testid="citation-footnote-link-1"]').click();
    await expect(page).toHaveURL(/#\/knowledge/);
    const panel = page.locator('[data-testid="source-chunk-panel"]');
    await expect(panel).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="source-chunk-title"]')).toContainText('quokka_real.txt');
    await expect(page.locator('[data-testid="source-chunk-title"]')).toContainText('Chunk 0');
    await expect(page.locator('[data-testid="source-chunk-text"]')).toContainText(real.text);

    // The markup title also stays inert when its chunk is opened (focus panel uses the stored title).
    await page.getByRole('button', { name: /Playground/i }).click();
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  });
});
