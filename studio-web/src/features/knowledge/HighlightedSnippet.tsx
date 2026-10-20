// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { buildHighlightSegments, type HighlightUnit } from './highlight';

interface Props {
  text: string;
  highlights?: ReadonlyArray<readonly [number, number]>;
  /** Unit of the spans; the API's `highlights_utf16` is 'utf16'. */
  unit?: HighlightUnit;
  testId?: string;
}

/** Snippet text with matched terms wrapped in `<mark>`; built from text nodes, never HTML. */
export function HighlightedSnippet({ text, highlights, unit, testId }: Props) {
  const segments = buildHighlightSegments(text, highlights, unit);
  return (
    <span data-testid={testId} style={{ whiteSpace: 'pre-wrap' }}>
      {segments.map((seg, i) =>
        seg.mark ? (
          <mark
            key={i}
            data-testid="snippet-highlight"
            style={{ backgroundColor: 'rgba(250, 204, 21, 0.45)', color: 'inherit', padding: '0 1px' }}
          >
            {seg.text}
          </mark>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </span>
  );
}
