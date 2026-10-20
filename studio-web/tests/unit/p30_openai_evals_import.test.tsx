// P30: the Studio dataset import accepts an OpenAI Evals JSONL file. Detection is client-side;
// the conversion is the service's (POST /evaluations/studio/datasets/import-openai, the same
// converter as `nexura-runtime evals import-openai`). Fixtures are the synthetic files under
// tests/fixtures/openai_evals. Mocked API; no network.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactNode } from 'react';
import { NexuraApiError } from '@liceron/nexura-sdk';

const FIXTURES = resolve(__dirname, '../../../tests/fixtures/openai_evals');
const fixture = (name: string) => readFileSync(resolve(FIXTURES, name), 'utf-8');

const state: { request: (method: string, path: string, options?: { body?: unknown }) => unknown } = {
  request: () => ({}),
};
const requests: Array<{ method: string; path: string; body?: unknown }> = [];
const client = {
  request: async <T,>(method: string, path: string, options?: { body?: unknown }): Promise<T> => {
    requests.push({ method, path, body: options?.body });
    return (await state.request(method, path, options)) as T;
  },
};
const api = { manager: { client, clearCache: () => undefined }, connection: { status: 'connected', workspace: 'w' } };
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { I18nProvider } from '../../src/i18n';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';
import { StudioEvalsPanel } from '../../src/features/evaluations/StudioEvalsPanel';
import {
  importFieldErrors,
  looksLikeOpenAiEvals,
  parseDatasetImport,
  withoutNulls,
} from '../../src/features/evaluations/evals_dataset_json';

const wrap = (ui: ReactNode) => render(<I18nProvider>{ui}</I18nProvider>);

const IMPORT_PATH = '/evaluations/studio/datasets/import-openai';

function converted() {
  return {
    dataset: {
      format: 'nexura.eval-dataset',
      dataset_id: 'tickets',
      name: 'tickets',
      description: 'Imported from OpenAI Evals (3 items).',
      cases: [
        { name: 'item-0001', input: "My monitor won't turn on!", expected_output: 'Hardware', expected_trajectory: null, expected_agent_path: null, tags: ['openai-evals'], metadata: { openai_line: 1 } },
        { name: 'item-0002', input: "I'm in vim and I can't quit!", expected_output: 'Software', expected_trajectory: null, expected_agent_path: null, tags: ['openai-evals'], metadata: { openai_line: 2 } },
      ],
    },
    report: {
      source: 'openai-evals',
      dataset_id: 'tickets',
      cases: 2,
      input_field: 'ticket_text',
      expected_field: 'correct_label',
      scorers: [{ type: 'exact_match' }],
      mapped_criteria: [],
      unmapped_criteria: [{ name: 'Custom code', type: 'python', reason: 'Nexura never runs uploaded code' }],
      skipped_lines: [],
      samples_ignored: 0,
      redactions: 0,
      warnings: [],
    },
  };
}

beforeEach(() => {
  requests.length = 0;
  state.request = (method, path) => {
    if (method === 'GET' && path.endsWith('/datasets')) return { datasets: [] };
    if (method === 'GET' && path.endsWith('/targets')) return { targets: [] };
    if (method === 'GET' && path.endsWith('/runs')) return { runs: [] };
    if (method === 'POST' && path === IMPORT_PATH) return converted();
    return {};
  };
});

describe('OpenAI Evals detection', () => {
  it.each([
    ['documented {item} JSONL', 'tickets.jsonl'],
    ['registry rows', 'registry_samples.jsonl'],
    ['inline run source', 'run_content.json'],
    ['custom fields', 'custom_fields.jsonl'],
    ['malformed JSONL (still sent so the server reports the lines)', 'malformed.jsonl'],
  ])('recognises %s', (_label, name) => {
    expect(looksLikeOpenAiEvals(fixture(name))).toBe(true);
  });

  it('recognises a single {item} line and a JSON array of {item} rows', () => {
    expect(looksLikeOpenAiEvals('{"item": {"input": "x"}}')).toBe(true);
    expect(looksLikeOpenAiEvals('[{"item": {"input": "x"}}]')).toBe(true);
  });

  it.each([
    ['a Nexura export', JSON.stringify({ format: 'nexura.eval-dataset', cases: [{ name: 'a', input: 'x' }] })],
    ['a bare array of cases', JSON.stringify([{ name: 'a', input: 'x' }])],
    ['an object with cases', JSON.stringify({ cases: [] })],
    ['broken JSON', '{oops'],
    ['a scalar', '3'],
    ['empty text', '  '],
  ])('leaves %s to the Nexura dataset import', (_label, text) => {
    expect(looksLikeOpenAiEvals(text)).toBe(false);
  });

  it('keeps the Nexura import unchanged', () => {
    expect(parseDatasetImport(JSON.stringify([{ name: 'a', input: 'x' }])).ok).toBe(true);
  });

  it('formats line errors from a refused import', () => {
    const err = new NexuraApiError(422, 'OPENAI_IMPORT_INVALID', 'bad', {
      code: 'OPENAI_IMPORT_INVALID',
      field_errors: [{ field: 'line 2', error: 'not valid JSON' }],
    });
    expect(importFieldErrors(err)).toEqual(['line 2: not valid JSON']);
    expect(importFieldErrors(new Error('x'))).toEqual([]);
  });

  it('drops server nulls so the editor sees absent fields', () => {
    const [c] = withoutNulls(converted().dataset.cases);
    expect('expected_trajectory' in c).toBe(false);
    expect(c.expected_output).toBe('Hardware');
  });

  it('has every new string in English and Hindi', () => {
    const keys = Object.keys(en.evalsStudio).filter((k) => k.startsWith('oai') || k === 'importFile');
    expect(keys.length).toBeGreaterThanOrEqual(13);
    for (const k of keys) expect((hi.evalsStudio as Record<string, string>)[k], k).toBeTruthy();
  });
});

