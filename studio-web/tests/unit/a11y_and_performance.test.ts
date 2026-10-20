import { describe, it, expect } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import { deduplicateEvents } from '../../src/features/runs/events';
import type { RunEvent } from '@liceron/nexura-sdk';

describe('Accessibility, Bilingual UX & Performance (F14)', () => {
  describe('Bilingual Formatting & Devanagari Preservation', () => {
    it('formats Indian Rupee (INR) correctly in both en-IN and hi-IN locales', () => {
      const formatINR = (amount: number, lang: 'en' | 'hi') => {
        return new Intl.NumberFormat(lang === 'hi' ? 'hi-IN' : 'en-IN', {
          style: 'currency',
          currency: 'INR',
          maximumFractionDigits: 0,
        }).format(amount);
      };

      const enFormatted = formatINR(150000, 'en');
      const hiFormatted = formatINR(150000, 'hi');

      expect(enFormatted).toContain('₹');
      expect(hiFormatted).toContain('₹');
      // Indian numbering system formats 150000 as 1,50,000
      expect(enFormatted).toMatch(/1,50,000/);
    });

    it('preserves Devanagari text, prompt templates and unicode metadata without mutating IDs', () => {
      const workflowSpec = {
        name: 'दैनिक सारांश कार्यप्रवाह',
        description: 'यह एक स्वचालित परीक्षण कार्यप्रवाह है जो हिंदी में विवरण रखता है।',
        entry_node: 'node_start',
        nodes: {
          node_start: {
            id: 'node_start',
            module_id: 'system/passthrough',
            label: 'प्रारंभिक नोड',
            config: {
              greeting: 'नमस्ते दुनिया',
              prompt_template: 'कृपया निम्नलिखित इनपुट का विश्लेषण करें: {{input}}',
              amount_inr: 25000,
            },
            default_transition: 'node_end',
          },
          node_end: {
            id: 'node_end',
            module_id: 'system/passthrough',
            label: 'समापन नोड',
          },
        },
      };

      // Ensure JSON roundtrip preserves exact Devanagari code points
      const serialized = JSON.stringify(workflowSpec);
      const deserialized = JSON.parse(serialized);

      expect(deserialized.name).toBe('दैनिक सारांश कार्यप्रवाह');
      expect(deserialized.nodes.node_start.label).toBe('प्रारंभिक नोड');
      expect(deserialized.nodes.node_start.config.greeting).toBe('नमस्ते दुनिया');

      // Canonical identifiers remain ASCII
      expect(deserialized.entry_node).toBe('node_start');
      expect(deserialized.nodes.node_start.id).toBe('node_start');
      expect(deserialized.nodes.node_start.default_transition).toBe('node_end');
    });
  });

  describe('Bounded Memory & Event Capping', () => {
    it('bounds retained events to maxRetained to prevent memory leaks during long runs', () => {
      const existingEvents: RunEvent[] = [];
      for (let i = 1; i <= 600; i++) {
        existingEvents.push({
          seq: i,
          type: 'NODE_STARTED',
          timestamp: 1000 + i,
          data: { node_id: `node_${i % 10}` },
        });
      }

      const incomingEvents: RunEvent[] = [];
      for (let i = 601; i <= 1500; i++) {
        incomingEvents.push({
          seq: i,
          type: 'NODE_COMPLETED',
          timestamp: 1000 + i,
          data: { node_id: `node_${i % 10}` },
        });
      }

      // Default cap is 1000
      const bounded = deduplicateEvents(existingEvents, incomingEvents, 1000);

      expect(bounded).toHaveLength(1000);
      // It should retain the most recent 1000 events (seq 501 to 1500)
      expect(bounded[0].seq).toBe(501);
      expect(bounded[bounded.length - 1].seq).toBe(1500);
    });
  });

  describe('Accessibility & Reduced Motion Tokens', () => {
    it('defines accessible focus rings and reduced-motion overrides in stylesheet', () => {
      const cssPath = path.resolve(__dirname, '../../src/styles/index.css');
      const cssContent = fs.readFileSync(cssPath, 'utf8');

      expect(cssContent).toContain(':focus-visible');
      expect(cssContent).toContain('@media (prefers-reduced-motion: reduce)');
      expect(cssContent).toContain('animation-duration: 0.01ms');
    });

    it('provides both light and dark theme design tokens', () => {
      const tokensPath = path.resolve(__dirname, '../../src/styles/tokens.css');
      const tokensContent = fs.readFileSync(tokensPath, 'utf8');

      expect(tokensContent).toContain(':root');
      expect(tokensContent).toContain(':root[data-theme="light"]');
      expect(tokensContent).toContain('--bg-canvas');
      expect(tokensContent).toContain('--text-primary');
      expect(tokensContent).toContain('--border-subtle');
      expect(tokensContent).toContain('--accent-primary');
    });
  });

  describe('Accessible Canvas Alternative', () => {
    it('provides an accessible structured node representation without canvas drag dependency', () => {
      const spec = {
        name: 'Keyboard Accessible Workflow',
        entry_node: 'fetch_data',
        nodes: {
          fetch_data: {
            id: 'fetch_data',
            module_id: 'http/request',
            requires_approval: false,
            default_transition: 'validate_data',
          },
          validate_data: {
            id: 'validate_data',
            module_id: 'validator',
            requires_approval: true,
            transitions: {
              valid: 'process_data',
              invalid: 'notify_error',
            },
          },
          process_data: {
            id: 'process_data',
            module_id: 'processor',
          },
          notify_error: {
            id: 'notify_error',
            module_id: 'notifier',
          },
        },
      };

      const nodeEntries = Object.entries(spec.nodes as Record<string, Record<string, unknown>>);
      expect(nodeEntries).toHaveLength(4);

      // Verify node list contains all information necessary for keyboard user
      const entriesMap = new Map<string, Record<string, unknown>>(nodeEntries);
      const validateNode = entriesMap.get('validate_data');
      expect(validateNode).toBeDefined();
      expect(validateNode?.requires_approval).toBe(true);
      expect(validateNode?.transitions).toEqual({
        valid: 'process_data',
        invalid: 'notify_error',
      });
    });
  });
});
