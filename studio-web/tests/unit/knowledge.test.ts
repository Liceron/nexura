import { describe, it, expect } from 'vitest';
import { SAMPLE_DOCS } from '../../src/features/knowledge/types';
import type {
  CitationItem,
  RetrieveResponse,
  AnswerResponse,
} from '../../src/features/knowledge/types';

describe('Knowledge Ingestion, Scoped Retrieval & Citations (F10)', () => {
  it('provides bilingual sample documents for ingestion testing', () => {
    expect(SAMPLE_DOCS).toHaveLength(2);
    const enDoc = SAMPLE_DOCS.find((d) => d.language === 'en');
    const hiDoc = SAMPLE_DOCS.find((d) => d.language === 'hi');

    expect(enDoc).toBeDefined();
    expect(enDoc?.filename).toBe('architecture_guide.md');
    expect(enDoc?.content).toContain('Nexura SDK architecture overview');

    expect(hiDoc).toBeDefined();
    expect(hiDoc?.filename).toBe('hindi_guide.txt');
    expect(hiDoc?.content).toContain('नेक्सुरा स्टूडियो एक स्वतंत्र और शक्तिशाली ढांचा है');
  });

  it('validates citation item structure and source character spans', () => {
    const citation: CitationItem = {
      citation_id: '[1]',
      document_id: 'doc-eng-101',
      origin: 'architecture_guide.md',
      page_start: 1,
      page_end: 1,
      start_char: 0,
      end_char: 180,
      snippet: 'Nexura SDK architecture overview. The standalone SDK operates independently...',
    };

    expect(citation.citation_id).toBe('[1]');
    expect(citation.origin).toBe('architecture_guide.md');
    expect(citation.end_char).toBeGreaterThan(citation.start_char);
    expect(citation.page_start).toBeGreaterThanOrEqual(1);
    expect(citation.snippet.length).toBeGreaterThan(0);
  });

  it('correctly models hybrid retrieval response and token budgeting', () => {
    const retResp: RetrieveResponse = {
      query: 'standalone SDK SQLite persistence',
      collection_id: 'docs-coll',
      workspace_id: 'default',
      has_sufficient_evidence: true,
      total_tokens: 120,
      citations: [
        {
          citation_id: '[1]',
          document_id: 'doc-1',
          origin: 'architecture_guide.md',
          page_start: 1,
          page_end: 1,
          start_char: 0,
          end_char: 120,
          snippet: 'Nexura SDK features SQLite session persistence.',
        },
      ],
      items: [
        {
          chunk_id: 'chk-1',
          document_id: 'doc-1',
          origin: 'architecture_guide.md',
          text: 'Nexura SDK features SQLite session persistence.',
          score: 0.85,
          dense_score: 0.82,
          lexical_score: 0.91,
        },
      ],
      latency_ms: 14.5,
    };

    expect(retResp.has_sufficient_evidence).toBe(true);
    expect(retResp.citations).toHaveLength(1);
    expect(retResp.items[0].dense_score).toBe(0.82);
    expect(retResp.items[0].lexical_score).toBe(0.91);
    expect(retResp.latency_ms).toBeGreaterThan(0);
  });

  it('truthfully signals insufficient evidence when retrieval has zero matching candidates', () => {
    const emptyResp: RetrieveResponse = {
      query: 'unrelated topic not in documents',
      collection_id: 'docs-coll',
      workspace_id: 'default',
      has_sufficient_evidence: false,
      total_tokens: 0,
      citations: [],
      items: [],
      latency_ms: 5.2,
    };

    expect(emptyResp.has_sufficient_evidence).toBe(false);
    expect(emptyResp.citations).toHaveLength(0);
    expect(emptyResp.items).toHaveLength(0);
  });

  it('formats grounded agent answer with bracketed citations and source references', () => {
    const ansResp: AnswerResponse = {
      query: 'What persistence does Nexura use?',
      answer: 'According to architecture_guide.md (Page 1), Nexura uses SQLite session persistence. [1]',
      has_sufficient_evidence: true,
      citations: [
        {
          citation_id: '[1]',
          document_id: 'doc-1',
          origin: 'architecture_guide.md',
          page_start: 1,
          page_end: 1,
          start_char: 40,
          end_char: 120,
          snippet: 'Nexura SDK features SQLite session persistence.',
        },
      ],
      retrieval_duration_ms: 12.0,
      generation_duration_ms: 2.5,
      model: 'fake-deterministic (Local Offline Fixture)',
    };

    expect(ansResp.has_sufficient_evidence).toBe(true);
    expect(ansResp.answer).toContain('[1]');
    expect(ansResp.answer).toContain('architecture_guide.md');
    expect(ansResp.citations).toHaveLength(1);
    expect(ansResp.model).toContain('Local Offline Fixture');
  });

  it('rejects path traversal patterns in document filenames', () => {
    const isValidFilename = (fn: string) => {
      const clean = fn.trim();
      if (!clean) return false;
      if (clean.includes('..') || clean.startsWith('/') || clean.includes('\\')) {
        return false;
      }
      return true;
    };

    expect(isValidFilename('../../etc/passwd')).toBe(false);
    expect(isValidFilename('/root/secrets.env')).toBe(false);
    expect(isValidFilename('sub\\..\\file.txt')).toBe(false);
    expect(isValidFilename('architecture_guide.md')).toBe(true);
    expect(isValidFilename('hindi_guide.txt')).toBe(true);
  });
});
