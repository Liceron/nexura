import { describe, expect, it } from 'vitest';
import { NexuraApiError } from '@liceron/nexura-sdk';
import { formatApiError } from '../../src/api/errors';

describe('formatApiError', () => {
  it('turns a 422 validation list into field messages with a plain lead', () => {
    const err = new NexuraApiError(422, 'VALIDATION_ERROR', 'replay_window_seconds: Input should be greater than or equal to 10', []);
    const text = formatApiError(err);
    expect(text).toBe(
      'Please check the form: replay_window_seconds: Input should be greater than or equal to 10 (VALIDATION_ERROR)',
    );
    expect(text).not.toContain('{"loc"');
    expect(text).not.toContain('[422]');
  });

  it('explains scoped keys and revision conflicts by code, other statuses by status', () => {
    expect(formatApiError(new NexuraApiError(403, 'RESOURCE_SCOPE_DENIED', 'denied'))).toContain(
      'limited to specific workflows or collections',
    );
    expect(formatApiError(new NexuraApiError(409, 'REVISION_CONFLICT', 'at revision 3'))).toContain('Reload and try again');
    expect(formatApiError(new NexuraApiError(404, 'SKILL_NOT_FOUND', "Unknown skill 'x'."))).toBe(
      "Not found. It may have been deleted, or it belongs to another workspace. Unknown skill 'x'. (SKILL_NOT_FOUND)",
    );
  });

  it('keeps the server code for unknown statuses and plain messages for other errors', () => {
    expect(formatApiError(new NexuraApiError(500, 'BOOM', 'internal'))).toBe('internal (BOOM)');
    expect(formatApiError(new Error('JSON syntax error'))).toBe('JSON syntax error');
    expect(formatApiError('plain')).toBe('plain');
  });
});
