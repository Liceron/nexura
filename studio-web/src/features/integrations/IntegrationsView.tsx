// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useAfterRender } from '../../app/useAfterRender';
import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useIntegrations } from './useIntegrations';
import { Button } from '../../components/Button';
import { InlineConfirm } from '../../components/InlineConfirm';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { WorkflowTabs } from '../../components/WorkflowTabs';
import { useI18n } from '../../i18n';
import { useCompactLayout } from '../../app/useCompactLayout';

const MCP_SAMPLE_ARGS: Record<string, string> = {
  math_add: '{\n  "x": 15,\n  "y": 27\n}',
  math_multiply: '{\n  "a": 6,\n  "b": 7\n}',
  echo: '{\n  "message": "Hello from Nexura Studio"\n}',
  reverse_text: '{\n  "text": "Nexura Standalone SDK"\n}',
};
const OPENAPI_SAMPLE_INPUTS: Record<string, string> = {
  getPetById: '{\n  "petId": "123"\n}',
  deletePet: '{\n  "petId": "123"\n}',
  addPet: '{\n  "body": {\n    "name": "Bruno",\n    "tag": "guard-dog"\n  }\n}',
  getCurrentWeather: '{\n  "city": "Bengaluru"\n}',
};

/** Reads, writes and side-effect-free operations, from the descriptor's effect policy. */
function EffectLabel({ policy: reported }: { policy?: string | null }) {
  const { t } = useI18n();
  // An older service or a custom import may not report a policy: say so, never guess (review minor 5).
  if (!reported) return <span data-effect="unknown">{t('integrations.effect.unknown')}</span>;
  const policy = reported;
  const writes = policy.includes('write');
  const known = ['pure', 'read', 'idempotent_write', 'non_idempotent_write'].includes(policy);
  return (
    <span className={writes ? 'resource-row-attention' : undefined} data-effect={policy}>
      {known ? t(`integrations.effect.${policy}`) : <code>{policy}</code>}
    </span>
  );
}

