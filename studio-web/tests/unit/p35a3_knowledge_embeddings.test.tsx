import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { I18nProvider } from '../../src/i18n';
import { KnowledgeView } from '../../src/features/knowledge/KnowledgeView';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';

const api = {
  manager: { client: {
    request: vi.fn(),
    knowledge: {
      listCollections: vi.fn(),
      listDocuments: vi.fn(),
    },
  } },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  api.manager.client.knowledge.listCollections.mockResolvedValue({ collections: ['default'] });
  api.manager.client.knowledge.listDocuments.mockResolvedValue({ items: [] });
});
afterEach(cleanup);

function limits(isFixture: boolean) {
  api.manager.client.request.mockResolvedValue({
    max_file_bytes: 5000000, max_workspace_bytes: 50000000, max_top_k: 50,
    workspace_used_bytes: 0, is_test_fixture: isFixture,
  });
}

describe('Knowledge embedding honesty', () => {
  it.each(['en', 'hi'] as const)('shows the fixture notice in %s from the API flag', async (language) => {
    limits(true);
    localStorage.setItem('nexura_locale', language);
    render(<I18nProvider><KnowledgeView /></I18nProvider>);
    expect(await screen.findByRole('note')).toHaveTextContent(
      (language === 'en' ? en : hi).knowledge.testEmbeddingsNotice,
    );
    expect(screen.getByRole('note')).toHaveTextContent('NEXURA_EMBEDDINGS');
    if (language === 'hi') expect(screen.getByRole('note')).toHaveTextContent('अर्थ के आधार पर नहीं');
  });

  it('hides the fixture notice when a real provider is configured', async () => {
    limits(false);
    render(<I18nProvider><KnowledgeView /></I18nProvider>);
    await waitFor(() => expect(api.manager.client.knowledge.listDocuments).toHaveBeenCalled());
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });
});
