// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
/**
 * Minimal Server-Sent Events reader for fetch response bodies.
 *
 * Event fields are accumulated across network chunks, so an event split between
 * two reads keeps its id and type.
 */

export interface SseMessage {
  id: string | null;
  event: string;
  data: string;
}

export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let id: string | null = null;
  let event = "message";
  let data: string[] = [];

  const flush = (): SseMessage | null => {
    const message = data.length > 0 ? { id, event, data: data.join("\n") } : null;
    id = null;
    event = "message";
    data = [];
    return message;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });

      let newline = buffer.search(/\r?\n/);
      while (newline >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + (buffer[newline] === "\r" ? 2 : 1));
        newline = buffer.search(/\r?\n/);

        if (line === "") {
          const message = flush();
          if (message) yield message;
          continue;
        }
        if (line.startsWith(":")) continue; // comment / keep-alive

        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        let fieldValue = colon < 0 ? "" : line.slice(colon + 1);
        if (fieldValue.startsWith(" ")) fieldValue = fieldValue.slice(1);

        if (field === "id") id = fieldValue;
        else if (field === "event") event = fieldValue;
        else if (field === "data") data.push(fieldValue);
      }

      if (done) {
        // Per the SSE spec, an event not terminated by a blank line is discarded.
        return;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export function parseSseData(data: string): Record<string, any> {
  try {
    const parsed = JSON.parse(data);
    return parsed && typeof parsed === "object" ? parsed : { value: parsed };
  } catch {
    return { raw: data };
  }
}