describe('Studio import of an OpenAI Evals file', () => {
  async function openImport() {
    const user = userEvent.setup();
    wrap(<StudioEvalsPanel />);
    await user.click(await screen.findByTestId('studio-evals-import'));
    return user;
  }

  it('sends the file and mapping to the converter route and loads the cases', async () => {
    const user = await openImport();
    fireEvent.change(screen.getByTestId('studio-evals-json'), { target: { value: fixture('tickets.jsonl') } });
    expect(await screen.findByTestId('studio-evals-oai-options')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('studio-evals-oai-input-field'), { target: { value: 'ticket_text' } });
    fireEvent.change(screen.getByTestId('studio-evals-oai-criteria'), { target: { value: fixture('tickets_eval.json') } });
    await user.click(screen.getByTestId('studio-evals-apply-import'));

    await waitFor(() => expect(requests.some((r) => r.method === 'POST' && r.path === IMPORT_PATH)).toBe(true));
    const post = requests.find((r) => r.path === IMPORT_PATH);
    const body = post?.body as Record<string, unknown>;
    expect(body.content).toBe(fixture('tickets.jsonl'));
    expect(body.dataset_id).toBe('openai-evals');
    expect(body.input_field).toBe('ticket_text');
    expect('expected_field' in body).toBe(false);
    expect((body.testing_criteria as { testing_criteria: unknown[] }).testing_criteria).toHaveLength(6);
    expect(body.skip_invalid).toBe(false);

    expect(await screen.findByTestId('studio-evals-case-item-0001')).toBeInTheDocument();
    expect(screen.getByTestId('studio-evals-case-item-0002')).toBeInTheDocument();
    expect((screen.getByTestId('studio-evals-dataset-id') as HTMLInputElement).value).toBe('openai-evals');
    expect(screen.getByTestId('studio-evals-oai-scorers').textContent).toContain('exact_match');
    expect(screen.getByTestId('studio-evals-oai-unmapped').textContent).toContain('Custom code');
    // Nothing was saved: no PUT until the user saves the dataset.
    expect(requests.some((r) => r.method === 'PUT')).toBe(false);
  });

  it('shows the line errors of a refused file and keeps the editor empty', async () => {
    state.request = (method, path) => {
      if (method === 'POST' && path === IMPORT_PATH) {
        throw new NexuraApiError(422, 'OPENAI_IMPORT_INVALID', '3 line(s) could not be converted', {
          code: 'OPENAI_IMPORT_INVALID',
          message: '3 line(s) could not be converted',
          field_errors: [
            { field: 'line 2', error: 'not valid JSON' },
            { field: 'line 4', error: "missing input field 'question'" },
          ],
        });
      }
      if (path.endsWith('/datasets')) return { datasets: [] };
      if (path.endsWith('/targets')) return { targets: [] };
      return { runs: [] };
    };
    const user = await openImport();
    fireEvent.change(screen.getByTestId('studio-evals-json'), { target: { value: fixture('malformed.jsonl') } });
    await user.click(await screen.findByTestId('studio-evals-oai-skip-invalid'));
    await user.click(screen.getByTestId('studio-evals-apply-import'));
    const errors = await screen.findByTestId('studio-evals-form-errors');
    expect(errors.textContent).toContain('line 2: not valid JSON');
    expect(errors.textContent).toContain("line 4: missing input field 'question'");
    expect(screen.getByTestId('studio-evals-error').textContent).toContain('OPENAI_IMPORT_INVALID');
    expect((requests.find((r) => r.path === IMPORT_PATH)?.body as { skip_invalid: boolean }).skip_invalid).toBe(true);
    expect(screen.queryByTestId('studio-evals-oai-report')).toBeNull();
  });

  it('refuses criteria that are not JSON without calling the service', async () => {
    const user = await openImport();
    fireEvent.change(screen.getByTestId('studio-evals-json'), { target: { value: fixture('tickets.jsonl') } });
    fireEvent.change(await screen.findByTestId('studio-evals-oai-criteria'), { target: { value: '{nope' } });
    await user.click(screen.getByTestId('studio-evals-apply-import'));
    expect((await screen.findByTestId('studio-evals-form-errors')).textContent).toContain('testing_criteria');
    expect(requests.some((r) => r.path === IMPORT_PATH)).toBe(false);
  });

  it('loads a .jsonl file from the file picker', async () => {
    const user = await openImport();
    const file = new File([fixture('registry_samples.jsonl')], 'samples.jsonl', { type: 'application/jsonl' });
    await user.upload(screen.getByTestId('studio-evals-import-file'), file);
    await waitFor(() =>
      expect((screen.getByTestId('studio-evals-json') as HTMLTextAreaElement).value).toBe(fixture('registry_samples.jsonl')),
    );
    expect(screen.getByTestId('studio-evals-oai-options')).toBeInTheDocument();
  });

  it('still imports a Nexura dataset client-side', async () => {
    const user = await openImport();
    fireEvent.change(screen.getByTestId('studio-evals-json'), {
      target: { value: JSON.stringify([{ name: 'local', input: 'hi' }]) },
    });
    expect(screen.queryByTestId('studio-evals-oai-options')).toBeNull();
    await user.click(screen.getByTestId('studio-evals-apply-import'));
    expect(await screen.findByTestId('studio-evals-case-local')).toBeInTheDocument();
    expect(requests.some((r) => r.path === IMPORT_PATH)).toBe(false);
  });
});
