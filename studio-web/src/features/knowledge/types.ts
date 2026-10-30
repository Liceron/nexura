// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type {
  KnowledgeCollection,
  KnowledgeDocument,
  IngestDocumentOptions,
  CitationItem,
  RetrievedChunkItem,
  RetrieveResponse,
  AnswerResponse,
} from '@liceron/nexura-sdk';

export type {
  KnowledgeCollection,
  KnowledgeDocument,
  IngestDocumentOptions,
  CitationItem,
  RetrievedChunkItem,
  RetrieveResponse,
  AnswerResponse,
};

export interface SampleDoc {
  id: string;
  name: string;
  filename: string;
  content: string;
  language: 'en' | 'hi';
}

export const SAMPLE_DOCS: SampleDoc[] = [
  {
    id: 'sample-en-arch',
    name: 'English Architecture Overview',
    filename: 'architecture_guide.md',
    content:
      'Nexura SDK architecture overview. The standalone SDK operates independently without any external framework dependencies. It features deterministic local models, SQLite session persistence, and hybrid RAG retrieval with citation tracking.',
    language: 'en',
  },
  {
    id: 'sample-hi-guide',
    name: 'Hindi Knowledge Guide',
    filename: 'hindi_guide.txt',
    content:
      'नेक्सुरा स्टूडियो एक स्वतंत्र और शक्तिशाली ढांचा है। यह स्थानीय निष्पादन, डेटा सुरक्षा और हिंदी और अंग्रेजी में बहुभाषी एजेंट का समर्थन करता है। सभी दस्तावेज़ सुरक्षित और अनुक्रमित हैं।',
    language: 'hi',
  },
];

/** Document record as returned by the hybrid knowledge routes (extends the SDK type). */
export type KnowledgeDocumentRecord = KnowledgeDocument & {
  status?: string;
  byte_size?: number;
  chunk_count?: number;
  source_uri?: string | null;
  title?: string;
};

/** Citation with chunk index, source URI and highlight spans (extends the SDK type). */
export type HybridCitationItem = CitationItem & {
  title?: string;
  chunk_index?: number;
  source_uri?: string | null;
  highlights?: [number, number][];
  /** The same spans in UTF-16 code units (preferred by the UI). */
  highlights_utf16?: [number, number][];
  matched_by?: string[];
};

/** Upload limits and the workspace's current usage from `GET /knowledge/limits`. */
export interface KnowledgeLimits {
  max_file_bytes: number;
  max_workspace_bytes: number;
  max_top_k: number;
  workspace_used_bytes: number;
  is_test_fixture?: boolean;
}

/** One chunk returned by `GET /knowledge/documents/{id}/chunks/{index}`. */
export interface KnowledgeChunk {
  chunk_id: string;
  document_id: string;
  chunk_index: number;
  text: string;
  start_char: number;
  end_char: number;
  title: string;
  source_uri: string | null;
}
