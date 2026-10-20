// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState, useEffect, useCallback, useRef } from 'react';
import { useLatestT } from '../../i18n';
import { useApi } from '../../api/context';
import type {
  KnowledgeDocumentRecord,
  KnowledgeLimits,
  KnowledgeChunk,
  RetrieveResponse,
  AnswerResponse,
} from './types';
import { prepareUpload } from './fileUpload';
import { takeKnowledgeFocus } from './focus';
import { formatApiError } from '../../api/errors';

const DEFAULT_LIMITS: KnowledgeLimits = {
  max_file_bytes: 5_000_000,
  max_workspace_bytes: 50_000_000,
  max_top_k: 50,
  workspace_used_bytes: 0,
};

export function useKnowledge() {
  const t = useLatestT();
  const { manager } = useApi();
  const [collections, setCollections] = useState<string[]>(['default']);
  const [selectedCollection, setSelectedCollection] = useState<string>('default');
  const [documents, setDocuments] = useState<KnowledgeDocumentRecord[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [uploading, setUploading] = useState<boolean>(false);
  const [retrieving, setRetrieving] = useState<boolean>(false);
  const [answering, setAnswering] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [limits, setLimits] = useState<KnowledgeLimits>(DEFAULT_LIMITS);
  const [focusedChunk, setFocusedChunk] = useState<KnowledgeChunk | null>(null);

  const [retrievalResult, setRetrievalResult] = useState<RetrieveResponse | null>(null);
  const [answerResult, setAnswerResult] = useState<AnswerResponse | null>(null);

  const isMountedRef = useRef<boolean>(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const refreshCollections = useCallback(async () => {
    try {
      const resp = await manager.client.knowledge.listCollections();
      if (!isMountedRef.current) return;
      const colls = resp.collections && resp.collections.length > 0 ? resp.collections : ['default'];
      setCollections(colls);
      return colls;
    } catch {
      return ['default'];
    }
  }, [manager]);

  const refreshLimits = useCallback(async () => {
    try {
      const resp = await manager.client.request<KnowledgeLimits>('GET', '/knowledge/limits');
      if (isMountedRef.current) setLimits(resp);
    } catch {
      // keep the defaults shown in the UI; the server enforces the real limits
    }
  }, [manager]);

  const openChunk = useCallback(
    async (documentId: string, chunkIndex: number) => {
      try {
        const chunk = await manager.client.request<KnowledgeChunk>(
          'GET',
          `/knowledge/documents/${encodeURIComponent(documentId)}/chunks/${chunkIndex}`,
        );
        if (isMountedRef.current) setFocusedChunk(chunk);
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('validationMessages.chunkUnavailable').replace('{v1}', String(msg)));
      }
    },
    [manager, t],
  );

  const refreshDocuments = useCallback(async (collectionId: string = selectedCollection) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await manager.client.knowledge.listDocuments({
        collection_id: collectionId,
        limit: 100,
      });
      if (!isMountedRef.current) return;
      setDocuments((resp.items || []) as KnowledgeDocumentRecord[]);
    } catch (err: unknown) {
      if (!isMountedRef.current) return;
      const msg = formatApiError(err, t);
      setError(t('validationMessages.documentList').replace('{v1}', String(msg)));
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
      }
    }
  }, [manager, selectedCollection, t]);

  const createCollection = useCallback(async (collectionId: string, name: string) => {
    setError(null);
    try {
      await manager.client.knowledge.createCollection({
        collection_id: collectionId,
        name,
      });
      await refreshCollections();
      if (!isMountedRef.current) return;
      setSelectedCollection(collectionId);
    } catch (err: unknown) {
      if (!isMountedRef.current) return;
      const msg = formatApiError(err, t);
      setError(t('validationMessages.collectionCreate').replace('{v1}', String(msg)));
    }
  }, [manager, refreshCollections, t]);

  const uploadDocument = useCallback(
    async (
      filename: string,
      content: string,
      mimeType: string = 'text/plain',
      encoding: 'text' | 'base64' = 'text',
    ) => {
      setError(null);
      setUploading(true);
      try {
        const body = {
          collection_id: selectedCollection,
          filename,
          content,
          mime_type: mimeType,
          content_encoding: encoding,
        };
        const res = await manager.client.knowledge.uploadDocument(body);
        await refreshDocuments(selectedCollection);
        await refreshLimits();
        return res;
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('validationMessages.upload').replace('{v1}', String(msg)));
        return null;
      } finally {
        if (isMountedRef.current) {
          setUploading(false);
        }
      }
    },
    [manager, selectedCollection, refreshDocuments, refreshLimits, t]
  );

  const uploadFile = useCallback(
    async (file: File) => {
      const prepared = await prepareUpload(file, limits.max_file_bytes);
      if (!prepared.ok) {
        setError(t('validationMessages.uploadRefused').replace('{v1}', String(file.name)).replace('{v2}', String(prepared.reason)));
        return { refused: prepared.reason } as const;
      }
      const u = prepared.upload;
      const res = await uploadDocument(u.filename, u.content, u.mimeType, u.encoding);
      return { refused: null, result: res } as const;
    },
    [limits.max_file_bytes, uploadDocument, t],
  );

  const deleteDocument = useCallback(
    async (documentId: string) => {
      setError(null);
      try {
        await manager.client.knowledge.deleteDocument(documentId);
        // Clear previous results referencing this document if needed
        setRetrievalResult(null);
        setAnswerResult(null);
        setFocusedChunk((c) => (c && c.document_id === documentId ? null : c));
        await refreshDocuments(selectedCollection);
        await refreshLimits();
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('validationMessages.documentDelete').replace('{v1}', String(msg)));
      }
    },
    [manager, selectedCollection, refreshDocuments, refreshLimits, t]
  );

  const runRetrieval = useCallback(
    async (query: string, topK: number = 5) => {
      if (!query.trim()) return;
      setError(null);
      setRetrieving(true);
      setRetrievalResult(null);
      try {
        const res = await manager.client.knowledge.retrieve({
          query,
          collection_id: selectedCollection,
          top_k: topK,
        });
        if (!isMountedRef.current) return;
        setRetrievalResult(res);
        return res;
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('validationMessages.retrieval').replace('{v1}', String(msg)));
        return null;
      } finally {
        if (isMountedRef.current) {
          setRetrieving(false);
        }
      }
    },
    [manager, selectedCollection, t]
  );

  const runAnswer = useCallback(
    async (query: string, language: 'en' | 'hi' = 'en') => {
      if (!query.trim()) return;
      setError(null);
      setAnswering(true);
      setAnswerResult(null);
      try {
        const res = await manager.client.knowledge.answer({
          query,
          collection_id: selectedCollection,
          language,
        });
        if (!isMountedRef.current) return;
        setAnswerResult(res);
        return res;
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('validationMessages.knowledgeAnswer').replace('{v1}', String(msg)));
        return null;
      } finally {
        if (isMountedRef.current) {
          setAnswering(false);
        }
      }
    },
    [manager, selectedCollection, t]
  );

  useEffect(() => {
    void refreshLimits();
    const focus = takeKnowledgeFocus();
    if (focus) void openChunk(focus.documentId, focus.chunkIndex);
  }, [refreshLimits, openChunk]);

  useEffect(() => {
    refreshCollections().then(() => {
      refreshDocuments(selectedCollection);
    });
  }, [refreshCollections, refreshDocuments, selectedCollection]);

  return {
    collections,
    selectedCollection,
    setSelectedCollection,
    documents,
    loading,
    uploading,
    retrieving,
    answering,
    error,
    limits,
    focusedChunk,
    openChunk,
    closeChunk: () => setFocusedChunk(null),
    uploadFile,
    retrievalResult,
    answerResult,
    createCollection,
    uploadDocument,
    deleteDocument,
    runRetrieval,
    runAnswer,
    refreshDocuments,
    clearError: () => setError(null),
  };
}
