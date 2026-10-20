import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { RunRecord } from '@liceron/nexura-sdk';
import { I18nProvider } from '../../src/i18n';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';
import { RunCancelReasonNotice, runCancelReason } from '../../src/features/runs/RunCancelReasonNotice';

function run(status: RunRecord['status'], cancel_reason?: string | null): RunRecord {
  return {
    run_id: 'run-1',
    spec_hash: 'h',
    status,
    step_count: 1,
    input_state: {},
    current_state: {},
    error: 'Cancelled: <b>not html</b>',
    created_at: 1,
    updated_at: 2,
    ...(cancel_reason !== undefined ? { cancel_reason } : {}),
  } as RunRecord;
}

function renderNotice(r: RunRecord) {
  localStorage.setItem('nexura_locale', 'en');
  return render(
    <I18nProvider>
      <RunCancelReasonNotice run={r} />
    </I18nProvider>,
  );
}

describe('B40 run cancel reason notice', () => {
  it('explains a run cancelled because access was revoked', () => {
    renderNotice(run('cancelled', 'access_revoked'));
    const notice = screen.getByTestId('run-cancel-reason');
    expect(notice.textContent).toContain(en.runs.cancelReasonTitle);
    expect(notice.textContent).toContain(en.runs.cancelReason_access_revoked);
  });

  it('explains a run cancelled because its publication was revoked', () => {
    renderNotice(run('cancelled', 'publication_revoked'));
    expect(screen.getByTestId('run-cancel-reason').textContent).toContain(
      en.runs.cancelReason_publication_revoked,
    );
  });

  it('shows an unknown reason as plain text and nothing for ordinary runs', () => {
    renderNotice(run('cancelled', '<img src=x>'));
    const notice = screen.getByTestId('run-cancel-reason');
    expect(notice.textContent).toContain('<img src=x>');
    expect(notice.querySelector('img')).toBeNull();
    expect(runCancelReason(run('cancelled'))).toBeNull();
    expect(runCancelReason(run('cancelled', null))).toBeNull();
    expect(runCancelReason(run('pending_approval', 'access_revoked'))).toBeNull();
  });

  it('has Hindi strings for every reason', () => {
    for (const key of ['cancelReasonTitle', 'cancelReason_access_revoked', 'cancelReason_publication_revoked'] as const) {
      expect(hi.runs[key]).toBeTruthy();
      expect(hi.runs[key]).not.toBe(en.runs[key]);
    }
  });
});
