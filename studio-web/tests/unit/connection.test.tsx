import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiProvider, useApi } from '../../src/api/context';
import { I18nProvider } from '../../src/i18n';
import { Shell } from '../../src/app/Shell';

function TestConsumer() {
  const { connection, changeWorkspace, changeBaseUrl } = useApi();
  return (
    <div>
      <div data-testid="status">{connection.status}</div>
      <div data-testid="workspace">{connection.workspace}</div>
      <div data-testid="url">{connection.baseUrl}</div>
      <button onClick={() => changeWorkspace('new-ws')}>Switch WS</button>
      <button onClick={() => changeBaseUrl('http://127.0.0.1:9999')}>Switch URL</button>
    </div>
  );
}

describe('Connection & ApiContext', () => {
  it('provides connection state and updates workspace and url', async () => {
    const user = userEvent.setup();
    render(
      <ApiProvider initialBaseUrl="/api" initialWorkspace="test-ws">
        <TestConsumer />
      </ApiProvider>
    );

    expect(screen.getByTestId('workspace').textContent).toBe('test-ws');
    expect(screen.getByTestId('url').textContent).toBe('/api');

    await user.click(screen.getByText('Switch WS'));
    expect(screen.getByTestId('workspace').textContent).toBe('new-ws');

    await user.click(screen.getByText('Switch URL'));
    expect(screen.getByTestId('url').textContent).toBe('http://127.0.0.1:9999');
  });

  it('renders recoverable error when service is offline and limits retries', async () => {
    // When service is offline, Shell shows error card with retry button
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Network error'));

    render(
      <I18nProvider>
        <ApiProvider initialBaseUrl="http://unreachable-host:9999">
          <Shell />
        </ApiProvider>
      </I18nProvider>
    );

    // Initial check triggers exactly one fetch
    await waitFor(() => {
      expect(screen.getByText(/Unable to connect to Nexura Service/i)).toBeInTheDocument();
    });

    const initialFetchCount = fetchSpy.mock.calls.length;
    expect(initialFetchCount).toBe(1);

    // Wait a brief moment to ensure no automated infinite retry storm occurs
    await act(async () => {
      await new Promise((r) => setTimeout(r, 200));
    });

    // Count should NOT have increased automatically
    expect(fetchSpy.mock.calls.length).toBe(initialFetchCount);

    fetchSpy.mockRestore();
  });
});
