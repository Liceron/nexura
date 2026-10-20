import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const sourceRoot = path.resolve(__dirname, '../../src');

function filesIn(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? filesIn(file) : [file];
  });
}

function missingTokens(source: string, definitions: Set<string>): string[] {
  return [...source.matchAll(/var\(\s*(--[\w-]+)\s*([,)])/g)]
    .filter((match) => match[2] !== ',' && !definitions.has(match[1]))
    .map((match) => match[1]);
}

describe('Studio theme tokens', () => {
  it('defines every CSS variable used without a fallback anywhere in src/', () => {
    const styles = filesIn(path.join(sourceRoot, 'styles'))
      .filter((file) => file.endsWith('.css')).map((file) => readFileSync(file, 'utf8')).join('\n');
    const definitions = new Set([...styles.matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]));
    const missing = filesIn(sourceRoot).flatMap((file) =>
      missingTokens(readFileSync(file, 'utf8'), definitions).map((name) => `${path.relative(sourceRoot, file)}: ${name}`)
    );
    expect(missing).toEqual([]);
  });

  it('allows fallbacks but still checks nested variable references', () => {
    expect(missingTokens('var(--optional, red) var(--known) var(--unknown)', new Set(['--known'])))
      .toEqual(['--unknown']);
    expect(missingTokens('var(--optional, var(--nested))', new Set())).toEqual(['--nested']);
  });

  it.each(['dark', 'light'])('keeps reviewed text and primary/danger buttons at AA contrast in %s', (theme) => {
    const css = readFileSync(path.join(sourceRoot, 'styles/tokens.css'), 'utf8');
    const block = css.split(':root[data-theme="light"]')[theme === 'dark' ? 0 : 1];
    const values = Object.fromEntries([...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2]]));
    const resolve = (name: string): string => {
      const value = values[name];
      return value.startsWith('var(') ? resolve(value.slice(4, -1)) : value;
    };
    const luminance = (hex: string) => {
      const rgb = hex.slice(1).match(/../g)!.map((part) => parseInt(part, 16) / 255)
        .map((c) => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
      return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
    };
    const contrast = (a: string, b: string) => {
      const [low, high] = [luminance(a), luminance(b)].sort((x, y) => x - y);
      return (high + 0.05) / (low + 0.05);
    };
    for (const surface of ['--bg-canvas', '--bg-surface', '--bg-surface-elevated']) {
      expect(contrast(resolve('--text-muted'), resolve(surface))).toBeGreaterThanOrEqual(4.5);
      if (theme === 'light') expect(contrast(resolve('--status-warning'), resolve(surface))).toBeGreaterThanOrEqual(4.5);
    }
    expect(contrast('#ffffff', resolve('--button-primary'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast('#ffffff', resolve('--button-danger'))).toBeGreaterThanOrEqual(4.5);
  });

  it('preserves mobile pane space and uses the sidebar breakpoint for all split rules', () => {
    const style = document.createElement('style');
    style.textContent = readFileSync(path.join(sourceRoot, 'styles/index.css'), 'utf8').replace(/^@import.*$/gm, '');
    document.head.append(style);
    try {
      const mediaRules = Array.from(style.sheet!.cssRules).filter((rule) => rule.type === 4) as CSSMediaRule[];
      const rules = mediaRules.flatMap((media) => Array.from(media.cssRules)
        .filter((rule) => rule.type === 1).map((rule) => ({ media, rule: rule as CSSStyleRule })));
      const find = (selector: string) => rules.find(({ rule }) => rule.selectorText === selector)!;
      for (const selector of ['.studio-split', '.studio-split > *', '.studio-split-main', '.studio-split-secondary', '.studio-grid']) {
        expect(find(selector).media.media.mediaText).toBe(find('.studio-sidebar').media.media.mediaText);
        expect(find(selector).media.media.mediaText).toBe('(max-width: 640px)');
      }
      expect(find('.studio-split > *').rule.style.getPropertyValue('flex')).toBe('0 0 auto');
      expect(find('.studio-split > *').rule.style.getPropertyPriority('flex')).toBe('important');
      expect(find('.studio-split-main').rule.style.getPropertyValue('min-height')).toBe('60vh');
      const secondary = find('.studio-split-secondary').rule.style;
      expect(secondary.getPropertyValue('height')).toBe('auto');
      expect(secondary.getPropertyPriority('height')).toBe('important');
      expect(secondary.getPropertyValue('max-height')).toBe('40vh');
      expect(secondary.getPropertyValue('overflow')).toBe('auto');
    } finally {
      style.remove();
    }
  });
});
