// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type {
  ChatMessage,
  SessionListItem,
  SessionDetail,
  ToolExecutionTrace,
  TokenUsage,
  ChatResponse,
  ChatModelInfo,
} from '@liceron/nexura-sdk';
import { englishT, type Translate } from '../../i18n';
import { NexuraApiError } from '@liceron/nexura-sdk';

export type {
  ChatMessage,
  SessionListItem,
  SessionDetail,
  ToolExecutionTrace,
  TokenUsage,
  ChatResponse,
  ChatModelInfo,
};

export interface QuickPrompt {
  id: string;
  label: string;
  prompt: string;
  language: 'en' | 'hi';
}

export const DEFAULT_QUICK_PROMPTS: QuickPrompt[] = [
  {
    id: 'en-calc',
    label: 'Math Calculation',
    prompt: 'calculate (120 * 5) + 45',
    language: 'en',
  },
  {
    id: 'en-order',
    label: 'Track Order',
    prompt: 'What is the delivery status for ORD-DELHI-501?',
    language: 'en',
  },
  {
    id: 'hi-greet',
    label: 'नमस्ते (Greeting)',
    prompt: 'नमस्ते! आप मेरी क्या मदद कर सकते हैं?',
    language: 'hi',
  },
  {
    id: 'hi-order',
    label: 'ऑर्डर स्थिति',
    prompt: 'मेरे ऑर्डर ORD-DELHI-501 की क्या स्थिति है?',
    language: 'hi',
  },
];

export const FAKE_MODEL_NAME = 'fake-deterministic';
export const FAKE_MODEL_LABEL = 'fake-deterministic (Local Offline Fixture)';

/** Models shown in the picker; always includes the offline fixture, default first. */
export function normalizeChatModels(models: ChatModelInfo[] | null | undefined): ChatModelInfo[] {
  const list = (models ?? []).filter((m) => m && typeof m.name === 'string');
  if (!list.some((m) => m.name === FAKE_MODEL_NAME)) {
    list.unshift({
      name: FAKE_MODEL_NAME,
      provider: 'local',
      label: FAKE_MODEL_LABEL,
      offline: true,
      streaming: false,
      default: true,
    });
  }
  return list;
}

/** The default model name: server-marked default, else the offline fixture. */
export function defaultChatModel(models: ChatModelInfo[]): string {
  return models.find((m) => m.default)?.name ?? FAKE_MODEL_NAME;
}

/** Delivery badge text from the response's `delivery` field (null = no response yet). */
export function deliveryLabel(delivery: string | null | undefined, t: Translate = englishT): string {
  if (delivery === 'streamed' || delivery === 'streaming') {
    return t('chat.delivery.streaming');
  }
  return t('chat.delivery.final');
}

/** A saved agent's tool call that needed approval and so was not run in chat. */
export type RefusedToolCall = NonNullable<ChatResponse['refused_tool_calls']>[number];

/** Codes of a saved agent that ran out of turns (non-streaming and streaming paths). */
const TURN_LIMIT_CODES = new Set(['AGENT_TURN_LIMIT', 'MaxTurnsExceededError']);

/** The service code of a failed chat turn (an API error or a `chat_failed` event code). */
export function chatFailureCode(source: unknown): string | null {
  if (source instanceof NexuraApiError) return source.code;
  return typeof source === 'string' && source ? source : null;
}

/** True when a saved agent reached its turn limit (usually asking for tools that need approval). */
export function isTurnLimitCode(code: string | null | undefined): boolean {
  return Boolean(code && TURN_LIMIT_CODES.has(code));
}
