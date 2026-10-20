import { test, expect } from '@playwright/test';
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

test.describe('Knowledge Ingestion, Scoped Retrieval and Citations (F10)', () => {
  test('ingests multilingual documents, retrieves citations with character spans, queries grounded agent, deletes document and verifies subsequent exclusion', async ({
    page,
  }) => {
    // 1. Navigate to Studio and open Knowledge Base
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    const knowledgeTab = page.getByRole('button', { name: /Knowledge Base/i });
    await knowledgeTab.click();
    await expect(page).toHaveURL(/#\/knowledge/);

    const knowledgeView = page.locator('[data-testid="knowledge-view"]');
    await expect(knowledgeView).toBeVisible();

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
    const agentTab = page.locator('[data-testid="tab-agent"]');
    await agentTab.click();

    // Verify visibly labelled fixture badge
    const agentBadge = page.locator('[data-testid="knowledge-agent-badge"]');
    await expect(agentBadge).toContainText(/Grounded Knowledge Agent \(Local Offline Fixture\)/i);

    const questionInput = page.locator('[data-testid="agent-question-input"]');
    await questionInput.fill('What is the architecture of Nexura SDK?');

    const askBtn = page.locator('[data-testid="ask-agent-button"]');
    await askBtn.click();

    // Verify grounded answer box renders with bracketed citation [1]
    const answerBox = page.locator('[data-testid="agent-answer-box"]');
    await expect(answerBox).toBeVisible({ timeout: 15000 });
    await expect(answerBox).toContainText('[1]');

    // Verify reference citations list
    const refCitation = page.locator('[data-testid="answer-citation-0"]');
    await expect(refCitation).toBeVisible();
    await expect(refCitation).toContainText('[1]');

    // 7. Delete English document
    const retTab = page.locator('[data-testid="tab-retrieval"]');
    await retTab.click();

    const deleteEnBtn = enDocItem.locator('button[data-testid^="delete-doc-"]');
    await deleteEnBtn.click();
    await enDocItem.getByRole('button', { name: 'Confirm', exact: true }).click();

    // Verify English document is removed from document list
    await expect(enDocItem).not.toBeVisible({ timeout: 10000 });

    // 8. Verify subsequent retrieval excludes the deleted document
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
    await page.getByRole('button', { name: /Knowledge Base/i }).click();
    await expect(page.locator('[data-testid="knowledge-view"]')).toBeVisible();

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
});
