// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState } from 'react';
import { useIntegrations } from './useIntegrations';
import { Badge, BadgeVariant } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Input } from '../../components/Input';
import { Alert } from '../../components/Alert';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';

export function IntegrationsView() {
  const { t } = useI18n();
  const {
    status,
    loading,
    error,
    refreshStatus,

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

  const [activeTab, setActiveTab] = useState<'mcp' | 'openapi'>('mcp');
  const [customSpecMode, setCustomSpecMode] = useState(false);

  const mcpAvailable = status?.mcp.available ?? false;
  const openapiAvailable = status?.openapi.available ?? false;

  return (
    <div data-testid="integrations-view" style={{ padding: 24, maxWidth: 1200, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Top Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', borderBottom: '1px solid var(--border-subtle)', paddingBottom: 16 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: 'var(--text-primary)' }}>
            {t('integrations.title') || t('ui.toolsIntegrations')}
          </h1>
          <p style={{ margin: '4px 0 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('integrations.subtitle') || t('ui.discoverConfigureAndDryRunLocalMcpToolsAndOpenapiFixturesThroughScopedBackend')}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" size="sm" onClick={refreshStatus} disabled={loading}>
            {loading ? <Spinner size={14} /> : <><Icon name="refresh" /> {t('ui.refresh')}</>}
          </Button>
        </div>
      </div>

      {error && (
        <Alert variant="danger" title={t('ui.integrationError')}>
          {error}
        </Alert>
      )}

      {/* Tabs */}
      <div role="tablist" style={{ display: 'flex', gap: 12, borderBottom: '1px solid var(--border-subtle)', paddingBottom: 4 }}>
        <button
          data-testid="tab-mcp"
          role="tab"
          aria-selected={activeTab === 'mcp'}
          onClick={() => setActiveTab('mcp')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'mcp' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'mcp' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'mcp' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span><Icon name="plug" /> {t('ui.modelContextProtocolMcp')}</span>
          <Badge variant={mcpAvailable ? 'success' : 'danger'}>
            {mcpAvailable ? t('ui.active') : t('ui.unavailable')}
          </Badge>
        </button>
        <button
          data-testid="tab-openapi"
          role="tab"
          aria-selected={activeTab === 'openapi'}
          onClick={() => setActiveTab('openapi')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'openapi' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'openapi' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'openapi' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span><Icon name="globe" /> {t('ui.openapi3XConnectors')}</span>
          <Badge variant={openapiAvailable ? 'success' : 'danger'}>
            {openapiAvailable ? t('ui.active') : t('ui.unavailable')}
          </Badge>
        </button>
      </div>

      {/* TAB 1: MCP */}
      {activeTab === 'mcp' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          {!mcpAvailable && status?.mcp.missing_reason && (
            <Alert variant="warning" title={t('ui.mcpIntegrationUnavailable')}>
              {t('ui.reason') + " "}{status.mcp.missing_reason}
            </Alert>
          )}

          {/* Discovery Card */}
          <div
            style={{
              padding: 16,
              backgroundColor: 'var(--bg-surface)',
              borderRadius: 'var(--radius-md)',
              border: '1px solid var(--border-subtle)',
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
            }}
          >
            <div style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)' }}>
              {t('ui.configureLocalMcpServerFixture')}
            </div>
            <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 240 }}>
                <label style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 500 }}>
                  {t('ui.localFixture')}
                </label>
                <select
                  value={mcpFixtureId}
                  onChange={(e) => setMcpFixtureId(e.target.value)}
                  style={{
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-canvas)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                >
                  <option value="math_fixture">{t('ui.mathCalculatorMathAddMathMultiply')}</option>
                  <option value="echo_fixture">{t('ui.echoTextTransformerEchoReverseText')}</option>
                </select>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, width: 160 }}>
                <label style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 500 }}>
                  {t('ui.serverPrefix')}
                </label>
                <Input
                  value={mcpServerName}
                  onChange={(e) => setMcpServerName(e.target.value)}
                  placeholder="calc"
                />
              </div>

              <Button
                variant="primary"
                size="md"
                onClick={() => discoverMcp(mcpFixtureId, mcpServerName)}
                disabled={loading}
              >
                {loading ? <Spinner size={14} /> : t('ui.discoverTools')}
              </Button>
            </div>
          </div>

          {/* Two-Column Tools & Test Runner View */}
          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
            {/* Left: Tools List */}
            <div
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--border-subtle)',
                padding: 16,
                display: 'flex',
                flexDirection: 'column',
                gap: 12,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontWeight: 600, fontSize: 14 }}>
                  {t('ui.discoveredMcpTools')}{mcpTools.length})
                </span>
                <Badge variant="info">{t('ui.localMockTransport')}</Badge>
              </div>

              {mcpTools.length === 0 ? (
                <div style={{ color: 'var(--text-secondary)', fontSize: 13, padding: '24px 0', textAlign: 'center' }}>
                  {t('ui.noToolsDiscoveredYetClickQuotDiscoverToolsQuotAbove')}
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {mcpTools.map((tool) => {
                    const isSelected = selectedMcpTool?.name === tool.name;
                    const isRegistered = mcpRegistered[tool.name];
                    return (
                      <div
                        key={tool.name}
                        onClick={() => {
                          setSelectedMcpTool(tool);
                          if (tool.name === 'math_add') {
                            setMcpArgsText('{\n  "x": 15,\n  "y": 27\n}');
                          } else if (tool.name === 'math_multiply') {
                            setMcpArgsText('{\n  "a": 6,\n  "b": 7\n}');
                          } else if (tool.name === 'echo') {
                            setMcpArgsText('{\n  "message": "Hello from Nexura Studio"\n}');
                          } else if (tool.name === 'reverse_text') {
                            setMcpArgsText('{\n  "text": "Nexura Standalone SDK"\n}');
                          }
                        }}
                        style={{
                          padding: 12,
                          borderRadius: 'var(--radius-sm)',
                          border: isSelected ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                          backgroundColor: isSelected ? 'var(--accent-subtle)' : 'var(--bg-canvas)',
                          cursor: 'pointer',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 6,
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
                            {tool.name}
                          </span>
                          <div style={{ display: 'flex', gap: 6 }}>
                            <Badge variant="neutral">{tool.digest.slice(0, 8)}</Badge>
                            {isRegistered && <Badge variant="success"><Icon name="check" /> {t('ui.inPalette')}</Badge>}
                          </div>
                        </div>
                        <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                          {tool.description}
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 4 }}>
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={isRegistered}
                            onClick={(e) => {
                              e.stopPropagation();
                              registerMcpTool(tool.name);
                            }}
                          >
                            {isRegistered ? t('ui.registered') : t('ui.addToPalette')}
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Right: Test Run Console */}
            <div
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--border-subtle)',
                padding: 16,
                display: 'flex',
                flexDirection: 'column',
                gap: 12,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontWeight: 600, fontSize: 14 }}>
                  {t('ui.toolDryRun') + " "}{selectedMcpTool ? selectedMcpTool.name : t('ui.selectATool')}
                </span>
                <Badge variant="warning">{t('ui.fixtureDryRunLocalMockTransport')}</Badge>
              </div>

              {selectedMcpTool ? (
                <>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <label style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 500 }}>
                      {t('ui.argumentsJson')}
                    </label>
                    <textarea
                      value={mcpArgsText}
                      onChange={(e) => setMcpArgsText(e.target.value)}
                      rows={6}
                      aria-label={t('ui.mcpToolArguments')}
                      style={{
                        padding: 10,
                        fontFamily: 'var(--font-mono)',
                        fontSize: 12,
                        borderRadius: 'var(--radius-sm)',
                        border: '1px solid var(--border-subtle)',
                        backgroundColor: 'var(--bg-canvas)',
                        color: 'var(--text-primary)',
                      }}
                    />
                  </div>

                  <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                    <Button
                      variant="primary"
                      size="md"
                      disabled={mcpRunning}
                      onClick={() => testRunMcp(selectedMcpTool.name, mcpArgsText)}
                    >
                      {mcpRunning ? <Spinner size={14} /> : t('ui.executeDryRun')}
                    </Button>
                  </div>

                  {/* Output */}
                  {mcpRunResult && (
                    <div
                      style={{
                        marginTop: 10,
                        padding: 12,
                        backgroundColor: 'var(--bg-canvas)',
                        borderRadius: 'var(--radius-sm)',
                        border: '1px solid var(--border-subtle)',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 8,
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span style={{ fontSize: 12, fontWeight: 600 }}>{t('ui.executionOutput')}</span>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <Badge variant={mcpRunResult.status === 'success' ? 'success' : 'danger'}>
                            {t(`statusUpper.${mcpRunResult.status}`, mcpRunResult.status.toUpperCase())}
                          </Badge>
                          <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                            {mcpRunResult.duration_ms} ms
                          </span>
                        </div>
                      </div>
                      <div style={{ fontSize: 11, color: 'var(--text-secondary)', fontStyle: 'italic' }}>
                        {t('ui.label') + " "}{mcpRunResult.execution_label}
                      </div>
                      <pre
                        style={{
                          margin: 0,
                          padding: 8,
                          backgroundColor: 'var(--bg-surface)',
                          borderRadius: 'var(--radius-sm)',
                          fontSize: 12,
                          fontFamily: 'var(--font-mono)',
                          overflowX: 'auto',
                        }}
                      >
                        {JSON.stringify(mcpRunResult.content || mcpRunResult.output, null, 2)}
                      </pre>
                    </div>
                  )}
                </>
              ) : (
                <div style={{ color: 'var(--text-secondary)', fontSize: 13, padding: '36px 0', textAlign: 'center' }}>
                  {t('ui.selectAnMcpToolOnTheLeftToConfigureArgumentsAndExecuteATestRun')}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: OpenAPI */}
      {activeTab === 'openapi' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          {!openapiAvailable && status?.openapi.missing_reason && (
            <Alert variant="warning" title={t('ui.openapiIntegrationUnavailable')}>
              {t('ui.reason') + " "}{status.openapi.missing_reason}
            </Alert>
          )}

          {/* Discovery Card */}
          <div
            style={{
              padding: 16,
              backgroundColor: 'var(--bg-surface)',
              borderRadius: 'var(--radius-md)',
              border: '1px solid var(--border-subtle)',
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
            }}
          >
            <div style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)' }}>
              {t('ui.configureOpenapiSpecificationFixture')}
            </div>
            <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 260 }}>
                <label style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 500 }}>
                  {t('ui.specificationSource')}
                </label>
                <select
                  value={customSpecMode ? 'custom' : openapiFixtureId}
                  onChange={(e) => {
                    if (e.target.value === 'custom') {
                      setCustomSpecMode(true);
                    } else {
                      setCustomSpecMode(false);
                      setOpenapiFixtureId(e.target.value);
                      if (e.target.value === 'weather_fixture') {
                        setOpenapiPrefix('weather');
                      } else {
                        setOpenapiPrefix('petstore');
                      }
                    }
                  }}
                  style={{
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-canvas)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                >
                  <option value="petstore_fixture">{t('ui.petstoreSampleApiGetpetbyidAddpetDeletepet')}</option>
                  <option value="weather_fixture">{t('ui.weatherSampleApiGetcurrentweather')}</option>
                  <option value="custom">{t('ui.pasteCustomOpenapiJson')}</option>
                </select>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, width: 160 }}>
                <label style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 500 }}>
                  {t('ui.modulePrefix')}
                </label>
                <Input
                  value={openapiPrefix}
                  onChange={(e) => setOpenapiPrefix(e.target.value)}
                  placeholder="api"
                />
              </div>

              <Button
                variant="primary"
                size="md"
                onClick={() =>
                  discoverOpenAPI(
                    openapiFixtureId,
                    openapiPrefix,
                    customSpecMode ? openapiSpecText : undefined
                  )
                }
                disabled={loading}
              >
                {loading ? <Spinner size={14} /> : t('ui.discoverEndpoints')}
              </Button>
            </div>

            {customSpecMode && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 8 }}>
                <label style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 500 }}>
                  {t('ui.openapi3XSpecificationJsonYaml')}
                </label>
                <textarea
                  value={openapiSpecText}
                  onChange={(e) => setOpenapiSpecText(e.target.value)}
                  placeholder='{"openapi": "3.0.0", "info": {"title": "My API", "version": "1.0.0"}, "paths": {...}}'
                  rows={8}
                  style={{
                    padding: 10,
                    fontFamily: 'var(--font-mono)',
                    fontSize: 12,
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-canvas)',
                    color: 'var(--text-primary)',
                  }}
                />
              </div>
            )}
          </div>

          {/* Two-Column Operations & Test Runner View */}
          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
            {/* Left: Operations List */}
            <div
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--border-subtle)',
                padding: 16,
                display: 'flex',
                flexDirection: 'column',
                gap: 12,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontWeight: 600, fontSize: 14 }}>
                  {t('ui.discoveredEndpoints')}{openapiOperations.length})
                </span>
                <Badge variant="info">{t('ui.localMockTransport')}</Badge>
              </div>

              {openapiOperations.length === 0 ? (
                <div style={{ color: 'var(--text-secondary)', fontSize: 13, padding: '24px 0', textAlign: 'center' }}>
                  {t('ui.noEndpointsDiscoveredYetClickQuotDiscoverEndpointsQuotAbove')}
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {openapiOperations.map((op) => {
                    const isSelected = selectedOpenAPIOp?.operation_id === op.operation_id;
                    const isRegistered = openapiRegistered[op.operation_id];
                    let methodBadge: BadgeVariant = 'info';
                    if (op.method === 'POST') methodBadge = 'success';
                    if (op.method === 'DELETE') methodBadge = 'danger';
                    if (op.method === 'PUT') methodBadge = 'warning';

                    return (
                      <div
                        key={op.operation_id}
                        onClick={() => {
                          setSelectedOpenAPIOp(op);
                          if (op.operation_id === 'getPetById' || op.operation_id === 'deletePet') {
                            setOpenapiInputsText('{\n  "petId": "123"\n}');
                          } else if (op.operation_id === 'addPet') {
                            setOpenapiInputsText('{\n  "body": {\n    "name": "Bruno",\n    "tag": "guard-dog"\n  }\n}');
                          } else if (op.operation_id === 'getCurrentWeather') {
                            setOpenapiInputsText('{\n  "city": "Bengaluru"\n}');
                          }
                        }}
                        style={{
                          padding: 12,
                          borderRadius: 'var(--radius-sm)',
                          border: isSelected ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                          backgroundColor: isSelected ? 'var(--accent-subtle)' : 'var(--bg-canvas)',
                          cursor: 'pointer',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 6,
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <Badge variant={methodBadge}>{op.method}</Badge>
                            <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
                              {op.operation_id}
                            </span>
                          </div>
                          {isRegistered && <Badge variant="success"><Icon name="check" /> {t('ui.inPalette')}</Badge>}
                        </div>
                        <div style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>
                          {op.path}
                        </div>
                        <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                          {op.summary}
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 4 }}>
                          <Badge variant="neutral">{op.effect_policy}</Badge>
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={isRegistered}
                            onClick={(e) => {
                              e.stopPropagation();
                              registerOpenAPIOp(op.operation_id);
                            }}
                          >
                            {isRegistered ? t('ui.registered') : t('ui.addToPalette')}
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Right: Test Run Console */}
            <div
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--border-subtle)',
                padding: 16,
                display: 'flex',
                flexDirection: 'column',
                gap: 12,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontWeight: 600, fontSize: 14 }}>
                  {t('ui.operationDryRun') + " "}{selectedOpenAPIOp ? selectedOpenAPIOp.operation_id : t('ui.selectAnEndpoint')}
                </span>
                <Badge variant="warning">{t('ui.fixtureDryRunLocalMockTransport')}</Badge>
              </div>

              {selectedOpenAPIOp ? (
                <>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <label style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 500 }}>
                      {t('ui.inputsPathQueryHeadersBodyJson')}
                    </label>
                    <textarea
                      value={openapiInputsText}
                      onChange={(e) => setOpenapiInputsText(e.target.value)}
                      rows={6}
                      aria-label={t('ui.openapiInputs')}
                      style={{
                        padding: 10,
                        fontFamily: 'var(--font-mono)',
                        fontSize: 12,
                        borderRadius: 'var(--radius-sm)',
                        border: '1px solid var(--border-subtle)',
                        backgroundColor: 'var(--bg-canvas)',
                        color: 'var(--text-primary)',
                      }}
                    />
                  </div>

                  <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                    <Button
                      variant="primary"
                      size="md"
                      disabled={openapiRunning}
                      onClick={() => testRunOpenAPI(selectedOpenAPIOp.operation_id, openapiInputsText)}
                    >
                      {openapiRunning ? <Spinner size={14} /> : t('ui.executeDryRun')}
                    </Button>
                  </div>

                  {/* Output */}
                  {openapiRunResult && (
                    <div
                      style={{
                        marginTop: 10,
                        padding: 12,
                        backgroundColor: 'var(--bg-canvas)',
                        borderRadius: 'var(--radius-sm)',
                        border: '1px solid var(--border-subtle)',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 8,
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span style={{ fontSize: 12, fontWeight: 600 }}>{t('ui.executionOutput')}</span>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <Badge variant={openapiRunResult.status_code < 400 ? 'success' : 'danger'}>
                            {openapiRunResult.status_code}
                          </Badge>
                          <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                            {openapiRunResult.duration_ms} ms
                          </span>
                        </div>
                      </div>
                      <div style={{ fontSize: 11, color: 'var(--text-secondary)', fontStyle: 'italic' }}>
                        {t('ui.label') + " "}{openapiRunResult.execution_label}
                      </div>
                      <pre
                        style={{
                          margin: 0,
                          padding: 8,
                          backgroundColor: 'var(--bg-surface)',
                          borderRadius: 'var(--radius-sm)',
                          fontSize: 12,
                          fontFamily: 'var(--font-mono)',
                          overflowX: 'auto',
                        }}
                      >
                        {JSON.stringify(openapiRunResult.body, null, 2)}
                      </pre>
                    </div>
                  )}
                </>
              ) : (
                <div style={{ color: 'var(--text-secondary)', fontSize: 13, padding: '36px 0', textAlign: 'center' }}>
                  {t('ui.selectAnEndpointOnTheLeftToConfigureInputsAndExecuteATestRun')}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
