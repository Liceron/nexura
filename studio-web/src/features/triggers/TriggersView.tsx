// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { InlineConfirm } from '../../components/InlineConfirm';
import { useEffect, useId, useState } from 'react';
import { useTriggers } from './useTriggers';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { WorkflowTabs } from '../../components/WorkflowTabs';
import { useI18n } from '../../i18n';
import { parseHash, runHref } from '../../app/routes';
import { setNavigationGuard } from '../../app/navigationGuard';
import { IssuedDeliveryTokenPanel, WebhookSecretPanel, WebhookSigningSnippets, WorkflowPicker } from './WebhookSecretPanel';

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
    issuedDeliveryToken,
    dismissIssuedDeliveryToken,
    handleRotateDeliveryToken,
    rotatingEndpointId,
    rotateError,
    deliveryTokenMode,
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

  const ids = useId();
  const panelId = `${ids}-panel`;
  // A one-time signing secret or delivery token on screen cannot be shown again: ask before the
  // operator leaves Triggers (reload/close, or another Studio page) (review minor 10).
  const holdsOneTimeValue = Boolean(registeredWebhook || issuedDeliveryToken);
  useEffect(() => {
    if (!holdsOneTimeValue) return;
    const message = t('triggers.leaveWarning');
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = message;
      return message;
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    setNavigationGuard((nextHash) => parseHash(nextHash).tab === 'triggers' || window.confirm(message));
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
      setNavigationGuard(null);
    };
  }, [holdsOneTimeValue, t]);
  const tabs = [
    { id: 'schedules', label: `${t('ui.schedules')}${visibleSchedules.length})` },
    { id: 'webhooks', label: `${t('ui.signedWebhooks')}${visibleWebhooks.length})` },
    { id: 'outbox', label: `${t('ui.dryRunOutbox')}${outbox.length})` },
  ];
  // The one-time secret panel owns the page's primary action until it is dismissed.
  const headerVariant = registeredWebhook || issuedDeliveryToken ? 'secondary' : 'primary';
  const headerAction =
    activeTab === 'schedules' ? (
      <Button
        variant={headerVariant}
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
    ) : activeTab === 'webhooks' ? (
      <Button
        variant={headerVariant}
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
    ) : null;
  const status = (variant: 'success' | 'warning' | 'danger' | 'neutral', text: string, testId?: string) => (
    <span className={`run-status run-status--${variant}`} data-testid={testId}>
      <span aria-hidden="true">●</span> {text}
    </span>
  );

  return (
    <PageLayout data-testid="triggers-view">
      <PageHeader
        title={t('triggers.title')}
        description={t('triggers.subtitle')}
        actions={
          <>
            <Button variant="ghost" onClick={refreshAll} loading={loading} disabled={actionLoading}>
              {t('ui.refresh')}
            </Button>
            {headerAction}
          </>
        }
      />

      <div className="resource-notices">
        {error && (
          <div role="alert" className="inline-notice inline-notice--danger">
            <span>{error}</span>
          </div>
        )}
        {initialWorkflowId && (
          <p data-testid="triggers-workflow-filter" className="inline-notice">
            {t('triggers.workflowFilter').replace('{id}', initialWorkflowId)}{' '}
            <a href="#/triggers" data-testid="triggers-show-all">
              {t('triggers.showAll')}
            </a>
          </p>
        )}
        {role !== null && !canEdit && (
          <p data-testid="triggers-viewer-note" className="inline-notice">
            {t('triggers.viewerNote')}
          </p>
        )}
        {registeredWebhook && <WebhookSecretPanel registered={registeredWebhook} onDismiss={dismissRegisteredWebhook} />}
        {issuedDeliveryToken && <IssuedDeliveryTokenPanel issued={issuedDeliveryToken} onDismiss={dismissIssuedDeliveryToken} />}
      </div>

      <WorkflowTabs tabs={tabs} active={activeTab} onChange={(id) => setActiveTab(id as typeof activeTab)} label={t('triggers.tabsLabel')} panelId={panelId} />

      <div role="tabpanel" id={panelId} aria-label={tabs.find((tab) => tab.id === activeTab)?.label} tabIndex={0} style={{ minWidth: 0 }}>
        {activeTab === 'schedules' && (
          <>
            {visibleSchedules.length === 0 ? (
              <p className="inspection-empty">{t('ui.noSchedulesRegisteredYetClickQuotCreateScheduleQuotToSetUpAnIntervalOrCron')}</p>
            ) : (
              <div className="inspection-table-scroll" role="region" aria-label={t('ui.activeSchedulesCronTimers')} tabIndex={0}>
                <table className="inspection-table settings-table settings-table--stack" role="table">
                  <caption className="studio-sr-only">{t('ui.activeSchedulesCronTimers')}</caption>
                  <thead>
                    <tr role="row">
                      <th role="columnheader" scope="col">{t('triggers.colSchedule')}</th>
                      <th role="columnheader" scope="col">{t('ui.workflow2')}</th>
                      <th role="columnheader" scope="col">{t('triggers.colPattern')}</th>
                      <th role="columnheader" scope="col">{t('triggers.colTimezone')}</th>
                      <th role="columnheader" scope="col">{t('ui.missedRunPolicy')}</th>
                      <th role="columnheader" scope="col">{t('ui.status')}</th>
                      <th role="columnheader" scope="col"><span className="studio-sr-only">{t('ui.actions')}</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleSchedules.map((s) => {
                      const isPaused = Boolean(s.paused ?? ((s as any).is_active === false));
                      return (
                        <tr role="row" key={s.schedule_id} data-testid={`schedule-item-${s.schedule_id}`}>
                          <td role="cell" data-label={t('triggers.colSchedule')}><code>{s.schedule_id}</code></td>
                          <td role="cell" data-label={t('ui.workflow2')}><code>{s.workflow_id}</code></td>
                          <td role="cell" data-label={t('triggers.colPattern')}>
                            {s.cron
                              ? t('triggers.cronPattern').replace('{value}', String(s.cron))
                              : t('triggers.intervalPattern').replace('{value}', String(s.interval_seconds))}
                            {s.next_run_at && (
                              <div className="resource-help">
                                {t('ui.nextFire') + ' '}
                                {new Date(s.next_run_at * 1000).toLocaleString(locale)}
                              </div>
                            )}
                          </td>
                          <td role="cell" data-label={t('triggers.colTimezone')} data-testid={`schedule-timezone-${s.schedule_id}`}>{s.timezone}</td>
                          <td role="cell" data-label={t('ui.missedRunPolicy')}><code>{s.missed_run_policy}</code></td>
                          <td role="cell" data-label={t('ui.status')}>{status(isPaused ? 'warning' : 'success', isPaused ? t('ui.paused') : t('ui.active2'), `schedule-status-${s.schedule_id}`)}</td>
                          <td role="cell" data-label={t('ui.actions')}>
                            <div className="settings-row-actions">
                              {isPaused ? (
                                <Button
                                  size="sm"
                                  data-testid={`btn-resume-schedule-${s.schedule_id}`}
                                  aria-label={`${t('ui.resume')} ${s.schedule_id}`}
                                  onClick={() => handleResumeSchedule(s.schedule_id)}
                                  disabled={actionLoading || !canEdit}
                                >
                                  {t('ui.resume')}
                                </Button>
                              ) : (
                                <Button
                                  size="sm"
                                  data-testid={`btn-pause-schedule-${s.schedule_id}`}
                                  aria-label={`${t('ui.pause')} ${s.schedule_id}`}
                                  onClick={() => handlePauseSchedule(s.schedule_id)}
                                  disabled={actionLoading || !canEdit}
                                >
                                  {t('ui.pause')}
                                </Button>
                              )}
                              <InlineConfirm key={s.schedule_id} consequence={t('confirmation.deleteSchedule')}>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  className="workflow-danger-action"
                                  data-testid={`btn-delete-schedule-${s.schedule_id}`}
                                  aria-label={`${t('ui.delete')} ${s.schedule_id}`}
                                  onClick={() => handleDeleteSchedule(s.schedule_id)}
                                  disabled={actionLoading || !canEdit}
                                >
                                  {t('ui.delete')}
                                </Button>
                              </InlineConfirm>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {jobs.length > 0 && (
              <section aria-labelledby={`${ids}-jobs`}>
                <h2 id={`${ids}-jobs`} style={{ margin: '8px 0' }}>
                  {t('ui.durableTriggerJobs')}{jobs.length})
                </h2>
                <div className="inspection-table-scroll" role="region" aria-labelledby={`${ids}-jobs`} tabIndex={0}>
                  <table className="inspection-table settings-table settings-table--stack" role="table">
                    <thead>
                      <tr role="row">
                        <th role="columnheader" scope="col">{t('ui.jobId')}</th>
                        <th role="columnheader" scope="col">{t('ui.sourceType')}</th>
                        <th role="columnheader" scope="col">{t('ui.sourceId')}</th>
                        <th role="columnheader" scope="col">{t('ui.workflow2')}</th>
                        <th role="columnheader" scope="col">{t('ui.status')}</th>
                        <th role="columnheader" scope="col">{t('runs.colRun')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {jobs.map((j) => (
                        <tr role="row" key={j.job_id}>
                          <td role="cell" data-label={t('ui.jobId')}><code>{j.job_id}</code></td>
                          <td role="cell" data-label={t('ui.sourceType')}>{j.trigger_type}</td>
                          <td role="cell" data-label={t('ui.sourceId')}><code>{j.trigger_source_id}</code></td>
                          <td role="cell" data-label={t('ui.workflow2')}><code>{j.workflow_id}</code></td>
                          <td role="cell" data-label={t('ui.status')}>{t(`statusLabels.${j.status}`, j.status)}</td>
                          <td role="cell" data-label={t('runs.colRun')} data-testid={`trigger-job-run-${j.job_id}`}>
                            {j.run_id ? (
                              <>
                                <code>{j.run_id}</code>{' '}
                                <a href={runHref(j.run_id)} data-testid="trigger-job-open-run">
                                  {t('runs.open')}
                                </a>
                              </>
                            ) : (
                              <span className="resource-help">
                                {/* A failed or finished job without a run will never start one. */}
                                {j.status === 'failed' || j.status === 'completed' ? t('runs.noRun') : t('runs.noRunYet')}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
          </>
        )}

        {activeTab === 'webhooks' && (
          <>
            {visibleWebhooks.length === 0 ? (
              <p className="inspection-empty">{t('ui.noWebhookEndpointsRegistered')}</p>
            ) : (
              <div className="inspection-table-scroll" role="region" aria-label={t('ui.registeredWebhookEndpoints')} tabIndex={0}>
                <table className="inspection-table settings-table settings-table--stack" role="table">
                  <caption className="studio-sr-only">{t('ui.registeredWebhookEndpoints')}</caption>
                  <thead>
                    <tr role="row">
                      <th role="columnheader" scope="col">{t('triggers.colEndpoint')}</th>
                      <th role="columnheader" scope="col">{t('ui.workflow2')}</th>
                      <th role="columnheader" scope="col">{t('triggers.deliveryUrl')}</th>
                      <th role="columnheader" scope="col" style={{ textAlign: 'right' }}>{t('ui.replayWindowSeconds')}</th>
                      <th role="columnheader" scope="col"><span className="studio-sr-only">{t('ui.actions')}</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleWebhooks.map((w) => (
                      <tr role="row" key={w.endpoint_id} data-testid={`webhook-item-${w.endpoint_id}`}>
                        <td role="cell" data-label={t('triggers.colEndpoint')}>
                          <button
                            type="button"
                            className="settings-row-select"
                            aria-pressed={testEndpointId === w.endpoint_id}
                            onClick={() => setTestEndpointId(w.endpoint_id)}
                          >
                            <code>{w.endpoint_id}</code>
                          </button>
                        </td>
                        <td role="cell" data-label={t('ui.workflow2')}><code>{w.workflow_id}</code></td>
                        <td role="cell" data-label={t('triggers.deliveryUrl')}>
                          <code data-testid={`webhook-delivery-url-${w.endpoint_id}`} className="settings-cell-code">{deliveryUrlFor(w.endpoint_id)}</code>
                          <div className="resource-help">{t('triggers.secretStored')}</div>
                        </td>
                        <td role="cell" data-label={t('ui.replayWindowSeconds')} style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{w.replay_window_seconds}</td>
                        <td role="cell" data-label={t('ui.actions')}>
                          <div className="settings-row-actions">
                          {deliveryTokenMode && (
                            <InlineConfirm key={`rotate-${w.endpoint_id}`} consequence={t('triggers.rotateConsequence')}>
                              <Button
                                size="sm"
                                loading={rotatingEndpointId === w.endpoint_id}
                                disabled={!canEdit}
                                title={viewerTitle}
                                aria-label={`${t('triggers.rotateToken')} ${w.endpoint_id}`}
                                data-testid={`btn-rotate-delivery-token-${w.endpoint_id}`}
                                onClick={() => handleRotateDeliveryToken(w.endpoint_id)}
                              >
                                {t('triggers.rotateToken')}
                              </Button>
                            </InlineConfirm>
                          )}
                          {rotateError?.endpointId === w.endpoint_id && (
                            <span role="alert" className="resource-error" data-testid={`rotate-error-${w.endpoint_id}`} style={{ flexBasis: '100%' }}>
                              {rotateError.message}
                            </span>
                          )}
                          <InlineConfirm key={w.endpoint_id} consequence={t('confirmation.deleteWebhook')}>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="workflow-danger-action"
                              disabled={actionLoading || !canEdit}
                              title={viewerTitle}
                              aria-label={`${t('ui.delete')} ${w.endpoint_id}`}
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
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {testEndpointId && visibleWebhooks.some((w) => w.endpoint_id === testEndpointId) && (
              <details data-testid="webhook-signing-help" className="resource-help-disclosure" style={{ marginBottom: 16 }}>
                <summary>{t('triggers.signingHelp').replace('{id}', testEndpointId)}</summary>
                <div style={{ marginTop: 10, maxWidth: 800 }}>
                  <WebhookSigningSnippets deliveryUrl={deliveryUrlFor(testEndpointId)} testIdPrefix="webhook-help" />
                </div>
              </details>
            )}

            <section className="settings-panel" aria-labelledby={`${ids}-test`}>
              <h2 id={`${ids}-test`}>{t('ui.testWebhookDeliverySimulator')}</h2>
              <p className="knowledge-panel-help">{t('ui.simulateHmacSha256SignatureVerificationReplayProtectionAndDeduplication')}</p>
              <div className="resource-form" style={{ gap: 16 }}>
                <div className="resource-field">
                  <label htmlFor={`${ids}-test-endpoint`}>{t('ui.targetEndpoint')}</label>
                  <select id={`${ids}-test-endpoint`} data-testid="select-test-webhook" value={testEndpointId} onChange={(e) => setTestEndpointId(e.target.value)}>
                    <option value="">{t('ui.selectEndpoint')}</option>
                    {visibleWebhooks.map((w) => (
                      <option key={w.endpoint_id} value={w.endpoint_id}>
                        {w.endpoint_id} ({w.workflow_id})
                      </option>
                    ))}
                  </select>
                </div>
                <div className="resource-field">
                  <label htmlFor={`${ids}-test-event`}>{t('ui.eventIdDedupKey')}</label>
                  <input
                    id={`${ids}-test-event`}
                    data-testid="input-webhook-event-id"
                    value={testEventId}
                    onChange={(e) => setTestEventId(e.target.value)}
                    placeholder={t('ui.leaveEmptyForAutoGeneratedUuid')}
                  />
                </div>
                <div className="resource-field">
                  <label htmlFor={`${ids}-test-payload`}>{t('ui.jsonPayload')}</label>
                  <textarea
                    id={`${ids}-test-payload`}
                    data-testid="textarea-webhook-payload"
                    value={testPayloadJson}
                    onChange={(e) => setTestPayloadJson(e.target.value)}
                    rows={4}
                    spellCheck={false}
                  />
                </div>
                <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                  <Button
                    data-testid="btn-test-webhook-valid"
                    onClick={() => handleTestWebhook()}
                    loading={actionLoading}
                    disabled={!testEndpointId}
                  >
                    {t('ui.deliverValidEvent')}
                  </Button>
                </div>
                <details className="resource-help-disclosure" data-testid="webhook-more-tests">
                  <summary>{t('triggers.moreTests')}</summary>
                  <div className="resource-actions" style={{ justifyContent: 'flex-start', marginTop: 8 }}>
                    <Button
                      size="sm"
                      data-testid="btn-test-webhook-replay"
                      onClick={() => handleTestWebhook({ useExistingEventId: true })}
                      disabled={actionLoading || !testEndpointId || !testEventId}
                    >
                      {t('ui.replayExactEvent')}
                    </Button>
                    <Button
                      size="sm"
                      data-testid="btn-test-webhook-bad-sig"
                      onClick={() => handleTestWebhook({ invalid_signature: true })}
                      disabled={actionLoading || !testEndpointId}
                    >
                      {t('ui.testBadSignature')}
                    </Button>
                    <Button
                      size="sm"
                      data-testid="btn-test-webhook-expired"
                      onClick={() => handleTestWebhook({ expired_timestamp: true })}
                      disabled={actionLoading || !testEndpointId}
                    >
                      {t('ui.testExpiredWindow')}
                    </Button>
                  </div>
                </details>
                {webhookTestResult && (
                  <div data-testid="webhook-test-result" className="inline-notice" role="status">
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 4 }}>
                      <strong>{t('ui.deliveryStatus')}</strong>
                      {status(
                        webhookTestResult.is_duplicate ? 'warning' : 'success',
                        webhookTestResult.is_duplicate ? t('ui.duplicateIngestedOnce') : t('ui.delivered'),
                      )}
                    </div>
                    <div>{t('ui.jobId2') + ' '}<code>{webhookTestResult.job_id}</code></div>
                    <div>{t('ui.duplicateFlag') + ' '}<code>{String(webhookTestResult.is_duplicate)}</code></div>
                  </div>
                )}
              </div>
            </section>
          </>
        )}

        {activeTab === 'outbox' && (
          <>
            <div className="inline-notice" role="note" style={{ marginBottom: 16 }} data-testid="outbox-mode">
              <strong>{t('ui.zeroExternalDispatchGuarantee')}</strong>{' '}
              {(language === 'en' && outboxGuarantee) || t('ui.dryRunOnlyAllOutboundMessagesAreCapturedInOutboxAndNeverTransmittedToRealCa')}
            </div>
            <div className="settings-split">
              <form
                className="resource-form settings-panel"
                style={{ gap: 16 }}
                aria-labelledby={`${ids}-dispatch`}
                onSubmit={(e) => {
                  e.preventDefault();
                  void handleTestDispatchOutbox();
                }}
              >
                <h2 id={`${ids}-dispatch`}>{t('ui.dispatchSimulatedAction')}</h2>
                <p className="resource-help" style={{ marginTop: -8 }}>{t('ui.testEmailOrWhatsappChannelTemplatesWithDeduplicationGuarantees')}</p>
                <fieldset className="resource-section" style={{ gap: 8 }}>
                  <legend className="resource-label" style={{ fontSize: 'var(--type-label)', fontWeight: 500, marginBottom: 6 }}>{t('ui.channel')}</legend>
                  <div style={{ display: 'flex', gap: 16 }}>
                    <label className="resource-check">
                      <input
                        type="radio"
                        name={`${ids}-channel`}
                        value="email"
                        checked={outboxChannel === 'email'}
                        onChange={() => {
                          setOutboxChannel('email');
                          setOutboxRecipient('customer@example.com');
                        }}
                      />
                      {t('ui.email')}
                    </label>
                    <label className="resource-check">
                      <input
                        type="radio"
                        name={`${ids}-channel`}
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
                </fieldset>
                <div className="resource-field">
                  <label htmlFor={`${ids}-recipient`}>{t('ui.recipient')}</label>
                  <input id={`${ids}-recipient`} data-testid="input-outbox-recipient" value={outboxRecipient} onChange={(e) => setOutboxRecipient(e.target.value)} placeholder={t('ui.eGUserExampleComOr91')} />
                </div>
                <div className="resource-field">
                  <label htmlFor={`${ids}-subject`}>{t('ui.subjectTemplateName')}</label>
                  <input id={`${ids}-subject`} data-testid="input-outbox-subject" value={outboxSubject} onChange={(e) => setOutboxSubject(e.target.value)} placeholder={t('ui.orderNotification')} />
                </div>
                <div className="resource-field">
                  <label htmlFor={`${ids}-body`}>{t('ui.bodyText')}</label>
                  <textarea id={`${ids}-body`} data-testid="textarea-outbox-body" value={outboxBody} onChange={(e) => setOutboxBody(e.target.value)} rows={3} style={{ fontFamily: 'inherit', fontSize: 'var(--type-body)' }} />
                </div>
                <div className="resource-field">
                  <label htmlFor={`${ids}-dedup`}>{t('ui.deduplicationKey')}</label>
                  <input id={`${ids}-dedup`} data-testid="input-outbox-dedup" value={outboxDedupKey} onChange={(e) => setOutboxDedupKey(e.target.value)} placeholder="dedup_order_123" />
                </div>
                <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                  <Button type="submit" variant="primary" data-testid="btn-dispatch-outbox" loading={actionLoading}>
                    {t('ui.dispatchToDryRunOutbox')}
                  </Button>
                </div>
                {lastDispatchResult && (
                  <div data-testid="dispatch-result-box" className="inline-notice" role="status">
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 4 }}>
                      <span>{t('ui.receipt') + ' '}<code>{lastDispatchResult.receipt.receipt_id}</code></span>
                      {status(lastDispatchResult.is_duplicate ? 'warning' : 'success', t(`statusLabels.${lastDispatchResult.receipt.status}`, lastDispatchResult.receipt.status))}
                    </div>
                    <div>{t('ui.duplicateFlag') + ' '}<code>{String(lastDispatchResult.is_duplicate)}</code></div>
                  </div>
                )}
              </form>

              <section aria-labelledby={`${ids}-captured`} style={{ minWidth: 0 }}>
                <div className="resource-detail-header">
                  <h2 id={`${ids}-captured`}>
                    {t('ui.capturedMessages')}{outbox.length})
                  </h2>
                  <div className="segmented" role="group" aria-label={t('triggers.channelFilter')}>
                    {(['all', 'email', 'whatsapp'] as const).map((f) => (
                      <button key={f} type="button" aria-pressed={outboxFilter === f} onClick={() => setOutboxFilter(f)}>
                        {t(`channelUpper.${f}`)}
                      </button>
                    ))}
                  </div>
                </div>
                {outbox.length === 0 ? (
                  <p className="inspection-empty">{t('ui.noMessagesCapturedInOutboxYet')}</p>
                ) : (
                  <ul className="resource-list">
                    {outbox.map((msg) => (
                      <li key={msg.receipt_id} data-testid={`outbox-item-${msg.receipt_id}`} className="settings-message">
                        <div className="resource-row-meta" style={{ marginTop: 0 }}>
                          <span>{t(`channelUpper.${msg.channel}`, msg.channel)}</span>
                          <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{msg.recipient}</span>
                          {status(msg.status === 'deduplicated' ? 'warning' : 'success', t(`statusLabels.${msg.status}`, msg.status))}
                        </div>
                        {msg.rendered_content.subject_or_template && <strong>{msg.rendered_content.subject_or_template}</strong>}
                        <div style={{ color: 'var(--text-secondary)', overflowWrap: 'anywhere' }}>{msg.rendered_content.body}</div>
                        <div className="resource-row-meta">
                          <span>
                            {t('ui.receipt') + ' '}<code>{msg.receipt_id}</code>
                          </span>
                          <span>{new Date(msg.created_at * 1000).toLocaleTimeString(locale)}</span>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>
          </>
        )}
      </div>

      <Modal
        open={scheduleModalOpen}
        onClose={() => setScheduleModalOpen(false)}
        title={t('ui.createScheduleTrigger')}
        maxWidth={640}
        footer={
          <>
            <Button variant="ghost" onClick={() => setScheduleModalOpen(false)}>
              {t('ui.cancel')}
            </Button>
            <Button
              variant="primary"
              data-testid="btn-submit-create-schedule"
              loading={actionLoading}
              disabled={noWorkflowToPick || !canEdit}
              onClick={async () => {
                const ok = await handleCreateSchedule();
                if (ok) setScheduleModalOpen(false);
              }}
            >
              {t('ui.saveSchedule')}
            </Button>
          </>
        }
      >
        <div className="resource-form" style={{ gap: 16 }}>
          {formError && (
            <div role="alert" className="inline-notice inline-notice--danger">
              <span>{formError}</span>
            </div>
          )}
          <div className="resource-field">
            <label htmlFor={`${ids}-sched-id`}>{t('ui.scheduleId')}</label>
            <input id={`${ids}-sched-id`} data-testid="input-new-schedule-id" value={newScheduleId} onChange={(e) => setNewScheduleId(e.target.value)} placeholder={t('ui.eGSchedOrderDaily')} />
          </div>
          <WorkflowPicker
            testId="select-new-schedule-wfid"
            value={newScheduleWfId}
            onChange={setNewScheduleWfId}
            options={workflowOptions}
            loading={workflowsLoading}
            error={workflowsError}
          />
          <div className="resource-limits" style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 2fr)' }}>
            <div className="resource-field">
              <label htmlFor={`${ids}-sched-type`}>{t('ui.scheduleType')}</label>
              <select id={`${ids}-sched-type`} value={newScheduleType} onChange={(e) => setNewScheduleType(e.target.value as any)}>
                <option value="interval">{t('ui.intervalSeconds')}</option>
                <option value="cron">{t('ui.cronExpression')}</option>
              </select>
            </div>
            <div className="resource-field">
              <label htmlFor={`${ids}-sched-value`}>{newScheduleType === 'interval' ? t('ui.intervalSeconds') : t('ui.cronExpression')}</label>
              {newScheduleType === 'interval' ? (
                <input
                  id={`${ids}-sched-value`}
                  type="number"
                  data-testid="input-schedule-interval"
                  value={String(newIntervalSeconds)}
                  onChange={(e) => setNewIntervalSeconds(Number(e.target.value))}
                  placeholder="60"
                />
              ) : (
                <input
                  id={`${ids}-sched-value`}
                  data-testid="input-schedule-cron"
                  value={newCronExpr}
                  onChange={(e) => setNewCronExpr(e.target.value)}
                  placeholder="0 * * * *"
                  aria-describedby={`${ids}-cron-help`}
                />
              )}
              {newScheduleType === 'cron' && <span id={`${ids}-cron-help`} className="resource-help">{t('triggers.cronHelp')}</span>}
            </div>
          </div>
          <div className="resource-limits" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
            <div className="resource-field">
              <label htmlFor={`${ids}-sched-tz`}>{t('ui.ianaTimezone')}</label>
              <input
                id={`${ids}-sched-tz`}
                data-testid="input-schedule-timezone"
                value={newTimezone}
                onChange={(e) => setNewTimezone(e.target.value)}
                placeholder="Asia/Kolkata"
                aria-describedby={`${ids}-tz-help`}
              />
              <span id={`${ids}-tz-help`} className="resource-help">
                {t('ui.examplePrefix')} <code>Asia/Kolkata, America/New_York, UTC</code>
              </span>
            </div>
            <div className="resource-field">
              <label htmlFor={`${ids}-sched-missed`}>{t('ui.missedRunPolicy')}</label>
              <select id={`${ids}-sched-missed`} value={newMissedPolicy} onChange={(e) => setNewMissedPolicy(e.target.value as any)}>
                <option value="fire_once">{t('ui.fireOnceEnqueue1Job')}</option>
                <option value="skip">{t('ui.skipDiscardMissed')}</option>
                <option value="burst">{t('ui.burstEnqueueAll')}</option>
              </select>
            </div>
          </div>
        </div>
      </Modal>

      <Modal
        open={webhookModalOpen}
        onClose={() => setWebhookModalOpen(false)}
        title={t('ui.registerWebhookEndpoint')}
        maxWidth={640}
        footer={
          <>
            <Button variant="ghost" onClick={() => setWebhookModalOpen(false)}>
              {t('ui.cancel')}
            </Button>
            <Button
              variant="primary"
              data-testid="btn-submit-register-webhook"
              loading={actionLoading}
              disabled={noWorkflowToPick || !canEdit}
              onClick={async () => {
                const ok = await handleRegisterWebhook();
                if (ok) setWebhookModalOpen(false);
              }}
            >
              {t('ui.registerWebhook')}
            </Button>
          </>
        }
      >
        <div className="resource-form" style={{ gap: 16 }}>
          {formError && (
            <div role="alert" className="inline-notice inline-notice--danger">
              <span>{formError}</span>
            </div>
          )}
          <div className="resource-field">
            <label htmlFor={`${ids}-wh-id`}>{t('ui.endpointId')}</label>
            <input id={`${ids}-wh-id`} data-testid="input-new-webhook-id" value={newEndpointId} onChange={(e) => setNewEndpointId(e.target.value)} placeholder={t('ui.eGOrdersIngress')} />
          </div>
          <WorkflowPicker
            testId="select-new-webhook-wfid"
            value={newWebhookWfId}
            onChange={setNewWebhookWfId}
            options={workflowOptions}
            loading={workflowsLoading}
            error={workflowsError}
          />
          <div className="resource-field">
            <label htmlFor={`${ids}-wh-window`}>{t('ui.replayWindowSeconds')}</label>
            <input id={`${ids}-wh-window`} type="number" value={String(newReplayWindow)} onChange={(e) => setNewReplayWindow(Number(e.target.value))} placeholder="300" />
          </div>
          <p className="resource-help">{t('triggers.secretShownOnce')}</p>
        </div>
      </Modal>
    </PageLayout>
  );
}
