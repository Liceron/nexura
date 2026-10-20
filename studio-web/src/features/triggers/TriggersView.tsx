// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { InlineConfirm } from '../../components/InlineConfirm';
import { Icon } from '../../components/Icon';
import { useState } from 'react';
import { useTriggers } from './useTriggers';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Input } from '../../components/Input';
import { Alert } from '../../components/Alert';
import { Spinner } from '../../components/Spinner';
import { Card } from '../../components/Card';
import { Modal } from '../../components/Modal';
import { useI18n } from '../../i18n';
import { runHref } from '../../app/routes';
import { WebhookSecretPanel, WebhookSigningSnippets, WorkflowPicker } from './WebhookSecretPanel';

interface TriggersViewProps {
  /** B39: workflow preselected by `#/triggers/{workflowId}` (the workflow page's Triggers entry). */
  initialWorkflowId?: string | null;
}

export function TriggersView({ initialWorkflowId = null }: TriggersViewProps = {}) {
  const { t, locale, language } = useI18n();
  const {
    schedules,
    webhooks,
    jobs,
    outbox,
    outboxGuarantee,
    loading,
    actionLoading,
    error,
    formError,
    setFormError,
    newScheduleId,
    setNewScheduleId,
    newScheduleWfId,
    setNewScheduleWfId,
    newScheduleType,
    setNewScheduleType,
    newIntervalSeconds,
    setNewIntervalSeconds,
    newCronExpr,
    setNewCronExpr,
    newTimezone,
    setNewTimezone,
    newMissedPolicy,
    setNewMissedPolicy,
    handleCreateSchedule,
    handlePauseSchedule,
    handleResumeSchedule,
    handleDeleteSchedule,
    newEndpointId,
    setNewEndpointId,
    newWebhookWfId,
    setNewWebhookWfId,
    newReplayWindow,
    setNewReplayWindow,
    testEndpointId,
    setTestEndpointId,
    testEventId,
    setTestEventId,
    testPayloadJson,
    setTestPayloadJson,
    webhookTestResult,
    handleRegisterWebhook,
    handleDeleteWebhook,
    handleTestWebhook,
    registeredWebhook,
    dismissRegisteredWebhook,
    deliveryUrlFor,
    role,
    canEdit,
    workflowOptions,
    workflowsLoading,
    workflowsError,
    outboxChannel,
    setOutboxChannel,
    outboxRecipient,
    setOutboxRecipient,
    outboxSubject,
    setOutboxSubject,
    outboxBody,
    setOutboxBody,
    outboxDedupKey,
    setOutboxDedupKey,
    outboxFilter,
    setOutboxFilter,
    lastDispatchResult,
    handleTestDispatchOutbox,
    refreshAll,
  } = useTriggers(initialWorkflowId);

  const [activeTab, setActiveTab] = useState<'schedules' | 'webhooks' | 'outbox'>('schedules');
  const [scheduleModalOpen, setScheduleModalOpen] = useState(false);
  const [webhookModalOpen, setWebhookModalOpen] = useState(false);

  // Opened from a workflow page: list only that workflow's triggers (link back to all).
  const visibleSchedules = initialWorkflowId ? schedules.filter((s) => s.workflow_id === initialWorkflowId) : schedules;
  const visibleWebhooks = initialWorkflowId ? webhooks.filter((w) => w.workflow_id === initialWorkflowId) : webhooks;
  const noWorkflowToPick = workflowsLoading || workflowOptions.length === 0;
  const viewerTitle = canEdit ? undefined : t('triggers.viewerNote');

  return (
    <div
      data-testid="triggers-view"
      style={{
        padding: 24,
        maxWidth: 1200,
        margin: '0 auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 20,
      }}
    >
      {/* Top Header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          borderBottom: '1px solid var(--border-subtle)',
          paddingBottom: 16,
        }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: 'var(--text-primary)' }}>
            {t('triggers.title') || t('ui.triggersSchedulesDryRunOutbox')}
          </h1>
          <p style={{ margin: '4px 0 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('triggers.subtitle') ||
              t('ui.manageCronAndIntervalSchedulesAcrossIanaTimezonesTestSignedWebhookIngressAndO')}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" size="sm" onClick={refreshAll} disabled={loading || actionLoading}>
            {loading ? <Spinner size={14} /> : <><Icon name="refresh" /> {t('ui.refresh')}</>}
          </Button>
        </div>
      </div>

      {error && (
        <Alert variant="danger" title={t('ui.triggerSystemError')}>
          {error}
        </Alert>
      )}

      {initialWorkflowId && (
        <div
          data-testid="triggers-workflow-filter"
          style={{ display: 'flex', gap: 10, alignItems: 'center', fontSize: 13, color: 'var(--text-secondary)' }}
        >
          <span>{t('triggers.workflowFilter').replace('{id}', initialWorkflowId)}</span>
          <a href="#/triggers" data-testid="triggers-show-all">
            {t('triggers.showAll')}
          </a>
        </div>
      )}

      {role !== null && !canEdit && (
        <p data-testid="triggers-viewer-note" style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>
          {t('triggers.viewerNote')}
        </p>
      )}

      {registeredWebhook && (
        <WebhookSecretPanel registered={registeredWebhook} onDismiss={dismissRegisteredWebhook} />
      )}

      {/* Tabs */}
      <div role="tablist" style={{ display: 'flex', gap: 12, borderBottom: '1px solid var(--border-subtle)', paddingBottom: 4 }}>
        <button
          data-testid="tab-schedules"
          role="tab"
          aria-selected={activeTab === 'schedules'}
          onClick={() => setActiveTab('schedules')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'schedules' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'schedules' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'schedules' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
          }}
        >
          <Icon name="triggers" /> {t('ui.schedules')}{visibleSchedules.length})
        </button>
        <button
          data-testid="tab-webhooks"
          role="tab"
          aria-selected={activeTab === 'webhooks'}
          onClick={() => setActiveTab('webhooks')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'webhooks' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'webhooks' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'webhooks' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
          }}
        >
          <Icon name="hook" /> {t('ui.signedWebhooks')}{visibleWebhooks.length})
        </button>
        <button
          data-testid="tab-outbox"
          role="tab"
          aria-selected={activeTab === 'outbox'}
          onClick={() => setActiveTab('outbox')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'outbox' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'outbox' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'outbox' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
          }}
        >
          <Icon name="send" /> {t('ui.dryRunOutbox')}{outbox.length})
        </button>
      </div>

      {/* TAB 1: Schedules */}
      {activeTab === 'schedules' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>
              {t('ui.activeSchedulesCronTimers')}
            </h2>
            <Button
              variant="primary"
              size="sm"
              data-testid="btn-open-create-schedule"
              disabled={!canEdit}
              title={viewerTitle}
              onClick={() => {
                setFormError(null);
                setScheduleModalOpen(true);
              }}
            >
              {t('ui.createSchedule')}
            </Button>
          </div>

          {visibleSchedules.length === 0 ? (
            <Card style={{ padding: 32, textAlign: 'center', color: 'var(--text-secondary)' }}>
              {t('ui.noSchedulesRegisteredYetClickQuotCreateScheduleQuotToSetUpAnIntervalOrCron')}
            </Card>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {visibleSchedules.map((s) => {
                const isPaused = Boolean(s.paused ?? ((s as any).is_active === false));
                return (
                  <Card
                    key={s.schedule_id}
                    data-testid={`schedule-item-${s.schedule_id}`}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: 16,
                    }}
                  >
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <span style={{ fontWeight: 700, fontSize: 15, color: 'var(--text-primary)' }}>
                          {s.schedule_id}
                        </span>
                        <Badge variant={isPaused ? 'warning' : 'success'}>
                          {isPaused ? t('ui.paused') : t('ui.active2')}
                        </Badge>
                        <Badge variant="neutral">{t('ui.timezonePrefix')} {s.timezone}</Badge>
                      </div>
                      <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                        {t('ui.workflow') + " "}<code style={{ color: 'var(--accent-primary)' }}>{s.workflow_id}</code> {t('ui.pattern')}{' '}
                        <strong>{s.cron ? t('triggers.cronPattern').replace('{value}', String(s.cron)) : t('triggers.intervalPattern').replace('{value}', String(s.interval_seconds))}</strong> {t('ui.policy2')}{' '}
                        <code>{s.missed_run_policy}</code>
                      </div>
                      {s.next_run_at && (
                        <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                          {t('ui.nextFire') + " "}{new Date(s.next_run_at * 1000).toLocaleString(locale)}
                        </div>
                      )}
                    </div>

                    <div style={{ display: 'flex', gap: 8 }}>
                      {isPaused ? (
                        <Button
                          variant="secondary"
                          size="sm"
                          data-testid={`btn-resume-schedule-${s.schedule_id}`}
                          onClick={() => handleResumeSchedule(s.schedule_id)}
                          disabled={actionLoading || !canEdit}
                        >
                          <Icon name="play" /> {t('ui.resume')}
                        </Button>
                      ) : (
                        <Button
                          variant="secondary"
                          size="sm"
                          data-testid={`btn-pause-schedule-${s.schedule_id}`}
                          onClick={() => handlePauseSchedule(s.schedule_id)}
                          disabled={actionLoading || !canEdit}
                        >
                          <Icon name="pause" /> {t('ui.pause')}
                        </Button>
                      )}
                    <InlineConfirm key={s.schedule_id} consequence={t('confirmation.deleteSchedule')} >
                      <Button
                        variant="danger"
                        size="sm"
                        data-testid={`btn-delete-schedule-${s.schedule_id}`}
                        onClick={() => handleDeleteSchedule(s.schedule_id)}
                        disabled={actionLoading || !canEdit}
                      >
                        {t('ui.delete')}
                      </Button>
                    </InlineConfirm>
                    </div>
                  </Card>
              );
            })}
            </div>
          )}

          {/* Pending Durable Trigger Jobs list */}
          {jobs.length > 0 && (
            <Card style={{ marginTop: 12 }}>
              <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 10, color: 'var(--text-primary)' }}>
                {t('ui.durableTriggerJobs')}{jobs.length})
              </h3>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border-subtle)', textAlign: 'left', color: 'var(--text-secondary)' }}>
                    <th style={{ padding: '8px 12px' }}>{t('ui.jobId')}</th>
                    <th style={{ padding: '8px 12px' }}>{t('ui.sourceType')}</th>
                    <th style={{ padding: '8px 12px' }}>{t('ui.sourceId')}</th>
                    <th style={{ padding: '8px 12px' }}>{t('ui.workflow2')}</th>
                    <th style={{ padding: '8px 12px' }}>{t('ui.status')}</th>
                    <th style={{ padding: '8px 12px' }}>{t('runs.colRun')}</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.job_id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                      <td style={{ padding: '8px 12px', fontWeight: 600 }}>{j.job_id}</td>
                      <td style={{ padding: '8px 12px' }}>{j.trigger_type}</td>
                      <td style={{ padding: '8px 12px' }}>{j.trigger_source_id}</td>
                      <td style={{ padding: '8px 12px' }}>{j.workflow_id}</td>
                      <td style={{ padding: '8px 12px' }}>
                        <Badge variant={j.status === 'pending' ? 'info' : 'success'}>{t(`statusLabels.${j.status}`, j.status)}</Badge>
                      </td>
                      <td style={{ padding: '8px 12px' }} data-testid={`trigger-job-run-${j.job_id}`}>
                        {j.run_id ? (
                          <>
                            <code>{j.run_id}</code>{' '}
                            <a href={runHref(j.run_id)} data-testid="trigger-job-open-run">
                              {t('runs.open')}
                            </a>
                          </>
                        ) : (
                          <span style={{ color: 'var(--text-muted)' }}>
                            {/* A failed or finished job without a run will never start one. */}
                            {j.status === 'failed' || j.status === 'completed' ? t('runs.noRun') : t('runs.noRunYet')}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </div>
      )}

      {/* TAB 2: Signed Webhooks */}
      {activeTab === 'webhooks' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>
              {t('ui.registeredWebhookEndpoints')}
            </h2>
            <Button
              variant="primary"
              size="sm"
              data-testid="btn-open-register-webhook"
              disabled={!canEdit}
              title={viewerTitle}
              onClick={() => {
                setFormError(null);
                setWebhookModalOpen(true);
              }}
            >
              {t('ui.registerEndpoint')}
            </Button>
          </div>

          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {/* Endpoints List */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {visibleWebhooks.length === 0 ? (
                <Card style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)' }}>
                  {t('ui.noWebhookEndpointsRegistered')}
                </Card>
              ) : (
                visibleWebhooks.map((w) => (
                  <Card
                    key={w.endpoint_id}
                    data-testid={`webhook-item-${w.endpoint_id}`}
                    style={{
                      border: testEndpointId === w.endpoint_id ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                      cursor: 'pointer',
                    }}
                    onClick={() => setTestEndpointId(w.endpoint_id)}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
                      <div>
                        <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)' }}>
                          {w.endpoint_id}
                        </span>
                        <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>
                          {t('ui.workflow') + " "}<code>{w.workflow_id}</code>
                        </div>
                      </div>
                      <InlineConfirm key={w.endpoint_id} consequence={t('confirmation.deleteWebhook')} >
                        <Button
                          variant="danger"
                          size="sm"
                          disabled={actionLoading || !canEdit}
                          title={viewerTitle}
                          data-testid={`btn-delete-webhook-${w.endpoint_id}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteWebhook(w.endpoint_id);
                          }}
                        >
                          {t('ui.delete')}
                        </Button>
                      </InlineConfirm>
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      {t('ui.replayWindow') + " "}{w.replay_window_seconds}{t('ui.sSecret') + " "}<code>●●●●●●●●</code>
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4, wordBreak: 'break-all' }}>
                      {t('triggers.deliveryUrl')}:{' '}
                      <code data-testid={`webhook-delivery-url-${w.endpoint_id}`}>{deliveryUrlFor(w.endpoint_id)}</code>
                    </div>
                  </Card>
                ))
              )}

              {testEndpointId && visibleWebhooks.some((w) => w.endpoint_id === testEndpointId) && (
                <Card data-testid="webhook-signing-help">
                  <details>
                    <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>
                      {t('triggers.signingHelp').replace('{id}', testEndpointId)}
                    </summary>
                    <div style={{ marginTop: 10 }}>
                      <WebhookSigningSnippets deliveryUrl={deliveryUrlFor(testEndpointId)} testIdPrefix="webhook-help" />
                    </div>
                  </details>
                </Card>
              )}
            </div>

            {/* Interactive Webhook Simulator */}
            <Card style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>{t('ui.testWebhookDeliverySimulator')}</h3>
              <p style={{ margin: 0, fontSize: 12, color: 'var(--text-secondary)' }}>
                {t('ui.simulateHmacSha256SignatureVerificationReplayProtectionAndDeduplication')}
              </p>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.targetEndpoint')}
                </label>
                <select
                  data-testid="select-test-webhook"
                  value={testEndpointId}
                  onChange={(e) => setTestEndpointId(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-surface)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                >
                  <option value="">{t('ui.selectEndpoint')}</option>
                  {visibleWebhooks.map((w) => (
                    <option key={w.endpoint_id} value={w.endpoint_id}>
                      {w.endpoint_id} ({w.workflow_id})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.eventIdDedupKey')}
                </label>
                <Input
                  data-testid="input-webhook-event-id"
                  value={testEventId}
                  onChange={(e) => setTestEventId(e.target.value)}
                  placeholder={t('ui.leaveEmptyForAutoGeneratedUuid')}
                />
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.jsonPayload')}
                </label>
                <textarea
                  data-testid="textarea-webhook-payload"
                  value={testPayloadJson}
                  onChange={(e) => setTestPayloadJson(e.target.value)}
                  rows={4}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-surface-elevated)',
                    color: 'var(--text-primary)',
                    fontFamily: 'monospace',
                    fontSize: 12,
                  }}
                />
              </div>

              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Button
                  variant="primary"
                  size="sm"
                  data-testid="btn-test-webhook-valid"
                  onClick={() => handleTestWebhook()}
                  disabled={actionLoading || !testEndpointId}
                >
                  {actionLoading ? <Spinner size={12} /> : <><Icon name="check" /> {t('ui.deliverValidEvent')}</>}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="btn-test-webhook-replay"
                  onClick={() => handleTestWebhook({ useExistingEventId: true })}
                  disabled={actionLoading || !testEndpointId || !testEventId}
                >
                  <Icon name="refresh" /> {t('ui.replayExactEvent')}
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  data-testid="btn-test-webhook-bad-sig"
                  onClick={() => handleTestWebhook({ invalid_signature: true })}
                  disabled={actionLoading || !testEndpointId}
                >
                  <Icon name="close" /> {t('ui.testBadSignature')}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="btn-test-webhook-expired"
                  onClick={() => handleTestWebhook({ expired_timestamp: true })}
                  disabled={actionLoading || !testEndpointId}
                >
                  <Icon name="triggers" /> {t('ui.testExpiredWindow')}
                </Button>
              </div>

              {webhookTestResult && (
                <div
                  data-testid="webhook-test-result"
                  style={{
                    padding: 12,
                    borderRadius: 'var(--radius-md)',
                    backgroundColor: 'var(--bg-surface-elevated)',
                    border: '1px solid var(--border-subtle)',
                    fontSize: 12,
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                    <span style={{ fontWeight: 600 }}>{t('ui.deliveryStatus')}</span>
                    <Badge variant={webhookTestResult.is_duplicate ? 'warning' : 'success'}>
                      {webhookTestResult.is_duplicate ? t('ui.duplicateIngestedOnce') : t('ui.delivered')}
                    </Badge>
                  </div>
                  <div>{t('ui.jobId2') + " "}<code>{webhookTestResult.job_id}</code></div>
                  <div>{t('ui.duplicateFlag') + " "}<code>{String(webhookTestResult.is_duplicate)}</code></div>
                </div>
              )}
            </Card>
          </div>
        </div>
      )}

      {/* TAB 3: Observable Dry-Run Outbox */}
      {activeTab === 'outbox' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          {/* Strict Dry Run Guarantee Banner */}
          <Alert variant="info" title={t('ui.zeroExternalDispatchGuarantee')}>
            {(language === 'en' && outboxGuarantee) ||
              t('ui.dryRunOnlyAllOutboundMessagesAreCapturedInOutboxAndNeverTransmittedToRealCa')}
          </Alert>

          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 16 }}>
            {/* Dispatch Simulator Form */}
            <Card style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>{t('ui.dispatchSimulatedAction')}</h3>
              <p style={{ margin: 0, fontSize: 12, color: 'var(--text-secondary)' }}>
                {t('ui.testEmailOrWhatsappChannelTemplatesWithDeduplicationGuarantees')}
              </p>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.channel')}
                </label>
                <div style={{ display: 'flex', gap: 12 }}>
                  <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <input
                      type="radio"
                      name="outbox_ch"
                      value="email"
                      checked={outboxChannel === 'email'}
                      onChange={() => {
                        setOutboxChannel('email');
                        setOutboxRecipient('customer@example.com');
                      }}
                    />
                    {t('ui.email')}
                  </label>
                  <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <input
                      type="radio"
                      name="outbox_ch"
                      value="whatsapp"
                      checked={outboxChannel === 'whatsapp'}
                      onChange={() => {
                        setOutboxChannel('whatsapp');
                        setOutboxRecipient('+919876543210');
                      }}
                    />
                    {t('ui.whatsapp')}
                  </label>
                </div>
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.recipient')}
                </label>
                <Input
                  data-testid="input-outbox-recipient"
                  value={outboxRecipient}
                  onChange={(e) => setOutboxRecipient(e.target.value)}
                  placeholder={t('ui.eGUserExampleComOr91')}
                />
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.subjectTemplateName')}
                </label>
                <Input
                  data-testid="input-outbox-subject"
                  value={outboxSubject}
                  onChange={(e) => setOutboxSubject(e.target.value)}
                  placeholder={t('ui.orderNotification')}
                />
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.bodyText')}
                </label>
                <textarea
                  data-testid="textarea-outbox-body"
                  value={outboxBody}
                  onChange={(e) => setOutboxBody(e.target.value)}
                  rows={3}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-surface-elevated)',
                    color: 'var(--text-primary)',
                    fontSize: 12,
                  }}
                />
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                  {t('ui.deduplicationKey')}
                </label>
                <Input
                  data-testid="input-outbox-dedup"
                  value={outboxDedupKey}
                  onChange={(e) => setOutboxDedupKey(e.target.value)}
                  placeholder="dedup_order_123"
                />
              </div>

              <Button
                variant="primary"
                data-testid="btn-dispatch-outbox"
                onClick={handleTestDispatchOutbox}
                disabled={actionLoading}
              >
                {actionLoading ? <Spinner size={12} /> : <><Icon name="onboarding" /> {t('ui.dispatchToDryRunOutbox')}</>}
              </Button>

              {lastDispatchResult && (
                <div
                  data-testid="dispatch-result-box"
                  style={{
                    padding: 10,
                    borderRadius: 'var(--radius-md)',
                    backgroundColor: 'var(--bg-surface-elevated)',
                    border: '1px solid var(--border-subtle)',
                    fontSize: 11,
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                    <span>{t('ui.receipt') + " "}<code>{lastDispatchResult.receipt.receipt_id}</code></span>
                    <Badge variant={lastDispatchResult.is_duplicate ? 'warning' : 'success'}>
                      {t(`statusLabels.${lastDispatchResult.receipt.status}`, lastDispatchResult.receipt.status)}
                    </Badge>
                  </div>
                  <div>{t('ui.duplicateFlag') + " "}<code>{String(lastDispatchResult.is_duplicate)}</code></div>
                </div>
              )}
            </Card>

            {/* Outbox Receipts List */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>
                  {t('ui.capturedMessages')}{outbox.length})
                </h3>
                <div style={{ display: 'flex', gap: 6 }}>
                  {(['all', 'email', 'whatsapp'] as const).map((f) => (
                    <Button
                      key={f}
                      variant={outboxFilter === f ? 'primary' : 'secondary'}
                      size="sm"
                      onClick={() => setOutboxFilter(f)}
                    >
                      {t(`channelUpper.${f}`)}
                    </Button>
                  ))}
                </div>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxHeight: 520, overflowY: 'auto' }}>
                {outbox.length === 0 ? (
                  <Card style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)' }}>
                    {t('ui.noMessagesCapturedInOutboxYet')}
                  </Card>
                ) : (
                  outbox.map((msg) => (
                    <Card key={msg.receipt_id} data-testid={`outbox-item-${msg.receipt_id}`} style={{ padding: 12 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <Badge variant="neutral">{t(`channelUpper.${msg.channel}`, msg.channel.toUpperCase())}</Badge>
                          <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
                            {msg.recipient}
                          </span>
                        </div>
                        <Badge variant={msg.status === 'deduplicated' ? 'warning' : 'success'}>
                          {t(`statusLabels.${msg.status}`, msg.status)}
                        </Badge>
                      </div>
                      <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 6 }}>
                        {msg.rendered_content.subject_or_template && (
                          <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                            {msg.rendered_content.subject_or_template}
                          </div>
                        )}
                        <div>{msg.rendered_content.body}</div>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-muted)' }}>
                        <span>ID: {msg.receipt_id}</span>
                        <span>{new Date(msg.created_at * 1000).toLocaleTimeString(locale)}</span>
                      </div>
                    </Card>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Modal: Create Schedule */}
      <Modal
        open={scheduleModalOpen}
        onClose={() => setScheduleModalOpen(false)}
        title={t('ui.createScheduleTrigger')}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {formError && (
            <Alert variant="danger" title={t('ui.validationError')}>
              {formError}
            </Alert>
          )}

          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
              {t('ui.scheduleId')}
            </label>
            <Input
              data-testid="input-new-schedule-id"
              value={newScheduleId}
              onChange={(e) => setNewScheduleId(e.target.value)}
              placeholder={t('ui.eGSchedOrderDaily')}
            />
          </div>

          <WorkflowPicker
            testId="select-new-schedule-wfid"
            value={newScheduleWfId}
            onChange={setNewScheduleWfId}
            options={workflowOptions}
            loading={workflowsLoading}
            error={workflowsError}
          />

          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                {t('ui.scheduleType')}
              </label>
              <select
                value={newScheduleType}
                onChange={(e) => setNewScheduleType(e.target.value as any)}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border-subtle)',
                  backgroundColor: 'var(--bg-surface)',
                  color: 'var(--text-primary)',
                  fontSize: 13,
                }}
              >
                <option value="interval">{t('ui.intervalSeconds')}</option>
                <option value="cron">{t('ui.cronExpression')}</option>
              </select>
            </div>

            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                {newScheduleType === 'interval' ? t('ui.intervalSeconds') : t('ui.cronExpression')}
              </label>
              {newScheduleType === 'interval' ? (
                <Input
                  type="number"
                  data-testid="input-schedule-interval"
                  value={String(newIntervalSeconds)}
                  onChange={(e) => setNewIntervalSeconds(Number(e.target.value))}
                  placeholder="60"
                />
              ) : (
                <Input
                  data-testid="input-schedule-cron"
                  value={newCronExpr}
                  onChange={(e) => setNewCronExpr(e.target.value)}
                  placeholder="0 * * * *"
                />
              )}
            </div>
          </div>

          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                {t('ui.ianaTimezone')}
              </label>
              <Input
                data-testid="input-schedule-timezone"
                value={newTimezone}
                onChange={(e) => setNewTimezone(e.target.value)}
                placeholder="Asia/Kolkata"
              />
              <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
                {t('ui.examplePrefix')} <code>Asia/Kolkata, America/New_York, UTC</code>
              </span>
            </div>

            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                {t('ui.missedRunPolicy')}
              </label>
              <select
                value={newMissedPolicy}
                onChange={(e) => setNewMissedPolicy(e.target.value as any)}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border-subtle)',
                  backgroundColor: 'var(--bg-surface)',
                  color: 'var(--text-primary)',
                  fontSize: 13,
                }}
              >
                <option value="fire_once">{t('ui.fireOnceEnqueue1Job')}</option>
                <option value="skip">{t('ui.skipDiscardMissed')}</option>
                <option value="burst">{t('ui.burstEnqueueAll')}</option>
              </select>
            </div>
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 10 }}>
            <Button variant="secondary" onClick={() => setScheduleModalOpen(false)}>
              {t('ui.cancel')}
            </Button>
            <Button
              variant="primary"
              data-testid="btn-submit-create-schedule"
              disabled={actionLoading || noWorkflowToPick || !canEdit}
              onClick={async () => {
                const ok = await handleCreateSchedule();
                if (ok) setScheduleModalOpen(false);
              }}
            >
              {actionLoading ? <Spinner size={14} /> : t('ui.saveSchedule')}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Modal: Register Webhook */}
      <Modal
        open={webhookModalOpen}
        onClose={() => setWebhookModalOpen(false)}
        title={t('ui.registerWebhookEndpoint')}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {formError && (
            <Alert variant="danger" title={t('ui.validationError')}>
              {formError}
            </Alert>
          )}

          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
              {t('ui.endpointId')}
            </label>
            <Input
              data-testid="input-new-webhook-id"
              value={newEndpointId}
              onChange={(e) => setNewEndpointId(e.target.value)}
              placeholder={t('ui.eGOrdersIngress')}
            />
          </div>

          <WorkflowPicker
            testId="select-new-webhook-wfid"
            value={newWebhookWfId}
            onChange={setNewWebhookWfId}
            options={workflowOptions}
            loading={workflowsLoading}
            error={workflowsError}
          />

          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
              {t('ui.replayWindowSeconds')}
            </label>
            <Input
              type="number"
              value={String(newReplayWindow)}
              onChange={(e) => setNewReplayWindow(Number(e.target.value))}
              placeholder="300"
            />
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 10 }}>
            <Button variant="secondary" onClick={() => setWebhookModalOpen(false)}>
              {t('ui.cancel')}
            </Button>
            <Button
              variant="primary"
              data-testid="btn-submit-register-webhook"
              disabled={actionLoading || noWorkflowToPick || !canEdit}
              onClick={async () => {
                const ok = await handleRegisterWebhook();
                if (ok) setWebhookModalOpen(false);
              }}
            >
              {actionLoading ? <Spinner size={14} /> : t('ui.registerWebhook')}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