export function IntegrationsView() {
  const { t } = useI18n();
  const {
    status,
    loading,
    error,
    registrations,
    registrationsError,
    registrationsLoading,
    refreshRegistrations,
    removeRegistration,

    // MCP
    mcpServerName,
    setMcpServerName,
    mcpFixtureId,
    setMcpFixtureId,
    mcpTools,
    selectedMcpTool,
    setSelectedMcpTool,
    mcpArgsText,
    setMcpArgsText,
    mcpRunResult,
    mcpRunning,
    mcpRegistered,
    discoverMcp,
    testRunMcp,
    registerMcpTool,

    // OpenAPI
    openapiPrefix,
    setOpenapiPrefix,
    openapiFixtureId,
    setOpenapiFixtureId,
    openapiSpecText,
    setOpenapiSpecText,
    openapiOperations,
    selectedOpenAPIOp,
    setSelectedOpenAPIOp,
    openapiInputsText,
    setOpenapiInputsText,
    openapiRunResult,
    openapiRunning,
    openapiRegistered,
    discoverOpenAPI,
    testRunOpenAPI,
    registerOpenAPIOp,
  } = useIntegrations();

  const ids = useId();
  const panelId = `${ids}-panel`;
  const compact = useCompactLayout();
  const [activeTab, setActiveTab] = useState<'mcp' | 'openapi'>('mcp');
  const [customSpecMode, setCustomSpecMode] = useState(false);
  // Phones show the source list, then the operation and its result, one at a time.
  const [phonePane, setPhonePane] = useState<'list' | 'detail'>('list');
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const afterRender = useAfterRender();
  const listRef = useRef<HTMLUListElement>(null);
  useEffect(() => setPhonePane('list'), [activeTab]);

  const mcpAvailable = status?.mcp.available ?? false;
  const openapiAvailable = status?.openapi.available ?? false;
  const availability = (available: boolean) => (available ? t('ui.active') : t('ui.unavailable'));

  const openDetail = (select: () => void) => {
    select();
    setPhonePane('detail');
    if (compact) afterRender(() => detailHeadingRef.current?.focus());
  };
  const backToList = () => {
    setPhonePane('list');
    afterRender(() => listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus());
  };

  const tabs = [
    { id: 'mcp', label: `${t('integrations.tabMcp')} · ${availability(mcpAvailable)}` },
    { id: 'openapi', label: `${t('integrations.tabOpenApi')} · ${availability(openapiAvailable)}` },
  ];
  const hasSelection = activeTab === 'mcp' ? Boolean(selectedMcpTool) : Boolean(selectedOpenAPIOp);

  const resultBlock = (
    ok: boolean,
    statusText: string,
    durationMs: number,
    label: string,
    body: unknown,
  ) => (
    <section className="tools-result" aria-labelledby={`${ids}-result`} data-testid="tool-run-result">
      <div className="tools-result-head">
        <h3 id={`${ids}-result`} className="resource-label">{t('ui.executionOutput')}</h3>
        <span className={`run-status run-status--${ok ? 'success' : 'danger'}`} data-testid="tool-run-status">
          <span aria-hidden="true">●</span> {statusText}
        </span>
        <span className="resource-help">{durationMs} ms</span>
      </div>
      <p className="resource-help" data-testid="tool-run-label">{label}</p>
      <pre className="inspection-code tools-result-body" tabIndex={0}>{JSON.stringify(body, null, 2)}</pre>
    </section>
  );

  const detailPane = (children: ReactNode) => (
    <div className="resource-detail">
      <Button type="button" variant="ghost" size="sm" className="resource-back" onClick={backToList}>
        {t('integrations.back')}
      </Button>
      {children}
    </div>
  );

  return (
    <PageLayout data-testid="integrations-view">
      <PageHeader title={t('integrations.title')} description={t('integrations.subtitle')} />
      <section aria-label={t('livefixUi.registrations')} style={{ marginBottom: 24 }}>
        <h2>{t('livefixUi.registrations')}</h2>
        <p className="resource-help">{t('livefixUi.registrationsHelp')}</p>
        <Button variant="ghost" size="sm" loading={registrationsLoading} onClick={refreshRegistrations}>{t('livefixUi.refreshRegistrations')}</Button>
        {registrationsError && <p role="alert">{registrationsError}</p>}
        {!registrationsLoading && !registrationsError && registrations.length === 0 && <p>{t('livefixUi.noRegistrations')}</p>}
        {registrations.map(row => <div key={row.module_id} className="resource-actions">
          <code>{row.module_id}</code><span>{row.kind} · {row.server_name_or_prefix}</span>
          <InlineConfirm consequence={t('livefixUi.removeRegistrationConfirm').replace('{id}', row.module_id).replace('{server}', row.server_name_or_prefix)}>
            <Button variant="danger" size="sm" onClick={() => removeRegistration(row.module_id)}>{t('livefixUi.remove')}</Button>
          </InlineConfirm>
        </div>)}
      </section>

      {error && (
        <div role="alert" className="inline-notice inline-notice--danger" style={{ marginBottom: 16 }}>
          <span>{error}</span>
        </div>
      )}

      <WorkflowTabs tabs={tabs} active={activeTab} onChange={(id) => setActiveTab(id as 'mcp' | 'openapi')} label={t('integrations.tabsLabel')} panelId={panelId} />

      <div role="tabpanel" id={panelId} aria-label={tabs.find((tab) => tab.id === activeTab)?.label} tabIndex={0} style={{ minWidth: 0 }}>
        {activeTab === 'mcp' && (
          <>
            {!mcpAvailable && status?.mcp.missing_reason && (
              <p className="inline-notice inline-notice--warning" style={{ marginBottom: 16 }}>
                <strong className="inline-notice-title">{t('ui.mcpIntegrationUnavailable')}</strong>
                {t('ui.reason') + ' '}{status.mcp.missing_reason}
              </p>
            )}
            <form
              className="resource-inline-form tools-connect"
              aria-label={t('ui.configureLocalMcpServerFixture')}
              onSubmit={(e) => {
                e.preventDefault();
                void discoverMcp(mcpFixtureId, mcpServerName);
              }}
            >
              <div className="resource-field">
                <label htmlFor={`${ids}-mcp-fixture`}>{t('ui.localFixture')}</label>
                <select id={`${ids}-mcp-fixture`} value={mcpFixtureId} onChange={(e) => setMcpFixtureId(e.target.value)}>
                  <option value="math_fixture">{t('ui.mathCalculatorMathAddMathMultiply')}</option>
                  <option value="echo_fixture">{t('ui.echoTextTransformerEchoReverseText')}</option>
                </select>
              </div>
              <div className="resource-field" style={{ flex: '0 1 160px' }}>
                <label htmlFor={`${ids}-mcp-prefix`}>{t('ui.serverPrefix')}</label>
                <input id={`${ids}-mcp-prefix`} value={mcpServerName} onChange={(e) => setMcpServerName(e.target.value)} placeholder="calc" />
              </div>
              <Button type="submit" variant={mcpTools.length ? 'secondary' : 'primary'} loading={loading}>
                {t('ui.discoverTools')}
              </Button>
            </form>

            <div className={`studio-list-detail tools-panes resource-layout--${phonePane === 'detail' && hasSelection ? 'detail' : 'browse'}`}>
              <div className="resource-list-pane">
                <h2 className="resource-label tools-list-title">
                  {t('ui.discoveredMcpTools')}{mcpTools.length})
                </h2>
                {mcpTools.length === 0 ? (
                  <p className="resource-empty">{t('ui.noToolsDiscoveredYetClickQuotDiscoverToolsQuotAbove')}</p>
                ) : (
                  <ul ref={listRef} className="resource-list" aria-label={t('ui.discoveredMcpTools').replace(/\s*\($/, '')}>
                    {mcpTools.map((tool) => (
                      <li key={tool.name}>
                        <button
                          type="button"
                          className="resource-row"
                          aria-pressed={selectedMcpTool?.name === tool.name}
                          data-testid={`mcp-tool-${tool.name}`}
                          onClick={() =>
                            openDetail(() => {
                              setSelectedMcpTool(tool);
                              if (MCP_SAMPLE_ARGS[tool.name]) setMcpArgsText(MCP_SAMPLE_ARGS[tool.name]);
                            })
                          }
                        >
                          <code className="resource-row-name">{tool.name}</code>
                          {tool.description && <span className="resource-row-description">{tool.description}</span>}
                          <span className="resource-row-meta">
                            <EffectLabel policy={tool.effect_policy} />
                            {mcpRegistered[tool.name] && <span>{t('ui.inPalette')}</span>}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {detailPane(
                selectedMcpTool ? (
                  <div className="tools-detail">
                    <h2 ref={detailHeadingRef} tabIndex={-1}><code>{selectedMcpTool.name}</code></h2>
                    <div className="inspection-meta">
                      <EffectLabel policy={selectedMcpTool.effect_policy} />
                      <span>{t('integrations.digest')} <code>{selectedMcpTool.digest.slice(0, 12)}</code></span>
                    </div>
                    <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                      <Button
                        size="sm"
                        disabled={mcpRegistered[selectedMcpTool.name]}
                        onClick={() => registerMcpTool(selectedMcpTool.name)}
                      >
                        {mcpRegistered[selectedMcpTool.name] ? t('ui.inPalette') : t('ui.addToPalette')}
                      </Button>
                    </div>
                    <form
                      className="resource-form"
                      style={{ gap: 12 }}
                      onSubmit={(e) => {
                        e.preventDefault();
                        void testRunMcp(selectedMcpTool.name, mcpArgsText);
                      }}
                    >
                      <div className="resource-field">
                        <label htmlFor={`${ids}-mcp-args`}>{t('ui.argumentsJson')}</label>
                        <textarea
                          id={`${ids}-mcp-args`}
                          value={mcpArgsText}
                          onChange={(e) => setMcpArgsText(e.target.value)}
                          rows={6}
                          spellCheck={false}
                          aria-describedby={`${ids}-mcp-mode`}
                        />
                        <span id={`${ids}-mcp-mode`} className="resource-help">{t('integrations.fixtureMode')}</span>
                      </div>
                      <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                        <Button type="submit" variant="primary" loading={mcpRunning}>
                          {t('ui.executeDryRun')}
                        </Button>
                      </div>
                    </form>
                    {mcpRunResult &&
                      resultBlock(
                        mcpRunResult.status === 'success',
                        t(`statusUpper.${mcpRunResult.status}`, mcpRunResult.status),
                        mcpRunResult.duration_ms,
                        mcpRunResult.execution_label,
                        mcpRunResult.content || mcpRunResult.output,
                      )}
                  </div>
                ) : (
                  <p className="resource-empty">{t('ui.selectAnMcpToolOnTheLeftToConfigureArgumentsAndExecuteATestRun')}</p>
                ),
              )}
            </div>
          </>
        )}

        {activeTab === 'openapi' && (
          <>
            {!openapiAvailable && status?.openapi.missing_reason && (
              <p className="inline-notice inline-notice--warning" style={{ marginBottom: 16 }}>
                <strong className="inline-notice-title">{t('ui.openapiIntegrationUnavailable')}</strong>
                {t('ui.reason') + ' '}{status.openapi.missing_reason}
              </p>
            )}
            <form
              className="tools-connect"
              aria-label={t('ui.configureOpenapiSpecificationFixture')}
              onSubmit={(e) => {
                e.preventDefault();
                void discoverOpenAPI(openapiFixtureId, openapiPrefix, customSpecMode ? openapiSpecText : undefined);
              }}
            >
              <div className="resource-inline-form">
                <div className="resource-field">
                  <label htmlFor={`${ids}-oa-source`}>{t('ui.specificationSource')}</label>
                  <select
                    id={`${ids}-oa-source`}
                    value={customSpecMode ? 'custom' : openapiFixtureId}
                    onChange={(e) => {
                      if (e.target.value === 'custom') {
                        setCustomSpecMode(true);
                      } else {
                        setCustomSpecMode(false);
                        setOpenapiFixtureId(e.target.value);
                        setOpenapiPrefix(e.target.value === 'weather_fixture' ? 'weather' : 'petstore');
                      }
                    }}
                  >
                    <option value="petstore_fixture">{t('ui.petstoreSampleApiGetpetbyidAddpetDeletepet')}</option>
                    <option value="weather_fixture">{t('ui.weatherSampleApiGetcurrentweather')}</option>
                    <option value="custom">{t('ui.pasteCustomOpenapiJson')}</option>
                  </select>
                </div>
                <div className="resource-field" style={{ flex: '0 1 160px' }}>
                  <label htmlFor={`${ids}-oa-prefix`}>{t('ui.modulePrefix')}</label>
                  <input id={`${ids}-oa-prefix`} value={openapiPrefix} onChange={(e) => setOpenapiPrefix(e.target.value)} placeholder="api" />
                </div>
                <Button type="submit" variant={openapiOperations.length ? 'secondary' : 'primary'} loading={loading}>
                  {t('ui.discoverEndpoints')}
                </Button>
              </div>
              {customSpecMode && (
                <div className="resource-field" style={{ marginTop: 12, maxWidth: 720 }}>
                  <label htmlFor={`${ids}-oa-spec`}>{t('ui.openapi3XSpecificationJsonYaml')}</label>
                  <textarea
                    id={`${ids}-oa-spec`}
                    value={openapiSpecText}
                    onChange={(e) => setOpenapiSpecText(e.target.value)}
                    placeholder='{"openapi": "3.0.0", "info": {"title": "My API", "version": "1.0.0"}, "paths": {...}}'
                    rows={8}
                    spellCheck={false}
                  />
                </div>
              )}
            </form>

            <div className={`studio-list-detail tools-panes resource-layout--${phonePane === 'detail' && hasSelection ? 'detail' : 'browse'}`}>
              <div className="resource-list-pane">
                <h2 className="resource-label tools-list-title">
                  {t('ui.discoveredEndpoints')}{openapiOperations.length})
                </h2>
                {openapiOperations.length === 0 ? (
                  <p className="resource-empty">{t('ui.noEndpointsDiscoveredYetClickQuotDiscoverEndpointsQuotAbove')}</p>
                ) : (
                  <ul ref={listRef} className="resource-list" aria-label={t('ui.discoveredEndpoints').replace(/\s*\($/, '')}>
                    {openapiOperations.map((op) => (
                      <li key={op.operation_id}>
                        <button
                          type="button"
                          className="resource-row"
                          aria-pressed={selectedOpenAPIOp?.operation_id === op.operation_id}
                          data-testid={`openapi-op-${op.operation_id}`}
                          onClick={() =>
                            openDetail(() => {
                              setSelectedOpenAPIOp(op);
                              if (OPENAPI_SAMPLE_INPUTS[op.operation_id]) setOpenapiInputsText(OPENAPI_SAMPLE_INPUTS[op.operation_id]);
                            })
                          }
                        >
                          <code className="resource-row-name">{op.operation_id}</code>
                          <span className="resource-row-description">
                            <code>{op.method}</code> <code>{op.path}</code>
                          </span>
                          <span className="resource-row-meta">
                            <EffectLabel policy={op.effect_policy} />
                            {openapiRegistered[op.operation_id] && <span>{t('ui.inPalette')}</span>}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {detailPane(
                selectedOpenAPIOp ? (
                  <div className="tools-detail">
                    <h2 ref={detailHeadingRef} tabIndex={-1}><code>{selectedOpenAPIOp.operation_id}</code></h2>
                    <div className="inspection-meta">
                      <span><code>{selectedOpenAPIOp.method}</code> <code>{selectedOpenAPIOp.path}</code></span>
                      <EffectLabel policy={selectedOpenAPIOp.effect_policy} />
                    </div>
                    {selectedOpenAPIOp.summary && <p style={{ color: 'var(--text-secondary)' }}>{selectedOpenAPIOp.summary}</p>}
                    <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                      <Button
                        size="sm"
                        disabled={openapiRegistered[selectedOpenAPIOp.operation_id]}
                        onClick={() => registerOpenAPIOp(selectedOpenAPIOp.operation_id)}
                      >
                        {openapiRegistered[selectedOpenAPIOp.operation_id] ? t('ui.inPalette') : t('ui.addToPalette')}
                      </Button>
                    </div>
                    <form
                      className="resource-form"
                      style={{ gap: 12 }}
                      onSubmit={(e) => {
                        e.preventDefault();
                        void testRunOpenAPI(selectedOpenAPIOp.operation_id, openapiInputsText);
                      }}
                    >
                      <div className="resource-field">
                        <label htmlFor={`${ids}-oa-inputs`}>{t('ui.inputsPathQueryHeadersBodyJson')}</label>
                        <textarea
                          id={`${ids}-oa-inputs`}
                          value={openapiInputsText}
                          onChange={(e) => setOpenapiInputsText(e.target.value)}
                          rows={6}
                          spellCheck={false}
                          aria-describedby={`${ids}-oa-mode`}
                        />
                        <span id={`${ids}-oa-mode`} className="resource-help">{t('integrations.fixtureMode')}</span>
                      </div>
                      <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                        <Button type="submit" variant="primary" loading={openapiRunning}>
                          {t('ui.executeDryRun')}
                        </Button>
                      </div>
                    </form>
                    {openapiRunResult &&
                      resultBlock(
                        openapiRunResult.status_code < 400,
                        String(openapiRunResult.status_code),
                        openapiRunResult.duration_ms,
                        openapiRunResult.execution_label,
                        openapiRunResult.body,
                      )}
                  </div>
                ) : (
                  <p className="resource-empty">{t('ui.selectAnEndpointOnTheLeftToConfigureInputsAndExecuteATestRun')}</p>
                ),
              )}
            </div>
          </>
        )}
      </div>
    </PageLayout>
  );
}
