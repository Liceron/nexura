// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { InlineConfirm } from '../../components/InlineConfirm';
import { Icon } from '../../components/Icon';
import { useEffect, useId, useRef, useState } from 'react';
import type { DragEvent, FormEvent } from 'react';
import { useKnowledge } from './useKnowledge';
import { SAMPLE_DOCS } from './types';
import type { HybridCitationItem } from './types';
import { HighlightedSnippet } from './HighlightedSnippet';
import { safeHref } from './highlight';
import { formatBytes, ALLOWED_EXTENSIONS } from './fileUpload';
import { useI18n, languageChoiceEnabled } from '../../i18n';
import { Button } from '../../components/Button';
import { Spinner } from '../../components/Spinner';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { WorkflowTabs } from '../../components/WorkflowTabs';

type KnowledgeTab = 'documents' | 'retrieval' | 'agent';

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
    deleteCollection,
    runRetrieval,
    runAnswer,
    clearError,
    openChunk,
  } = useKnowledge();

  const ids = useId();
  const panelId = `${ids}-panel`;
  const [activeTab, setActiveTab] = useState<KnowledgeTab>('documents');
  const [adding, setAdding] = useState<boolean>(false);
  const [uploadFilename, setUploadFilename] = useState<string>('architecture_guide.md');
  const [uploadContent, setUploadContent] = useState<string>(SAMPLE_DOCS[0].content);
  const [newCollectionName, setNewCollectionName] = useState<string>('');
  const [showNewCollection, setShowNewCollection] = useState<boolean>(false);

  const [searchQuery, setSearchQuery] = useState<string>(t('knowledge.sampleSearch'));
  const [agentQuestion, setAgentQuestion] = useState<string>(t('knowledge.sampleQuestion'));
  const [agentLang, setAgentLang] = useState<'en' | 'hi'>('en');

  const fileInputRef = useRef<HTMLInputElement>(null);
  const chooseFilesRef = useRef<HTMLButtonElement>(null);
  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const newCollectionRef = useRef<HTMLButtonElement>(null);
  const [dragOver, setDragOver] = useState<boolean>(false);
  // The opened source chunk takes focus (it renders above the tabs); Close returns it.
  const chunkPanelRef = useRef<HTMLDivElement>(null);
  const chunkOpenerRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (focusedChunk) chunkPanelRef.current?.focus();
  }, [focusedChunk]);
  const closeChunkPanel = () => {
    closeChunk();
    const opener = chunkOpenerRef.current;
    chunkOpenerRef.current = null;
    requestAnimationFrame(() => {
      if (opener?.isConnected) opener.focus();
    });
  };

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

  const handleUpload = async (e: FormEvent) => {
    e.preventDefault();
    if (!uploadFilename.trim() || !uploadContent.trim()) return;
    const res = await uploadDocument(uploadFilename, uploadContent);
    if (res) {
      setUploadFilename('');
      setUploadContent('');
    }
  };

  const openAddForm = () => {
    setAdding(true);
    // The trigger is replaced by the form; move focus into it.
    requestAnimationFrame(() => chooseFilesRef.current?.focus());
  };
  const closeAddForm = () => {
    setAdding(false);
    requestAnimationFrame(() => addTriggerRef.current?.focus());
  };

  const handleCreateCollection = async (e: FormEvent) => {
    e.preventDefault();
    if (!newCollectionName.trim()) return;
    const id = newCollectionName.toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    await createCollection(id, newCollectionName);
    setNewCollectionName('');
    setShowNewCollection(false);
    newCollectionRef.current?.focus();
  };

  const handleSelectSample = (sampleId: string) => {
    const s = SAMPLE_DOCS.find((item) => item.id === sampleId);
    if (s) {
      setUploadFilename(s.filename);
      setUploadContent(s.content);
    }
  };

  const tabs = [
    { id: 'documents', label: t('knowledge.documentsTab') },
    { id: 'retrieval', label: t('ui.retrievalCitationsPreview') },
    { id: 'agent', label: t('ui.groundedKnowledgeAgent') },
  ];
  const activeLabel = tabs.find((tab) => tab.id === activeTab)?.label;

  return (
    <PageLayout data-testid="knowledge-view" className="knowledge-page">
      <PageHeader
        title={t('nav.knowledge')}
        actions={
          <div className="knowledge-collection">
            <div className="resource-field">
              <label htmlFor={`${ids}-collection`}>{t('knowledge.collection')}</label>
              <select
                id={`${ids}-collection`}
                value={selectedCollection}
                onChange={(e) => setSelectedCollection(e.target.value)}
                data-testid="collection-select"
              >
                {collections.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
            <InlineConfirm key={selectedCollection} consequence={`${t('livefixUi.deleteCollectionConfirm').replace('{id}', selectedCollection)}${selectedCollection === 'default' ? ` ${t('livefixUi.defaultCollectionEmptied')}` : ''}`}>
              <Button variant="danger" size="sm" onClick={() => deleteCollection(selectedCollection)}>{t('livefixUi.deleteCollection')}</Button>
            </InlineConfirm>
            <Button
              ref={newCollectionRef}
              variant="ghost"
              aria-expanded={showNewCollection}
              onClick={() => setShowNewCollection(!showNewCollection)}
              data-testid="new-collection-btn"
            >
              {t('knowledge.newCollection')}
            </Button>
          </div>
        }
        filters={
          showNewCollection ? (
            <form className="resource-inline-form" onSubmit={handleCreateCollection}>
              <div className="resource-field">
                <label htmlFor={`${ids}-new-collection`}>{t('knowledge.collectionName')}</label>
                <input
                  id={`${ids}-new-collection`}
                  type="text"
                  autoFocus
                  value={newCollectionName}
                  onChange={(e) => setNewCollectionName(e.target.value)}
                />
              </div>
              {newCollectionName.trim() && <p role="status" className="resource-help">Collection ID: <code>{newCollectionName.toLowerCase().replace(/[^a-z0-9_-]/g, '-')}</code></p>}
              <Button type="submit" disabled={!newCollectionName.trim()}>
                {t('knowledge.createCollection')}
              </Button>
              <Button type="button" variant="ghost" onClick={() => { setShowNewCollection(false); newCollectionRef.current?.focus(); }}>
                {t('common.cancel')}
              </Button>
            </form>
          ) : undefined
        }
      />

      <div className="resource-notices">
        {limits.is_test_fixture && (
          <p role="note" className="inline-notice">
            {t('knowledge.testEmbeddingsNotice')}
          </p>
        )}
        {error && (
          <div data-testid="knowledge-error-banner" role="alert" className="inline-notice inline-notice--danger">
            <span>{error}</span>
            <button type="button" className="studio-icon-button" onClick={clearError} aria-label={t('ui.dismissError')}>
              <Icon name="close" />
            </button>
          </div>
        )}
      </div>

      {focusedChunk && (
        <div
          ref={chunkPanelRef}
          tabIndex={-1}
          data-testid="source-chunk-panel"
          role="region"
          aria-label={t('knowledge.sourceChunk', 'Source chunk')}
          className="knowledge-chunk"
        >
          <div className="knowledge-chunk-head">
            <strong data-testid="source-chunk-title">
              {focusedChunk.title} · {t('knowledge.chunk', 'Chunk')} {focusedChunk.chunk_index} ·{' '}
              {focusedChunk.start_char}..{focusedChunk.end_char}
            </strong>
            <Button variant="ghost" size="sm" onClick={closeChunkPanel} data-testid="source-chunk-close">
              {t('common.close', 'Close')}
            </Button>
          </div>
          <div data-testid="source-chunk-text" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {focusedChunk.text}
          </div>
        </div>
      )}

      <WorkflowTabs
        tabs={tabs}
        active={activeTab}
        onChange={(id) => setActiveTab(id as KnowledgeTab)}
        label={t('knowledge.tabsLabel')}
        panelId={panelId}
      />

      <div role="tabpanel" id={panelId} aria-label={activeLabel} tabIndex={0} style={{ minWidth: 0 }}>
        {activeTab === 'documents' && (
          <>
            {adding ? (
              <form className="knowledge-add-form" onSubmit={handleUpload} aria-labelledby={`${ids}-add-title`}>
                <h2 id={`${ids}-add-title`}>{t('ui.ingestIndexDocument')}</h2>
                <div
                  data-testid="knowledge-dropzone"
                  className={`knowledge-dropzone${dragOver ? ' knowledge-dropzone--active' : ''}`}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragOver(true);
                  }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={handleDrop}
                >
                  <span>{t('knowledge.dropHint', 'Drag and drop files here, or')}</span>
                  <Button
                    ref={chooseFilesRef}
                    type="button"
                    size="sm"
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
                  <span data-testid="knowledge-size-limit" className="knowledge-limits">
                    {t('knowledge.typesHint', 'Text, Markdown, HTML, PDF')} ·{' '}
                    {t('knowledge.maxPerFile', 'Max per file')}: {formatBytes(limits.max_file_bytes)} ·{' '}
                    {t('knowledge.workspaceUsage', 'Workspace')}: {formatBytes(limits.workspace_used_bytes)} /{' '}
                    {formatBytes(limits.max_workspace_bytes)}
                  </span>
                </div>

                <div className="resource-field">
                  <label htmlFor={`${ids}-filename`}>{t('knowledge.fileName')}</label>
                  <input
                    id={`${ids}-filename`}
                    type="text"
                    value={uploadFilename}
                    onChange={(e) => setUploadFilename(e.target.value)}
                    placeholder={t('ui.documentMdOrGuideTxt')}
                    data-testid="upload-filename-input"
                  />
                </div>

                <div className="resource-field">
                  <label htmlFor={`${ids}-content`}>{t('knowledge.content')}</label>
                  <textarea
                    id={`${ids}-content`}
                    value={uploadContent}
                    onChange={(e) => setUploadContent(e.target.value)}
                    placeholder={t('ui.pasteDocumentTextInEnglishOrHindi')}
                    rows={5}
                    data-testid="upload-content-textarea"
                    style={{ fontFamily: 'inherit', fontSize: 'var(--type-body)' }}
                  />
                  <div className="knowledge-samples">
                    <span>{t('ui.samples')}</span>
                    <Button type="button" size="sm" variant="ghost" onClick={() => handleSelectSample('sample-en-arch')} data-testid="sample-en-btn">
                      {t('ui.englishSdk')}
                    </Button>
                    {languageChoiceEnabled() && <Button type="button" size="sm" variant="ghost" onClick={() => handleSelectSample('sample-hi-guide')} data-testid="sample-hi-btn">
                      {t('ui.guide')}
                    </Button>}
                  </div>
                </div>

                <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                  <Button
                    type="submit"
                    variant="primary"
                    loading={uploading}
                    disabled={!uploadFilename.trim() || !uploadContent.trim()}
                    data-testid="upload-document-button"
                  >
                    {t('ui.ingestIndexDocument')}
                  </Button>
                  <Button type="button" variant="ghost" onClick={closeAddForm}>
                    {t('common.cancel')}
                  </Button>
                </div>
              </form>
            ) : (
              <div className="knowledge-toolbar">
                <Button ref={addTriggerRef} variant="primary" onClick={openAddForm} data-testid="add-document-button">
                  {t('ui.ingestIndexDocument')}
                </Button>
                <span className="resource-help">
                  {t('knowledge.typesHint', 'Text, Markdown, HTML, PDF')} · {t('knowledge.maxPerFile', 'Max per file')}:{' '}
                  {formatBytes(limits.max_file_bytes)}
                </span>
                {!loading && documents.length > 0 && (
                  <span className="knowledge-documents-count" data-testid="knowledge-document-count">
                    {documents.length === 1
                      ? t('knowledge.documentCountOne')
                      : t('knowledge.documentCount').replace('{count}', String(documents.length))}
                  </span>
                )}
              </div>
            )}

            {loading ? (
              <div role="status" aria-busy="true" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '16px 0' }}>
                <Spinner size={16} /> <span>{t('common.loading')}</span>
              </div>
            ) : documents.length === 0 ? (
              <p className="inspection-empty">{t('ui.noDocumentsIndexedInThisCollectionYet')}</p>
            ) : (
              <div className="inspection-table-scroll" role="region" aria-label={t('knowledge.documentsTable')} tabIndex={0}>
                <table className="inspection-table knowledge-documents">
                  <caption className="studio-sr-only">{t('knowledge.documentsTable')}</caption>
                  <thead>
                    <tr>
                      <th scope="col">{t('knowledge.colFile')}</th>
                      <th scope="col">{t('knowledge.colStatus')}</th>
                      <th scope="col" style={{ textAlign: 'right' }}>{t('knowledge.colChars')}</th>
                      <th scope="col" style={{ textAlign: 'right' }}>{t('knowledge.colChunks')}</th>
                      <th scope="col">{t('knowledge.colHash')}</th>
                      <th scope="col"><span className="studio-sr-only">{t('knowledge.colActions')}</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {documents.map((doc) => (
                      <tr key={doc.document_id} data-testid={`document-item-${doc.document_id}`}>
                        <td data-testid="doc-filename">{doc.filename}</td>
                        <td data-testid="doc-status">
                          {doc.status === 'indexed' || !doc.status ? t('knowledge.statusIndexed', 'Indexed') : doc.status}
                        </td>
                        <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{doc.text_length}</td>
                        <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                          {typeof doc.chunk_count === 'number' ? doc.chunk_count : t('ui.unknown')}
                        </td>
                        <td><code>{doc.content_hash.substring(0, 8)}…</code></td>
                        <td>
                          <InlineConfirm key={doc.document_id} consequence={t('confirmation.deleteDocument')}>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="workflow-danger-action"
                              onClick={() => deleteDocument(doc.document_id)}
                              data-testid={`delete-doc-${doc.document_id}`}
                              aria-label={`${t('ui.deleteDocument')} ${doc.filename}`}
                            >
                              {t('ui.delete')}
                            </Button>
                          </InlineConfirm>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}

        {activeTab === 'retrieval' && (
          <>
            <h2 className="knowledge-panel-heading">{t('ui.hybridRetrievalPreview')}</h2>
            <form
              className="knowledge-query"
              onSubmit={(e) => {
                e.preventDefault();
                void runRetrieval(searchQuery);
              }}
            >
              <div className="resource-field">
                <label htmlFor={`${ids}-query`}>{t('knowledge.searchQuery')}</label>
                <input
                  id={`${ids}-query`}
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder={t('ui.enterSearchQueryEnglishOrHindi')}
                  data-testid="retrieval-query-input"
                />
              </div>
              <Button
                type="submit"
                variant="primary"
                loading={retrieving}
                disabled={!searchQuery.trim()}
                data-testid="search-retrieval-button"
              >
                {t('ui.search')}
              </Button>
            </form>
            <details className="resource-help-disclosure">
              <summary>{t('knowledge.howSearchWorks')}</summary>
              <p>{t('ui.searchIndexedChunksUsingFusedDenseVectorSimilarityLexicalBm25KeywordMatchingWi')}</p>
            </details>

            {!retrievalResult ? (
              <p className="inspection-empty">{t('ui.executeASearchQueryAboveToViewRetrievedChunksAndVerifiableCitations')}</p>
            ) : !retrievalResult.has_sufficient_evidence || retrievalResult.citations.length === 0 ? (
              <p data-testid="insufficient-evidence-alert" className="inline-notice inline-notice--warning" style={{ marginTop: 16, maxWidth: 800 }}>
                {t('ui.insufficientEvidenceNoMatchingChunksFoundAboveThresholdInThisCollection')}
              </p>
            ) : (
              <>
                <div className="knowledge-summary" role="status">
                  <span>{t('ui.found') + ' '}<strong>{retrievalResult.citations.length}</strong> {t('ui.citations')}</span>
                  <span>{t('ui.latency') + ' '}{retrievalResult.latency_ms}ms</span>
                </div>
                <ol className="knowledge-results">
                  {(retrievalResult.citations as HybridCitationItem[]).map((cit, idx) => (
                    <li key={cit.citation_id || idx} data-testid={`citation-card-${idx}`}>
                      <div className="knowledge-result-head">
                        <span data-testid="citation-origin" className="knowledge-result-origin" title={cit.origin}>
                          {cit.citation_id} {t('ui.source') + ' '}{cit.origin}
                        </span>
                        {typeof cit.chunk_index === 'number' && (
                          <span data-testid="citation-chunk-index" className="resource-help">
                            {t('knowledge.chunk', 'Chunk')} {cit.chunk_index}
                          </span>
                        )}
                        <code className="resource-help">
                          {t('ui.spanChars') + ' '}{cit.start_char}..{cit.end_char}
                          {cit.page_start != null ? t('ui.pageSuffix').replace('{page}', String(cit.page_start)) : ''}
                        </code>
                      </div>
                      <div data-testid="citation-snippet" className="knowledge-snippet">
                        <HighlightedSnippet
                          text={cit.snippet}
                          highlights={cit.highlights_utf16 ?? cit.highlights}
                          unit={cit.highlights_utf16 ? 'utf16' : 'codepoint'}
                        />
                      </div>
                      <div className="knowledge-result-meta">
                        {cit.matched_by && cit.matched_by.length > 0 && (
                          <span>
                            {t('knowledge.matchedBy').split('{methods}')[0]}
                            <code>{cit.matched_by.join(', ')}</code>
                            {t('knowledge.matchedBy').split('{methods}')[1] ?? ''}
                          </span>
                        )}
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
                            onClick={(e) => {
                              chunkOpenerRef.current = e.currentTarget;
                              void openChunk(cit.document_id, cit.chunk_index as number);
                            }}
                          >
                            {t('citations.openChunk', 'Open source chunk')}
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
              </>
            )}
          </>
        )}

        {activeTab === 'agent' && (
          <>
            <h2 data-testid="knowledge-agent-badge" className="knowledge-panel-heading" style={{ marginBottom: 4 }}>
              {t('ui.groundedKnowledgeAgentLocalOfflineFixture')}
            </h2>
            <p className="knowledge-panel-help">
              {t('ui.answersQuestionsStrictlyGroundedInVerifiableSourceCitationsIfNoEvidenceExistsE')}
            </p>
            <form
              className="knowledge-query"
              onSubmit={(e) => {
                e.preventDefault();
                void runAnswer(agentQuestion, agentLang);
              }}
            >
              <div className="resource-field">
                <label htmlFor={`${ids}-question`}>{t('knowledge.question')}</label>
                <input
                  id={`${ids}-question`}
                  type="text"
                  value={agentQuestion}
                  onChange={(e) => setAgentQuestion(e.target.value)}
                  placeholder={t('ui.askAQuestionGroundedInKnowledgeBase')}
                  data-testid="agent-question-input"
                />
              </div>
              <div className="resource-field resource-field--fit">
                <span className="resource-label" id={`${ids}-answer-lang`}>{t('knowledge.answerLanguage')}</span>
                <div className="segmented" role="group" aria-labelledby={`${ids}-answer-lang`}>
                  <button type="button" aria-pressed={agentLang === 'en'} onClick={() => setAgentLang('en')} data-testid="agent-lang-en">
                    {t('ui.english')}
                  </button>
                  <button type="button" aria-pressed={agentLang === 'hi'} onClick={() => setAgentLang('hi')} data-testid="agent-lang-hi" lang="hi">
                    हिन्दी
                  </button>
                </div>
              </div>
              <Button
                type="submit"
                variant="primary"
                loading={answering}
                disabled={!agentQuestion.trim()}
                data-testid="ask-agent-button"
              >
                {t('ui.send')}
              </Button>
            </form>

            {!answerResult ? (
              <p className="inspection-empty">{t('ui.askAQuestionAboveToReceiveACitationBackedGroundedAnswer')}</p>
            ) : !answerResult.has_sufficient_evidence ? (
              <div data-testid="insufficient-evidence-banner" className="inline-notice inline-notice--warning" style={{ marginTop: 16, maxWidth: 800 }}>
                <strong className="inline-notice-title">{t('ui.insufficientEvidence')}</strong>
                <span>{answerResult.answer}</span>
              </div>
            ) : (
              <div className="knowledge-answer">
                <section data-testid="agent-answer-box" aria-labelledby={`${ids}-answer`}>
                  <h3 id={`${ids}-answer`} className="resource-label" style={{ marginBottom: 4 }}>{t('knowledge.answer')}</h3>
                  <div data-testid="grounded-answer-text" className="knowledge-answer-text">{answerResult.answer}</div>
                </section>
                {answerResult.citations.length > 0 && (
                  <section aria-labelledby={`${ids}-answer-sources`}>
                    <h3 id={`${ids}-answer-sources`} className="resource-label" style={{ marginBottom: 4 }}>
                      {t('ui.verifiedReferenceCitations')}{answerResult.citations.length})
                    </h3>
                    <ol className="knowledge-answer-citations">
                      {answerResult.citations.map((c, i) => (
                        <li key={i} data-testid={`answer-citation-${i}`}>
                          <strong>
                            {c.citation_id} {c.origin}
                            {c.page_start != null ? t('ui.pageSuffix').replace('{page}', String(c.page_start)) : ''}:
                          </strong>{' '}
                          <span style={{ color: 'var(--text-secondary)' }}>{c.snippet}</span>
                        </li>
                      ))}
                    </ol>
                  </section>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </PageLayout>
  );
}
