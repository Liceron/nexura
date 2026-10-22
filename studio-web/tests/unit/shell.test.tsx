import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nProvider, useI18n } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';

describe('Shell & Localization', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/capabilities')) {
        return new Response(
          JSON.stringify({
            sdk_version: '0.4.0',
            active_profile: 'local-owner',
            supported_profiles: ['local-owner'],
            capabilities: {
              modules: true,
              validation: true,
              downloadable_export: true,
              live_events: true,
            },
          }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        );
      }
      if (url.includes('/workflows')) {
        return new Response(
          JSON.stringify({
            items: [
              {
                workflow_id: 'wf-local-test',
                name: 'Test Workflow',
                revision: 1,
                entry_node: 'n1',
                node_count: 1,
                spec: { name: 'Test Workflow', entry_node: 'n1', nodes: { n1: {} } },
              },
            ],
            total: 1,
            limit: 50,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        );
      }
      return new Response('{}', { status: 200 });
    });
  });

  it('renders header, navigation, and switches tabs when connected', async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <ApiProvider>
          <Shell />
        </ApiProvider>
      </I18nProvider>
    );

    // Header renders
    expect(screen.getByText('Nexura Studio')).toBeInTheDocument();

    // Connected state reached
    await waitFor(() => {
      expect(screen.getByText(/local-owner/i)).toBeInTheDocument();
    });

    // Workflows rendered
    await waitFor(() => {
      expect(screen.getByText('Test Workflow')).toBeInTheDocument();
    });

    // Round five: the System page heading matches its navigation name.
    await user.click(screen.getByRole('button', { name: 'System' }));
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'System' })).toBeInTheDocument();
      expect(screen.getByText('0.4.0')).toBeInTheDocument();
    });
  });

  it('switches between English and Hindi correctly', async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <ApiProvider>
          <Shell />
        </ApiProvider>
      </I18nProvider>
    );

    await waitFor(() => {
      expect(screen.getByText('Workflows')).toBeInTheDocument();
    });

    // Click language switcher
    const langBtn = screen.getByRole('button', { name: /^Toggle Language:/ });
    await user.click(langBtn);

    // Now Hindi labels render
    await waitFor(() => {
      expect(screen.getByText('नेक्सुरा स्टूडियो')).toBeInTheDocument();
      expect(screen.getByText('वर्कफ़्लो')).toBeInTheDocument();
    });
  });

  it('formats INR currency properly in both locales', () => {
    function CurrencyTest() {
      const { formatINR } = useI18n();
      return <div data-testid="inr">{formatINR(7500)}</div>;
    }

    render(
      <I18nProvider>
        <CurrencyTest />
      </I18nProvider>
    );

    const text = screen.getByTestId('inr').textContent;
    expect(text).toContain('7,500');
  });
});
