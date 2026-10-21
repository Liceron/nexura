import { test, expect } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

/**
 * P15 real chat citations. No route interception: a document is ingested through the Knowledge page, the
 * playground's deterministic handler calls the real knowledge_search tool, and the footnotes render from
 * that tool output. Complements chat_citations.spec.ts (forged citations).
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

test.describe('Chat citations from real knowledge_search output (P15)', () => {
  test('ingest on Knowledge page, ask in playground, footnote links to the chunk', async ({ page }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    await page.getByRole('button', { name: 'Knowledge', exact: true }).click();
    await expect(page.locator('[data-testid="knowledge-view"]')).toBeVisible();
    await page.locator('[data-testid="upload-filename-input"]').fill('wombat_facts.txt');
    await page
      .locator('[data-testid="upload-content-textarea"]')
      .fill('Wombats sleep sixteen hours a day inside their burrows.');
    await page.locator('[data-testid="upload-document-button"]').click();
    await expect(
      page.locator('[data-testid^="document-item-"]').filter({ hasText: 'wombat_facts.txt' }),
    ).toBeVisible({ timeout: 10000 });

    await page.getByRole('button', { name: /Playground/i }).click();
    await expect(page.locator('[data-testid="chat-playground"]')).toBeVisible();
    await expect(page.locator('[data-testid="citation-footnotes"]')).toHaveCount(0);

    const streamed = page.waitForResponse((r) => /\/sessions\/[^/]+\/chat\/stream/.test(r.url()), {
      timeout: 15000,
    });
    await page.locator('[data-testid="chat-input-textarea"]').fill('How long do wombats sleep?');
    await page.locator('[data-testid="send-message-button"]').click();
    const response = await streamed;
    expect(response.status()).toBe(200);
    // The stream is the real one: its final body carries the real tool output.
    expect(await response.text()).toContain('knowledge_search');

    const footnotes = page.locator('[data-testid="citation-footnotes"]');
    await expect(footnotes).toBeVisible({ timeout: 10000 });
    const link = page.locator('[data-testid="citation-footnote-link-1"]');
    await expect(link).toHaveText('wombat_facts.txt');
    await expect(page.locator('[data-testid="citation-footnote-unavailable-1"]')).toHaveCount(0);

    await link.click();
    await expect(page).toHaveURL(/#\/knowledge/);
    await expect(page.locator('[data-testid="source-chunk-panel"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="source-chunk-title"]')).toContainText('wombat_facts.txt');
    await expect(page.locator('[data-testid="source-chunk-text"]')).toContainText('sixteen hours');
  });
});
