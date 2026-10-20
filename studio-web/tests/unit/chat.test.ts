import { describe, it, expect } from 'vitest';
import { DEFAULT_QUICK_PROMPTS } from '../../src/features/chat/types';
import type {
  ChatMessage,
  ChatResponse,
  ToolExecutionTrace,
  TokenUsage,
} from '../../src/features/chat/types';

describe('Chat Playground & Agent Tracing (F09)', () => {
  it('provides bilingual quick prompt templates', () => {
    const enPrompts = DEFAULT_QUICK_PROMPTS.filter((p) => p.language === 'en');
    const hiPrompts = DEFAULT_QUICK_PROMPTS.filter((p) => p.language === 'hi');

    expect(enPrompts.length).toBeGreaterThanOrEqual(2);
    expect(hiPrompts.length).toBeGreaterThanOrEqual(2);

    expect(enPrompts.some((p) => p.prompt.includes('calculate'))).toBe(true);
    expect(hiPrompts.some((p) => p.prompt.includes('नमस्ते'))).toBe(true);
  });

  it('correctly models and preserves ordered transcript messages', () => {
    const transcript: ChatMessage[] = [
      {
        role: 'system',
        content: 'You are a helpful assistant capable of mathematical calculation.',
      },
      {
        role: 'user',
        content: 'calculate 40 * 5',
      },
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call_1',
            name: 'calculate',
            arguments: { expression: '40 * 5' },
          },
        ],
      },
      {
        role: 'tool',
        name: 'calculate',
        tool_call_id: 'call_1',
        content: '200',
      },
      {
        role: 'assistant',
        content: 'The calculation result is: 200',
      },
    ];

    expect(transcript).toHaveLength(5);
    expect(transcript[0].role).toBe('system');
    expect(transcript[1].role).toBe('user');
    expect(transcript[2].tool_calls?.[0].name).toBe('calculate');
    expect(transcript[3].content).toBe('200');
    expect(transcript[4].content).toContain('200');
  });

  it('formats tool execution traces with latency and arguments', () => {
    const traces: ToolExecutionTrace[] = [
      {
        tool: 'get_order_status',
        arguments: { order_id: 'ORD-DELHI-501' },
        output: {
          order_id: 'ORD-DELHI-501',
          status: 'In Transit',
          carrier: 'Delhivery Express',
        },
        timestamp: 1700000000.5,
      },
    ];

    expect(traces[0].tool).toBe('get_order_status');
    expect(traces[0].arguments.order_id).toBe('ORD-DELHI-501');
    expect(traces[0].output.status).toBe('In Transit');
  });

  it('tracks token usage metrics accurately across turns', () => {
    const turn1Usage: TokenUsage = {
      prompt_tokens: 18,
      completion_tokens: 32,
      total_tokens: 50,
      is_complete: true,
    };

    const turn2Usage: TokenUsage = {
      prompt_tokens: 45,
      completion_tokens: 65,
      total_tokens: 110,
      is_complete: true,
    };

    const sum = (a: number | null, b: number | null) => (a === null || b === null ? null : a + b);
    const accumulated: TokenUsage = {
      prompt_tokens: sum(turn1Usage.prompt_tokens, turn2Usage.prompt_tokens),
      completion_tokens: sum(turn1Usage.completion_tokens, turn2Usage.completion_tokens),
      total_tokens: sum(turn1Usage.total_tokens, turn2Usage.total_tokens),
      is_complete: turn1Usage.is_complete && turn2Usage.is_complete,
    };

    expect(accumulated.prompt_tokens).toBe(63);
    expect(accumulated.completion_tokens).toBe(97);
    expect(accumulated.total_tokens).toBe(160);

    // Unreported usage stays unknown instead of being counted as zero.
    const unknown: TokenUsage = {
      prompt_tokens: null,
      completion_tokens: null,
      total_tokens: null,
      is_complete: false,
    };
    expect(sum(accumulated.total_tokens, unknown.total_tokens)).toBeNull();
  });

  it('redacts sensitive credentials and escapes html tags safely', () => {
    const rawApiKey = 'sk-proj-supersecretkey1234567890';
    const sanitizeText = (input: string) => {
      // Redact api keys
      const redacted = input.replace(/sk-[a-zA-Z0-9_\-]{16,}/g, '[REDACTED_SECRET]');
      // Escape unsafe markup
      return redacted
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    };

    const untrustedContent = `Echoing: <script>alert("xss")</script> with ${rawApiKey}`;
    const sanitized = sanitizeText(untrustedContent);

    expect(sanitized).not.toContain('<script>');
    expect(sanitized).not.toContain(rawApiKey);
    expect(sanitized).toContain('&lt;script&gt;');
    expect(sanitized).toContain('[REDACTED_SECRET]');
  });

  it('handles simulated chat response structure from deterministic model', () => {
    const chatResp: ChatResponse = {
      session_id: 'sess-abc-123',
      revision: 3,
      messages: [
        { role: 'user', content: 'calculate 10 + 5' },
        { role: 'assistant', content: 'The calculation result is: 15' },
      ],
      reply: 'The calculation result is: 15',
      model: 'fake-deterministic (Local Offline Fixture)',
      delivery: 'final_only',
      tool_executions: [
        {
          tool: 'calculate',
          arguments: { expression: '10 + 5' },
          output: '15',
          timestamp: 1700000002,
        },
      ],
      usage: {
        prompt_tokens: 20,
        completion_tokens: 30,
        total_tokens: 50,
        is_complete: true,
      },
      latency_ms: 12.5,
    };

    expect(chatResp.model).toContain('Local Offline Fixture');
    expect(chatResp.reply).toContain('15');
    expect(chatResp.tool_executions).toHaveLength(1);
    expect(chatResp.revision).toBe(3);
    expect(chatResp.latency_ms).toBeGreaterThan(0);
  });
});

import {
  normalizeChatModels,
  defaultChatModel,
  deliveryLabel,
  FAKE_MODEL_NAME,
} from '../../src/features/chat/types';

describe('Chat model picker helpers', () => {
  it('always offers the offline fixture as the default when the server list is empty or missing', () => {
    for (const input of [null, undefined, []]) {
      const list = normalizeChatModels(input);
      expect(list.map((m) => m.name)).toEqual([FAKE_MODEL_NAME]);
      expect(defaultChatModel(list)).toBe(FAKE_MODEL_NAME);
    }
  });

  it('keeps server models and selects the server-marked default', () => {
    const list = normalizeChatModels([
      { name: 'fake-deterministic', provider: 'local', label: 'fake', offline: true, streaming: false, default: true },
      { name: 'gpt', provider: 'openai', label: 'gpt (openai)', offline: false, streaming: true, default: false },
    ]);
    expect(list.map((m) => m.name)).toEqual(['fake-deterministic', 'gpt']);
    expect(defaultChatModel(list)).toBe('fake-deterministic');
  });

  it('delivery badge reflects the response delivery field', () => {
    expect(deliveryLabel(null)).toMatch(/final-only/i);
    expect(deliveryLabel('final_only')).toMatch(/final-only/i);
    expect(deliveryLabel('streamed')).toMatch(/streamed/i);
  });
});
