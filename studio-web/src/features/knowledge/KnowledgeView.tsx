// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { InlineConfirm } from '../../components/InlineConfirm';
import { Icon } from '../../components/Icon';
import { useRef, useState } from 'react';
import type { DragEvent } from 'react';
import { useKnowledge } from './useKnowledge';
import { SAMPLE_DOCS } from './types';
import type { HybridCitationItem } from './types';
import { HighlightedSnippet } from './HighlightedSnippet';
import { safeHref } from './highlight';
import { formatBytes, ALLOWED_EXTENSIONS } from './fileUpload';
import { useI18n } from '../../i18n';
import { Button } from '../../components/Button';
import { Spinner } from '../../components/Spinner';

export function KnowledgeView() {
  const { t } = useI18n();
  const {
    limits,
    focusedChunk,
    closeChunk,
    uploadFile,
    collections,
    selectedCollection,
    setSelectedCollection,
    documents,
    loading,
    uploading,
    retrieving,
    answering,
    error,
    retrievalResult,
    answerResult,
    createCollection,
    uploadDocument,
    deleteDocument,
    runRetrieval,
    runAnswer,
    clearError,
    openChunk,
  } = useKnowledge();

  const [activeTab, setActiveTab] = useState<'retrieval' | 'agent'>('retrieval');
  const [uploadFilename, setUploadFilename] = useState<string>('architecture_guide.md');
  const [uploadContent, setUploadContent] = useState<string>(SAMPLE_DOCS[0].content);
  const [newCollectionName, setNewCollectionName] = useState<string>('');
  const [showNewCollection, setShowNewCollection] = useState<boolean>(false);

  const [searchQuery, setSearchQuery] = useState<string>(t('knowledge.sampleSearch'));
  const [agentQuestion, setAgentQuestion] = useState<string>(t('knowledge.sampleQuestion'));
  const [agentLang, setAgentLang] = useState<'en' | 'hi'>('en');

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState<boolean>(false);

  const handleFiles = async (files: FileList | File[] | null) => {
    if (!files) return;
    for (const file of Array.from(files)) {
      await uploadFile(file);
    }
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    void handleFiles(e.dataTransfer.files);
  };

  const handleUpload = async () => {
    if (!uploadFilename.trim() || !uploadContent.trim()) return;
    const res = await uploadDocument(uploadFilename, uploadContent);
    if (res) {
      setUploadFilename('');
      setUploadContent('');
    }
  };

  const handleCreateCollection = async () => {
    if (!newCollectionName.trim()) return;
    const id = newCollectionName.toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    await createCollection(id, newCollectionName);
    setNewCollectionName('');
    setShowNewCollection(false);
  };

  const handleSelectSample = (sampleId: string) => {
    const s = SAMPLE_DOCS.find((item) => item.id === sampleId);
    if (s) {
      setUploadFilename(s.filename);
      setUploadContent(s.content);
    }
  };

  return (
    <div
      data-testid="knowledge-view"
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: 'calc(100vh - 120px)',
        gap: 16,
      }}
    >
      {/* Top Bar: Collections & Tabs */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: 12,
          backgroundColor: 'var(--bg-surface)',
          padding: '12px 18px',
          borderRadius: 'var(--radius-lg)',
          border: '1px solid var(--border-subtle)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('ui.collection')}
          </span>
          <select
            value={selectedCollection}
            onChange={(e) => setSelectedCollection(e.target.value)}
            data-testid="collection-select"
            style={{
              padding: '5px 10px',
              borderRadius: 'var(--radius-md)',
              border: '1px solid var(--border-subtle)',
              backgroundColor: 'var(--bg-canvas)',
              color: 'var(--text-primary)',
              fontSize: 13,
            }}
          >
            {collections.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>

          <Button
            size="sm"
            variant="secondary"
            onClick={() => setShowNewCollection(!showNewCollection)}
            data-testid="new-collection-btn"
          >
            {t('ui.newCollection')}
          </Button>

          {showNewCollection && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <input
                type="text"
                placeholder={t('ui.collectionName')}
                value={newCollectionName}
                onChange={(e) => setNewCollectionName(e.target.value)}
                style={{
                  padding: '4px 8px',
                  borderRadius: 'var(--radius-sm)',
                  border: '1px solid var(--border-subtle)',
                  fontSize: 12,
                }}
              />
              <Button size="sm" variant="primary" onClick={handleCreateCollection}>
                {t('ui.create')}
              </Button>
            </div>
          )}
        </div>

        {/* View Switcher: Retrieval Preview vs Grounded Knowledge Agent */}
        <div role="tablist"
          style={{
            display: 'inline-flex',
            borderRadius: 'var(--radius-md)',
            border: '1px solid var(--border-subtle)',
            overflow: 'hidden',
          }}
        >
          <button
            role="tab"
            aria-selected={activeTab === 'retrieval'}
            onClick={() => setActiveTab('retrieval')}
            data-testid="tab-retrieval"
            style={{
              padding: '6px 14px',
              fontSize: 12,
              fontWeight: activeTab === 'retrieval' ? 600 : 500,
              backgroundColor:
                activeTab === 'retrieval' ? 'var(--accent-subtle)' : 'transparent',
              color:
                activeTab === 'retrieval'
                  ? 'var(--accent-primary)'
                  : 'var(--text-secondary)',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            <Icon name="search" /> {t('ui.retrievalCitationsPreview')}
          </button>
          <button
            role="tab"
            aria-selected={activeTab === 'agent'}
            onClick={() => setActiveTab('agent')}
            data-testid="tab-agent"
            style={{
              padding: '6px 14px',
              fontSize: 12,
              fontWeight: activeTab === 'agent' ? 600 : 500,
              backgroundColor:
                activeTab === 'agent' ? 'var(--accent-subtle)' : 'transparent',
              color:
                activeTab === 'agent' ? 'var(--accent-primary)' : 'var(--text-secondary)',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            <Icon name="agents" /> {t('ui.groundedKnowledgeAgent')}
          </button>
        </div>
      </div>

      {/* Error Alert */}
      {error && (
        <div
          data-testid="knowledge-error-banner"
          style={{
            padding: '8px 16px',
            backgroundColor: 'rgba(239, 68, 68, 0.1)',
            borderRadius: 'var(--radius-md)',
            border: '1px solid rgba(239, 68, 68, 0.3)',
            color: 'var(--status-danger)',
            fontSize: 12,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <span><Icon name="warning" /> {error}</span>
          <button
            onClick={clearError}
            aria-label={t('ui.dismissError')}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--status-danger)',
              cursor: 'pointer',
              fontSize: 14,
            }}
          >
            <Icon name="close" />
          </button>
        </div>
      )}

      {focusedChunk && (
        <div
          data-testid="source-chunk-panel"
          role="region"
          aria-label={t('knowledge.sourceChunk', 'Source chunk')}
          style={{
            padding: '10px 16px',
            borderRadius: 'var(--radius-md)',
            border: '1px solid var(--accent-primary)',
            backgroundColor: 'var(--bg-surface)',
            fontSize: 12,
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
            <strong data-testid="source-chunk-title">
              {focusedChunk.title} · {t('knowledge.chunk', 'Chunk')} {focusedChunk.chunk_index} ·{' '}
              {focusedChunk.start_char}..{focusedChunk.end_char}
            </strong>
            <button type="button" onClick={closeChunk} data-testid="source-chunk-close" style={{ background: 'none', border: 'none', cursor: 'pointer' }}>
              {t('common.close', 'Close')}
            </button>
          </div>
          <div data-testid="source-chunk-text" style={{ whiteSpace: 'pre-wrap' }}>
            {focusedChunk.text}
          </div>
        </div>
      )}

      {/* Main Split Layout: Left = Documents & Ingestion; Right = Retrieval / Agent */}
      <div className="studio-split" style={{ display: 'flex', flex: 1, gap: 16, overflow: 'hidden' }}>
        {/* Left Panel: Document Management & Upload Form */}
        <div
          className="studio-split-secondary"
          style={{
            width: 380,
            display: 'flex',
            flexDirection: 'column',
            gap: 14,
            overflowY: 'auto',
          }}
        >
          {/* Upload Card */}
          <div
            style={{
              backgroundColor: 'var(--bg-surface)',
              padding: 16,
              borderRadius: 'var(--radius-lg)',
              border: '1px solid var(--border-subtle)',
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
                {t('ui.ingestDocument')}
              </span>
              <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                {t('knowledge.maxShort', 'Max')} {formatBytes(limits.max_file_bytes)}
              </span>
            </div>

            {/* Drag-and-drop / file picker */}
            <div
              data-testid="knowledge-dropzone"
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              style={{
                border: `2px dashed ${dragOver ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                borderRadius: 'var(--radius-md)',
                padding: 12,
                textAlign: 'center',
                fontSize: 12,
                color: 'var(--text-secondary)',
                backgroundColor: dragOver ? 'var(--accent-subtle)' : 'transparent',
              }}
            >
              <div>{t('knowledge.dropHint', 'Drag and drop files here, or')}</div>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                data-testid="knowledge-choose-files"
              >
                {t('knowledge.chooseFiles', 'Choose files')}
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept={ALLOWED_EXTENSIONS.join(',')}
                data-testid="knowledge-file-input"
                aria-label={t('knowledge.chooseFiles', 'Choose files')}
                style={{ display: 'none' }}
                onChange={(e) => {
                  void handleFiles(e.target.files);
                  e.target.value = '';
                }}
              />
              <div data-testid="knowledge-size-limit" style={{ marginTop: 6, fontSize: 11, color: 'var(--text-tertiary)' }}>
                {t('knowledge.typesHint', 'Text, Markdown, HTML, PDF')} ·{' '}
                {t('knowledge.maxPerFile', 'Max per file')}: {formatBytes(limits.max_file_bytes)} ·{' '}
                {t('knowledge.workspaceUsage', 'Workspace')}: {formatBytes(limits.workspace_used_bytes)} /{' '}
                {formatBytes(limits.max_workspace_bytes)}
              </div>
            </div>

            {/* Quick Samples */}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{t('ui.samples')}</span>
              <button
                type="button"
                onClick={() => handleSelectSample('sample-en-arch')}
                data-testid="sample-en-btn"
                style={{
                  fontSize: 10,
                  padding: '2px 6px',
                  borderRadius: 4,
                  border: '1px solid var(--border-subtle)',
                  background: 'var(--bg-canvas)',
                  cursor: 'pointer',
                }}
              >
                {t('ui.englishSdk')}
              </button>
              <button
                type="button"
                onClick={() => handleSelectSample('sample-hi-guide')}
                data-testid="sample-hi-btn"
                style={{
                  fontSize: 10,
                  padding: '2px 6px',
                  borderRadius: 4,
                  border: '1px solid var(--border-subtle)',
                  background: 'var(--bg-canvas)',
                  cursor: 'pointer',
                }}
              >
                {t('ui.guide')}
              </button>
            </div>

            <div>
              <label style={{ fontSize: 11, fontWeight: 500, color: 'var(--text-secondary)' }}>
                {t('ui.filename')}
              </label>
              <input
                type="text"
                value={uploadFilename}
                onChange={(e) => setUploadFilename(e.target.value)}
                placeholder={t('ui.documentMdOrGuideTxt')}
                data-testid="upload-filename-input"
                style={{
                  width: '100%',
                  marginTop: 4,
                  padding: '6px 10px',
                  borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border-subtle)',
                  backgroundColor: 'var(--bg-canvas)',
                  fontSize: 12,
                  color: 'var(--text-primary)',
                  boxSizing: 'border-box',
                }}
              />
            </div>

            <div>
              <label style={{ fontSize: 11, fontWeight: 500, color: 'var(--text-secondary)' }}>
                {t('ui.multilingualContent')}
              </label>
              <textarea
                value={uploadContent}
                onChange={(e) => setUploadContent(e.target.value)}
                placeholder={t('ui.pasteDocumentTextInEnglishOrHindi')}
                rows={4}
                data-testid="upload-content-textarea"
                style={{
                  width: '100%',
                  marginTop: 4,
                  padding: '6px 10px',
                  borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border-subtle)',
                  backgroundColor: 'var(--bg-canvas)',
                  fontSize: 12,
                  color: 'var(--text-primary)',
                  resize: 'vertical',
                  boxSizing: 'border-box',
                  lineHeight: 1.4,
                }}
              />
            </div>

            <Button
              variant="primary"
              onClick={handleUpload}
              disabled={!uploadFilename.trim() || !uploadContent.trim() || uploading}
              data-testid="upload-document-button"
            >
              {uploading ? <Spinner size={14} /> : <><Icon name="workflows" /> {t('ui.ingestIndexDocument')}</>}
            </Button>
          </div>

          {/* Documents List */}
          <div
            style={{
              backgroundColor: 'var(--bg-surface)',
              padding: 16,
              borderRadius: 'var(--radius-lg)',
              border: '1px solid var(--border-subtle)',
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
                {t('ui.documents')}{documents.length})
              </span>
            </div>

            {loading ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: 24 }}>
                <Spinner size={18} />
              </div>
            ) : documents.length === 0 ? (
              <div
                style={{
                  textAlign: 'center',
                  padding: '24px 12px',
                  color: 'var(--text-tertiary)',
                  fontSize: 12,
                }}
              >
                {t('ui.noDocumentsIndexedInThisCollectionYet')}
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {documents.map((doc) => (
                  <div
                    key={doc.document_id}
                    data-testid={`document-item-${doc.document_id}`}
                    style={{
                      padding: 10,
                      borderRadius: 'var(--radius-md)',
                      border: '1px solid var(--border-subtle)',
                      backgroundColor: 'var(--bg-canvas)',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 4,
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <span
                        data-testid="doc-filename"
                        style={{
                          fontWeight: 600,
                          fontSize: 12,
                          color: 'var(--text-primary)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          maxWidth: 240,
                        }}
                      >
                        <Icon name="file" /> {doc.filename}
                      </span>
                      <InlineConfirm key={doc.document_id} consequence={t('confirmation.deleteDocument')} >
                        <button
                          onClick={() => deleteDocument(doc.document_id)}
                          data-testid={`delete-doc-${doc.document_id}`}
                          title={t('ui.deleteDocument')}
                          style={{
                            background: 'none',
                            border: 'none',
                            color: 'var(--status-danger)',
                            cursor: 'pointer',
                            fontSize: 12,
                            padding: '2px 4px',
                          }}
                        >
                          <Icon name="close" /> {t('ui.delete')}
                        </button>
                      </InlineConfirm>
                    </div>

                    <div style={{ display: 'flex', gap: 10, fontSize: 11, color: 'var(--text-secondary)' }}>
                      <span
                        data-testid="doc-status"
                        style={{
                          padding: '0 6px',
                          borderRadius: 8,
                          backgroundColor: 'rgba(34, 197, 94, 0.15)',
                          color: '#15803d',
                        }}
                      >
                        {doc.status === 'indexed' || !doc.status
                          ? t('knowledge.statusIndexed', 'Indexed')
                          : doc.status}
                      </span>
                      <span>•</span>
                      <span>{doc.text_length} {t('ui.chars')}</span>
                      {typeof doc.chunk_count === 'number' && (
                        <>
                          <span>•</span>
                          <span>{doc.chunk_count} {t('knowledge.chunks', 'chunks')}</span>
                        </>
                      )}
                      <span>•</span>
                      <span>{t('ui.hash') + " "}{doc.content_hash.substring(0, 8)}...</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Right Panel: Retrieval Preview or Grounded Knowledge Agent */}
        <div
          className="studio-split-main"
          style={{
            flex: 1,
            backgroundColor: 'var(--bg-surface)',
            borderRadius: 'var(--radius-lg)',
            border: '1px solid var(--border-subtle)',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {activeTab === 'retrieval' ? (
            /* Tab 1: Retrieval Preview */
            <div style={{ display: 'flex', flexDirection: 'column', height: '100%', padding: 20 }}>
              <div style={{ marginBottom: 16 }}>
                <h3 style={{ fontSize: 15, fontWeight: 600, margin: '0 0 6px 0', color: 'var(--text-primary)' }}>
                  {t('ui.hybridRetrievalPreview')}
                </h3>
                <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: 0 }}>
                  {t('ui.searchIndexedChunksUsingFusedDenseVectorSimilarityLexicalBm25KeywordMatchingWi')}
                </p>
              </div>

              {/* Search Form */}
              <div style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder={t('ui.enterSearchQueryEnglishOrHindi')}
                  data-testid="retrieval-query-input"
                  style={{
                    flex: 1,
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-canvas)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                />
                <Button
                  variant="primary"
                  onClick={() => runRetrieval(searchQuery)}
                  disabled={!searchQuery.trim() || retrieving}
                  data-testid="search-retrieval-button"
                >
                  {retrieving ? <Spinner size={14} /> : <>{t('ui.search') + " "}<Icon name="search" /></>}
                </Button>
              </div>

              {/* Retrieval Results */}
              <div style={{ flex: 1, overflowY: 'auto' }}>
                {!retrievalResult ? (
                  <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-tertiary)', fontSize: 13 }}>
                    {t('ui.executeASearchQueryAboveToViewRetrievedChunksAndVerifiableCitations')}
                  </div>
                ) : !retrievalResult.has_sufficient_evidence || retrievalResult.citations.length === 0 ? (
                  <div
                    data-testid="insufficient-evidence-alert"
                    style={{
                      padding: 16,
                      borderRadius: 'var(--radius-md)',
                      backgroundColor: 'rgba(234, 179, 8, 0.1)',
                      border: '1px solid rgba(234, 179, 8, 0.3)',
                      color: '#b45309',
                      fontSize: 13,
                    }}
                  >
                    <Icon name="warning" /> {t('ui.insufficientEvidenceNoMatchingChunksFoundAboveThresholdInThisCollection')}
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-secondary)' }}>
                      <span>{t('ui.found') + " "}<strong>{retrievalResult.citations.length}</strong> {t('ui.citations')}</span>
                      <span>{t('ui.latency') + " "}<strong>{retrievalResult.latency_ms}ms</strong></span>
                    </div>

                    {(retrievalResult.citations as HybridCitationItem[]).map((cit, idx) => (
                      <div
                        key={cit.citation_id || idx}
                        data-testid={`citation-card-${idx}`}
                        style={{
                          padding: 14,
                          borderRadius: 'var(--radius-md)',
                          border: '1px solid var(--border-subtle)',
                          backgroundColor: 'var(--bg-canvas)',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 6,
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                          <span
                            data-testid="citation-origin"
                            style={{ fontWeight: 600, fontSize: 12, color: 'var(--accent-primary)' }}
                          >
                            {cit.citation_id} {t('ui.source') + " "}{cit.origin}
                          </span>
                          {typeof cit.chunk_index === 'number' && (
                            <span data-testid="citation-chunk-index" style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                              {t('knowledge.chunk', 'Chunk')} {cit.chunk_index}
                            </span>
                          )}
                          <span style={{ fontSize: 11, color: 'var(--text-tertiary)', fontFamily: 'monospace' }}>
                            {t('ui.spanChars') + " "}{cit.start_char}..{cit.end_char}{cit.page_start != null ? t('ui.pageSuffix').replace('{page}', String(cit.page_start)) : ''}
                          </span>
                        </div>

                        <div
                          data-testid="citation-snippet"
                          style={{
                            fontSize: 12,
                            lineHeight: 1.5,
                            color: 'var(--text-primary)',
                            whiteSpace: 'pre-wrap',
                            backgroundColor: 'var(--bg-surface)',
                            padding: 8,
                            borderRadius: 'var(--radius-sm)',
                            border: '1px solid var(--border-subtle)',
                          }}
                        >
                          <HighlightedSnippet
                            text={cit.snippet}
                            highlights={cit.highlights_utf16 ?? cit.highlights}
                            unit={cit.highlights_utf16 ? 'utf16' : 'codepoint'}
                          />
                        </div>
                        <div style={{ display: 'flex', gap: 10, fontSize: 11, color: 'var(--text-tertiary)', alignItems: 'center' }}>
                          {cit.source_uri &&
                            (safeHref(cit.source_uri) ? (
                              <a
                                data-testid="citation-source-uri"
                                href={safeHref(cit.source_uri) as string}
                                target="_blank"
                                rel="noopener noreferrer"
                              >
                                {cit.source_uri}
                              </a>
                            ) : (
                              <span data-testid="citation-source-uri">{cit.source_uri}</span>
                            ))}
                          {typeof cit.chunk_index === 'number' && (
                            <button
                              type="button"
                              data-testid={`open-chunk-${idx}`}
                              onClick={() => void openChunk(cit.document_id, cit.chunk_index as number)}
                              style={{ background: 'none', border: 'none', color: 'var(--accent-primary)', cursor: 'pointer', fontSize: 11 }}
                            >
                              {t('citations.openChunk', 'Open source chunk')}
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : (
            /* Tab 2: Grounded Knowledge Agent */
            <div style={{ display: 'flex', flexDirection: 'column', height: '100%', padding: 20 }}>
              <div style={{ marginBottom: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                  <span
                    data-testid="knowledge-agent-badge"
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 6,
                      padding: '4px 10px',
                      borderRadius: 'var(--radius-sm)',
                      backgroundColor: 'rgba(59, 130, 246, 0.1)',
                      border: '1px solid rgba(59, 130, 246, 0.3)',
                      color: '#2563eb',
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    <span><Icon name="agents" /></span>
                    <span>{t('ui.groundedKnowledgeAgentLocalOfflineFixture')}</span>
                  </span>
                </div>
                <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: 0 }}>
                  {t('ui.answersQuestionsStrictlyGroundedInVerifiableSourceCitationsIfNoEvidenceExistsE')}
                </p>
              </div>

              {/* Agent Form */}
              <div style={{ display: 'flex', gap: 10, marginBottom: 16, alignItems: 'center' }}>
                <input
                  type="text"
                  value={agentQuestion}
                  onChange={(e) => setAgentQuestion(e.target.value)}
                  placeholder={t('ui.askAQuestionGroundedInKnowledgeBase')}
                  data-testid="agent-question-input"
                  style={{
                    flex: 1,
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-canvas)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                />

                <div
                  style={{
                    display: 'inline-flex',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--border-subtle)',
                    overflow: 'hidden',
                  }}
                >
                  <button
                    onClick={() => setAgentLang('en')}
                    data-testid="agent-lang-en"
                    style={{
                      padding: '6px 10px',
                      fontSize: 12,
                      fontWeight: agentLang === 'en' ? 600 : 400,
                      backgroundColor:
                        agentLang === 'en' ? 'var(--accent-subtle)' : 'transparent',
                      color:
                        agentLang === 'en' ? 'var(--accent-primary)' : 'var(--text-secondary)',
                      border: 'none',
                      cursor: 'pointer',
                    }}
                  >
                    EN
                  </button>
                  <button
                    onClick={() => setAgentLang('hi')}
                    data-testid="agent-lang-hi"
                    style={{
                      padding: '6px 10px',
                      fontSize: 12,
                      fontWeight: agentLang === 'hi' ? 600 : 400,
                      backgroundColor:
                        agentLang === 'hi' ? 'var(--accent-subtle)' : 'transparent',
                      color:
                        agentLang === 'hi' ? 'var(--accent-primary)' : 'var(--text-secondary)',
                      border: 'none',
                      cursor: 'pointer',
                    }}
                  >
                    HI
                  </button>
                </div>

                <Button
                  variant="primary"
                  onClick={() => runAnswer(agentQuestion, agentLang)}
                  disabled={!agentQuestion.trim() || answering}
                  data-testid="ask-agent-button"
                >
                  {answering ? <Spinner size={14} /> : <>{t('ui.askAgent') + " "}<Icon name="send" /></>}
                </Button>
              </div>

              {/* Agent Answer View */}
              <div style={{ flex: 1, overflowY: 'auto' }}>
                {!answerResult ? (
                  <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-tertiary)', fontSize: 13 }}>
                    {t('ui.askAQuestionAboveToReceiveACitationBackedGroundedAnswer')}
                  </div>
                ) : !answerResult.has_sufficient_evidence ? (
                  <div
                    data-testid="insufficient-evidence-banner"
                    style={{
                      padding: 16,
                      borderRadius: 'var(--radius-md)',
                      backgroundColor: 'rgba(234, 179, 8, 0.1)',
                      border: '1px solid rgba(234, 179, 8, 0.3)',
                      color: '#b45309',
                      fontSize: 13,
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 4,
                    }}
                  >
                    <strong><Icon name="warning" /> {t('ui.insufficientEvidence')}</strong>
                    <span>{answerResult.answer}</span>
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    <div
                      data-testid="agent-answer-box"
                      style={{
                        padding: 16,
                        borderRadius: 'var(--radius-lg)',
                        backgroundColor: 'var(--bg-canvas)',
                        border: '1px solid var(--border-subtle)',
                        fontSize: 14,
                        lineHeight: 1.6,
                        color: 'var(--text-primary)',
                      }}
                    >
                      <div style={{ fontWeight: 600, fontSize: 12, color: 'var(--text-secondary)', marginBottom: 6 }}>
                        {t('ui.answer')}
                      </div>
                      <div data-testid="grounded-answer-text">{answerResult.answer}</div>
                    </div>

                    {/* Citations list */}
                    {answerResult.citations.length > 0 && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                        <div style={{ fontWeight: 600, fontSize: 12, color: 'var(--text-secondary)' }}>
                          {t('ui.verifiedReferenceCitations')}{answerResult.citations.length}):
                        </div>
                        {answerResult.citations.map((c, i) => (
                          <div
                            key={i}
                            data-testid={`answer-citation-${i}`}
                            style={{
                              padding: 10,
                              borderRadius: 'var(--radius-sm)',
                              border: '1px solid var(--border-subtle)',
                              backgroundColor: 'var(--bg-canvas)',
                              fontSize: 12,
                            }}
                          >
                            <span style={{ fontWeight: 600, color: 'var(--accent-primary)' }}>
                              {c.citation_id} {c.origin}{c.page_start != null ? t('ui.pageSuffix').replace('{page}', String(c.page_start)) : ''}:
                            </span>{' '}
                            <span style={{ color: 'var(--text-secondary)' }}>{c.snippet}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
