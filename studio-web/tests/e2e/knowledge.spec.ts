import { test, expect, type Page } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

let cluster: TestCluster;

test.beforeAll(async () => {
  cluster = await startTestCluster();
});

test.afterAll(async () => {
  if (cluster) {
    await cluster.stop();
  }
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript((backendUrl) => {
    localStorage.setItem('nexura_api_url', backendUrl);
  }, cluster.backendUrl);
});

/** Only labelled scroll regions may scroll sideways; the document itself must fit the viewport. */
async function expectNoPageOverflow(page: Page) {
  const { scrollWidth, innerWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
}

test.describe('Knowledge Ingestion, Scoped Retrieval and Citations (F10)', () => {
  test('ingests multilingual documents, retrieves citations with character spans, queries grounded agent, deletes document and verifies subsequent exclusion', async ({
    page,
  }) => {
    // 1. Navigate to Studio and open Knowledge Base
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    const knowledgeTab = page.getByRole('button', { name: 'Knowledge', exact: true });
    await knowledgeTab.click();
    await expect(page).toHaveURL(/#\/knowledge/);

    const knowledgeView = page.locator('[data-testid="knowledge-view"]');
    await expect(knowledgeView).toBeVisible();

    // Documents is the first tab; Add document opens the upload/paste form in place.
    await expect(page.getByRole('tab', { name: 'Documents', exact: true })).toHaveAttribute('aria-selected', 'true');
    await page.getByRole('button', { name: 'Add document', exact: true }).click();

    // 2. Ingest English document using sample button
    const sampleEnBtn = page.locator('[data-testid="sample-en-btn"]');
    await sampleEnBtn.click();

    const uploadBtn = page.locator('[data-testid="upload-document-button"]');
    await uploadBtn.click();

    // Verify document appears in list
    const enDocItem = page.locator('[data-testid^="document-item-"]').filter({ hasText: 'architecture_guide.md' });
    await expect(enDocItem).toBeVisible({ timeout: 10000 });

    // 3. Ingest Multilingual Hindi document using sample button
    const sampleHiBtn = page.locator('[data-testid="sample-hi-btn"]');
    await sampleHiBtn.click();
    await uploadBtn.click();

    // Verify Hindi document appears in list
    const hiDocItem = page.locator('[data-testid^="document-item-"]').filter({ hasText: 'hindi_guide.txt' });
    await expect(hiDocItem).toBeVisible({ timeout: 10000 });

    // 4. Test Hybrid Retrieval: English query
    const searchTab = page.getByRole('tab', { name: 'Search', exact: true });
    await searchTab.click();
    await expect(searchTab).toHaveAttribute('aria-selected', 'true');
    const searchInput = page.locator('[data-testid="retrieval-query-input"]');
    await searchInput.fill('standalone SDK SQLite persistence');

    const searchBtn = page.locator('[data-testid="search-retrieval-button"]');
    await searchBtn.click();

    // Verify citation card appears with character span and source origin
    const citation0 = page.locator('[data-testid="citation-card-0"]');
    await expect(citation0).toBeVisible({ timeout: 10000 });
    await expect(citation0.locator('[data-testid="citation-origin"]')).toContainText('architecture_guide.md');
    await expect(citation0.locator('[data-testid="citation-snippet"]')).toContainText('SQLite session persistence');
    await expect(citation0).toContainText(/Span: chars/i);

    // 5. Test Multilingual Hindi Retrieval
    await searchInput.fill('स्वतंत्र और शक्तिशाली ढांचा');
    await searchBtn.click();

    const hiCitation = page.locator('[data-testid="citation-origin"]').filter({ hasText: 'hindi_guide.txt' });
    await expect(hiCitation).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="citation-snippet"]').filter({ hasText: 'नेक्सुरा स्टूडियो' })).toBeVisible();

    // 6. Test Grounded Knowledge Agent
    const agentTab = page.getByRole('tab', { name: 'Ask documents', exact: true });
    await agentTab.click();
    await expect(agentTab).toHaveAttribute('aria-selected', 'true');

    // Verify visibly labelled fixture badge
    const agentBadge = page.locator('[data-testid="knowledge-agent-badge"]');
    await expect(agentBadge).toHaveText('Ask documents · Offline fake model');

    const questionInput = page.getByLabel('Question', { exact: true });
    await questionInput.fill('What is the architecture of Nexura SDK?');

    const askBtn = page.getByRole('button', { name: 'Send', exact: true });
    await askBtn.click();

    // Verify grounded answer box renders with bracketed citation [1]
    const answerBox = page.locator('[data-testid="agent-answer-box"]');
    await expect(answerBox).toBeVisible({ timeout: 15000 });
    await expect(answerBox).toContainText('[1]');

    // Verify reference citations list
    const refCitation = page.locator('[data-testid="answer-citation-0"]');
    await expect(refCitation).toBeVisible();
    await expect(refCitation).toContainText('[1]');

    // 7. Delete English document (the list lives on the Documents tab)
    await page.getByRole('tab', { name: 'Documents', exact: true }).click();

    const deleteEnBtn = enDocItem.locator('button[data-testid^="delete-doc-"]');
    await deleteEnBtn.click();
    await enDocItem.getByRole('button', { name: 'Confirm', exact: true }).click();

    // Verify English document is removed from document list
    await expect(enDocItem).not.toBeVisible({ timeout: 10000 });

    // 8. Verify subsequent retrieval excludes the deleted document
    await searchTab.click();
    await searchInput.fill('standalone SDK SQLite persistence');
    await searchBtn.click();

    // Wait for search to complete (either alert or citation card appears)
    await expect(
      page.locator('[data-testid="insufficient-evidence-alert"]').or(page.locator('[data-testid="citation-card-0"]'))
    ).toBeVisible({ timeout: 10000 });

    // The deleted document architecture_guide.md must NOT be returned in any citation
    const anyEnCitation = page.locator('[data-testid="citation-origin"]').filter({ hasText: 'architecture_guide.md' });
    await expect(anyEnCitation).not.toBeVisible();
  });

  test('file picker upload shows size limit, highlights snippets safely, refuses unsupported files', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await page.getByRole('button', { name: 'Knowledge', exact: true }).click();
    await expect(page.locator('[data-testid="knowledge-view"]')).toBeVisible();
    await page.getByRole('button', { name: 'Add document', exact: true }).click();

    // Size limit is displayed next to the drop zone
    await expect(page.locator('[data-testid="knowledge-size-limit"]')).toContainText(/Max per file/i);

    // Upload an HTML file with a script tag through the file picker
    const html =
      '<html><body><script>window.__pwned = 1;</script><p>Quokka habitat notes for reciprocal rank fusion.</p></body></html>';
    await page.locator('[data-testid="knowledge-file-input"]').setInputFiles({
      name: 'quokka.html',
      mimeType: 'text/html',
      buffer: Buffer.from(html),
    });
    const docItem = page.locator('[data-testid^="document-item-"]').filter({ hasText: 'quokka.html' });
    await expect(docItem).toBeVisible({ timeout: 10000 });
    await expect(docItem.locator('[data-testid="doc-status"]')).toBeVisible();

    // Unsupported type is refused, not listed
    await page.locator('[data-testid="knowledge-file-input"]').setInputFiles({
      name: 'malware.exe',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from('MZ'),
    });
    await expect(page.locator('[data-testid^="document-item-"]').filter({ hasText: 'malware.exe' })).toHaveCount(0);

    // Search shows highlighted snippet built from text nodes; script content never searchable or executed
    await page.getByRole('tab', { name: 'Search', exact: true }).click();
    await page.locator('[data-testid="retrieval-query-input"]').fill('quokka');
    await page.locator('[data-testid="search-retrieval-button"]').click();
    // Rank order depends on the non-semantic fake embedder (dense weight 0.7), so pick the
    // quokka.html card by source rather than assuming it is first.
    const card = page.locator('[data-testid^="citation-card-"]').filter({ hasText: 'quokka.html' });
    await expect(card).toHaveCount(1, { timeout: 10000 });
    await expect(card.locator('[data-testid="snippet-highlight"]').first()).toContainText(/quokka/i);
    await expect(card.locator('[data-testid="citation-snippet"]')).not.toContainText('__pwned');
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();

    // Markup typed as a query is rendered as inert text
    await page.locator('[data-testid="retrieval-query-input"]').fill('<img src=x onerror=window.__pwned=1>');
    await page.locator('[data-testid="search-retrieval-button"]').click();
    await page.waitForTimeout(500);
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
    await expect(page.locator('[data-testid="knowledge-view"] img')).toHaveCount(0);
  });

  test('phone: the documents table scrolls, the delete confirmation is readable and long source names stay on screen', async ({
    page,
  }) => {
    const longName = 'hindi_guide_with_a_rather_long_file_name_for_wrapping_checks.txt';
    const up = await page.request.post(`${cluster.backendUrl}/api/v1/knowledge/documents/upload`, {
      headers: cluster.apiHeaders,
      data: { filename: longName, content: 'Platypus burrow notes for the phone layout check.' },
    });
    expect(up.status()).toBe(201);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${cluster.frontendUrl}/#/knowledge`);

    const table = page.getByRole('table', { name: 'Documents in this collection' });
    const row = table.getByRole('row').filter({ hasText: longName });
    await expect(row).toBeVisible({ timeout: 10000 });
    // Headers keep whole words; the table scrolls inside its labelled region instead of squeezing.
    const header = table.getByRole('columnheader', { name: 'Characters' });
    expect((await header.boundingBox())!.height).toBeLessThan(40);
    const scroller = page.getByRole('region', { name: 'Documents in this collection' });
    expect(await scroller.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    await expectNoPageOverflow(page);
    // File names break into readable pieces, not one character per line.
    expect((await row.getByRole('cell').first().boundingBox())!.width).toBeGreaterThanOrEqual(190);

    await row.getByRole('button', { name: `Delete document ${longName}` }).click();
    const consequence = row.getByRole('status');
    await expect(consequence).toHaveText('Deleting this document removes it from knowledge retrieval.');
    expect((await consequence.boundingBox())!.width).toBeGreaterThan(150);
    await expectNoPageOverflow(page);
    await row.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(row).toBeVisible();

    await page.getByRole('button', { name: 'Add document', exact: true }).click();
    await expect(page.getByTestId('knowledge-size-limit')).toBeVisible();
    await expectNoPageOverflow(page);

    await page.getByRole('tab', { name: 'Search', exact: true }).click();
    await page.getByLabel('Search query').fill('platypus burrow');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    const origin = page.getByTestId('citation-origin').filter({ hasText: longName });
    await expect(origin).toBeVisible({ timeout: 10000 });
    const originBox = (await origin.boundingBox())!;
    expect(originBox.x + originBox.width).toBeLessThanOrEqual(390);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    await page.getByRole('tab', { name: 'Ask documents', exact: true }).click();
    await page.getByLabel('Question', { exact: true }).fill('What do platypus burrow notes say?');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(
      page.getByTestId('agent-answer-box').or(page.getByTestId('insufficient-evidence-banner')),
    ).toBeVisible({ timeout: 15000 });
    await expectNoPageOverflow(page);
  });
});
