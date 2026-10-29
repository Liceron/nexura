import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { ApiProvider } from '../../src/api/context';
import { I18nProvider } from '../../src/i18n';
import { WorkflowList } from '../../src/features/workflows/WorkflowList';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';
import { CreateWorkflowModal } from '../../src/features/workflows/CreateWorkflowModal';
import { INCOMPATIBLE_TEST_TEMPLATE } from '../../src/features/workflows/templates';

describe('Workflow Library, Templates & Lifecycle (F03)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders workflow list with search bar, templates and authoritative revision badge', async () => {
    const mockList = {
      items: [
        {
          workflow_id: 'wf-alpha-123',
          name: 'OrderProcessingWorkflow',
          revision: 3,
          entry_node: 'lookup',
          node_count: 3,
          created_at: 1700000000,
          updated_at: 1700000500,
        },
      ],
      total: 1,
      limit: 10,
      offset: 0,
      has_more: false,
    };

    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/capabilities')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              sdk_version: '0.4.0',
              active_profile: 'local-owner',
              supported_profiles: ['local-owner'],
              credential_types: [],
              feature_flags: {},
            }),
        });
      }
      if (url.includes('/workflows')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(mockList),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    render(
      <I18nProvider>
        <ApiProvider>
          <WorkflowList onSelectWorkflow={vi.fn()} />
        </ApiProvider>
      </I18nProvider>
    );

    // Real rows lead; templates remain reachable from creation.
    const link = await screen.findByRole('link', { name: 'OrderProcessingWorkflow' });
    expect(link).toHaveAttribute('href', '#/workflows/wf-alpha-123');
    const row = link.closest('tr')!;
    expect(within(row).getAllByRole('cell')[3]).toHaveTextContent(/^3$/);
    expect(screen.getByLabelText('Search workflows input')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'New workflow' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Order support with refund approval')).toBeInTheDocument();
    expect(screen.getByText('English and Hindi greeting')).toBeInTheDocument();

  });

  it('pre-validates templates and blocks creation of incompatible/unsupported module versions', async () => {
    global.fetch = vi.fn().mockImplementation((url: string, opts: { method?: string }) => {
      if (url.includes('/capabilities')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              sdk_version: '0.4.0',
              active_profile: 'local-owner',
              supported_profiles: ['local-owner'],
            }),
        });
      }
      if (url.includes('/workflows?')) return Promise.resolve({ ok: true, json: () => Promise.resolve({ items: [], has_more: false }) });
      if (url.includes('/workflows/validate') && opts?.method === 'POST') {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              valid: false,
              errors: [
                {
                  node_id: 'unsupported_step',
                  field: 'handler',
                  code: 'UNKNOWN_HANDLER',
                  message: "Node 'unsupported_step' references unknown handler 'nonexistent.billing.v99'",
                },
              ],
              warnings: [],
            }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    const onCreated = vi.fn();
    render(
      <I18nProvider>
        <ApiProvider>
          <CreateWorkflowModal
            open={true}
            onClose={vi.fn()}
            onCreated={onCreated}
            initialTemplateId="incompatible-module-template"
            extraTemplates={[INCOMPATIBLE_TEST_TEMPLATE]}
          />
        </ApiProvider>
      </I18nProvider>
    );

    fireEvent.change(screen.getByLabelText('Workflow Name'), { target: { value: 'Incompatible validation check' } });
    const submitBtn = screen.getByRole('button', { name: /Validate & Create/i });
    fireEvent.click(submitBtn);

    // Validation error rendered
    expect(await screen.findByText(/Template Validation Failed/i)).toBeInTheDocument();
    expect(screen.getByText(/UNKNOWN_HANDLER/i)).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it('recovers cleanly when deep linked workflow is missing or deleted (404)', async () => {
    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/capabilities')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              sdk_version: '0.4.0',
              active_profile: 'local-owner',
            }),
        });
      }
      if (url.includes('/workflows/wf-nonexistent')) {
        return Promise.resolve({
          ok: false,
          status: 404,
          statusText: 'Not Found',
          json: () =>
            Promise.resolve({
              detail: {
                code: 'WORKFLOW_NOT_FOUND',
                message: "Workflow 'wf-nonexistent' not found.",
              },
            }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    const onBack = vi.fn();
    render(
      <I18nProvider>
        <ApiProvider>
          <WorkflowDetail
            workflowId="wf-nonexistent"
            onBack={onBack}
            onOpenWorkflow={vi.fn()}
          />
        </ApiProvider>
      </I18nProvider>
    );

    // Verify 404 error recovery UI
    expect(await screen.findByText('Workflow Not Found')).toBeInTheDocument();
    expect(
      screen.getByText('The requested workflow does not exist or has been deleted.')
    ).toBeInTheDocument();

    const backBtn = screen.getByRole('button', { name: /Back to Workflows/i });
    fireEvent.click(backBtn);
    expect(onBack).toHaveBeenCalled();
  });

  it('displays authoritative revision and canonical hash on valid workflow detail', async () => {
    const mockWorkflow = {
      workflow_id: 'wf-demo-456',
      name: 'OrderSupportWorkflow',
      revision: 7,
      spec_hash: 'a1b2c3d4e5f67890abcdef1234567890',
      spec: {
        name: 'OrderSupportWorkflow',
        entry_node: 'lookup',
        nodes: {
          lookup: {
            id: 'lookup',
            handler: 'synthetic_order_lookup',
            default_transition: 'route',
            requires_approval: false,
          },
        },
      },
      created_at: 1700000000,
      updated_at: 1700000900,
    };

    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/capabilities')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              sdk_version: '0.4.0',
              active_profile: 'local-owner',
            }),
        });
      }
      if (url.includes('/workflows/wf-demo-456')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(mockWorkflow),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    render(
      <I18nProvider>
        <ApiProvider>
          <WorkflowDetail
            workflowId="wf-demo-456"
            onBack={vi.fn()}
            onOpenWorkflow={vi.fn()}
          />
        </ApiProvider>
      </I18nProvider>
    );

    // Verify authoritative revision badge
    expect(await screen.findByText('Revision: 7')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Details', { exact: true }));
    expect(screen.getByText(/a1b2c3d4/i)).toBeInTheDocument();
    expect(screen.getByText('OrderSupportWorkflow')).toBeInTheDocument();
  });
});
