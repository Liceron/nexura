// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState, useEffect, useCallback, useRef } from 'react';
import { useLatestT } from '../../i18n';
import { useApi } from '../../api/context';
import type {
  ScheduleConfig,
  WebhookConfig,
  DurableTriggerJob,
  OutboxReceipt,
  TestWebhookResult,
  TestDispatchOutboxResult,
  WorkflowRecord,
} from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';
import { NexuraApiError } from '@liceron/nexura-sdk';
import { deriveEndpoint } from '../../api/endpoint';
import { webhookDeliveryUrl } from './webhookSnippet';
import { getMyMembership } from '../../api/membership';

/** Workflows offered by the trigger forms' picker. */
export interface TriggerWorkflowOption {
  workflow_id: string;
  name: string;
}

/**
 * B39: the result of a successful registration. The signing secret is returned by the server
 * only once, so it lives in this React state alone (never storage, logs or the URL) and is
 * dropped on dismiss or when the triggers view unmounts.
 */
export interface RegisteredWebhookSecret {
  endpointId: string;
  workflowId: string;
  secret: string;
  deliveryUrl: string;
  /** Remote single-operator (token) mode only: the endpoint's delivery token, returned once. */
  deliveryToken?: string;
}

/** A delivery token just issued by rotation (token mode). Held in React state only. */
export interface IssuedDeliveryToken {
  endpointId: string;
  token: string;
  header: string;
}

const WORKFLOW_PAGE_SIZE = 100; // server maximum for GET /workflows
const WORKFLOW_MAX_PAGES = 20;

export function useTriggers(initialWorkflowId: string | null = null) {
  const t = useLatestT();
  const { manager, connection } = useApi();
  const client = manager.client;

  const [schedules, setSchedules] = useState<ScheduleConfig[]>([]);
  const [webhooks, setWebhooks] = useState<WebhookConfig[]>([]);
  const [jobs, setJobs] = useState<DurableTriggerJob[]>([]);
  const [outbox, setOutbox] = useState<OutboxReceipt[]>([]);
  const [outboxGuarantee, setOutboxGuarantee] = useState<string>('');

  const [loading, setLoading] = useState<boolean>(true);
  const [actionLoading, setActionLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // Schedule form state
  const [newScheduleId, setNewScheduleId] = useState<string>('');
  const [newScheduleWfId, setNewScheduleWfId] = useState<string>(initialWorkflowId ?? '');
  const [newScheduleType, setNewScheduleType] = useState<'interval' | 'cron'>('interval');
  const [newIntervalSeconds, setNewIntervalSeconds] = useState<number>(60);
  const [newCronExpr, setNewCronExpr] = useState<string>('0 * * * *');
  const [newTimezone, setNewTimezone] = useState<string>('Asia/Kolkata');
  const [newMissedPolicy, setNewMissedPolicy] = useState<'fire_once' | 'skip' | 'burst'>('fire_once');

  // Webhook register state
  const [newEndpointId, setNewEndpointId] = useState<string>('');
  const [newWebhookWfId, setNewWebhookWfId] = useState<string>(initialWorkflowId ?? '');
  const [newReplayWindow, setNewReplayWindow] = useState<number>(300);
  const [registeredWebhook, setRegisteredWebhook] = useState<RegisteredWebhookSecret | null>(null);
  // Token mode (P38 C4): delivery tokens are shown once, never stored or shown again.
  const [issuedDeliveryToken, setIssuedDeliveryToken] = useState<IssuedDeliveryToken | null>(null);
  const [deliveryTokensSeen, setDeliveryTokensSeen] = useState(false);
  const [rotatingEndpointId, setRotatingEndpointId] = useState<string | null>(null);
  // A refused rotation is reported on its webhook's row, not in the page banner (review minor 3).
  const [rotateError, setRotateError] = useState<{ endpointId: string; message: string } | null>(null);

  // Workflow picker (B39): the workspace's workflows instead of a free-text id.
  const [workflowOptions, setWorkflowOptions] = useState<TriggerWorkflowOption[]>([]);
  const [workflowsLoading, setWorkflowsLoading] = useState<boolean>(true);
  const [workflowsError, setWorkflowsError] = useState<string | null>(null);

  // Caller's role, same source as the Agents and Skills pages (a server without memberships is
  // the local owner). Registering and scheduling need edit (registering also run); the server
  // enforces this, Studio only avoids offering forms a viewer cannot submit.
  const [role, setRole] = useState<string | null>(null);

  // Webhook test state
  const [testEndpointId, setTestEndpointId] = useState<string>('');
  const [testEventId, setTestEventId] = useState<string>('');
  const [testPayloadJson, setTestPayloadJson] = useState<string>('{\n  "order_id": "ord_1001",\n  "status": "shipped"\n}');
  const [webhookTestResult, setWebhookTestResult] = useState<TestWebhookResult | null>(null);
  const [webhookTestError, setWebhookTestError] = useState<string | null>(null);

  // Outbox test dispatch state
  const [outboxChannel, setOutboxChannel] = useState<'email' | 'whatsapp'>('email');
  const [outboxRecipient, setOutboxRecipient] = useState<string>('customer@example.com');
  const [outboxSubject, setOutboxSubject] = useState<string>(t('triggers.sampleSubject'));
  const [outboxBody, setOutboxBody] = useState<string>(t('triggers.sampleBody'));
  const [outboxDedupKey, setOutboxDedupKey] = useState<string>('order_1001_confirmation');
  const [outboxFilter, setOutboxFilter] = useState<'all' | 'email' | 'whatsapp'>('all');
  const [lastDispatchResult, setLastDispatchResult] = useState<TestDispatchOutboxResult | null>(null);

  const fetchWorkflows = useCallback(async () => {
    setWorkflowsLoading(true);
    setWorkflowsError(null);
    try {
      const items: WorkflowRecord[] = [];
      let complete = false;
      for (let page = 0; page < WORKFLOW_MAX_PAGES; page += 1) {
        const res = await client.workflows.list({ limit: WORKFLOW_PAGE_SIZE, offset: page * WORKFLOW_PAGE_SIZE });
        items.push(...(res.items || []));
        if (!res.has_more) {
          complete = true;
          break;
        }
      }
      const options = items.map((w) => ({ workflow_id: w.workflow_id, name: w.name || w.workflow_id }));
      // A workflow opened from its page stays selectable when it is past the listed pages. When
      // the list is complete, an id it lacks is not in this workspace (another workspace's id,
      // a deleted workflow or a typo in the URL), so it is neither offered nor preselected.
      const routedListed = Boolean(initialWorkflowId && options.some((o) => o.workflow_id === initialWorkflowId));
      if (initialWorkflowId && !routedListed && !complete) {
        options.unshift({ workflow_id: initialWorkflowId, name: initialWorkflowId });
      }
      setWorkflowOptions(options);
      const routed = initialWorkflowId && options.some((o) => o.workflow_id === initialWorkflowId) ? initialWorkflowId : null;
      const fallback = routed ?? options[0]?.workflow_id ?? '';
      const keep = (current: string) => (current && options.some((o) => o.workflow_id === current) ? current : fallback);
      setNewScheduleWfId(keep);
      setNewWebhookWfId(keep);
    } catch (err: unknown) {
      setWorkflowsError(formatApiError(err, t));
    } finally {
      setWorkflowsLoading(false);
    }
  }, [client, initialWorkflowId, t]);

  const fetchSchedules = useCallback(async () => {
    try {
      const res = await client.triggers.listSchedules();
      setSchedules(res.schedules || []);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  }, [client, t]);

  const fetchWebhooks = useCallback(async () => {
    try {
      const res = await client.triggers.listWebhooks();
      setWebhooks(res.webhooks || []);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  }, [client, t]);

  const fetchJobs = useCallback(async () => {
    try {
      const res = await client.triggers.listJobs(50);
      setJobs(res.jobs || []);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  }, [client, t]);

  const fetchOutbox = useCallback(async () => {
    try {
      const ch = outboxFilter === 'all' ? undefined : outboxFilter;
      const res = await client.triggers.listOutbox(ch);
      setOutbox(res.outbox || []);
      setOutboxGuarantee(res.guarantee || 'DRY_RUN_ONLY');
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  }, [client, outboxFilter, t]);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([fetchSchedules(), fetchWebhooks(), fetchJobs(), fetchOutbox(), fetchWorkflows()]);
    } finally {
      setLoading(false);
    }
  }, [fetchSchedules, fetchWebhooks, fetchJobs, fetchOutbox, fetchWorkflows]);

  useEffect(() => {
    // A secret shown for one workspace must not linger after switching to another.
    setRegisteredWebhook(null);
    setIssuedDeliveryToken(null);
    refreshAll();
  }, [connection.workspace]);

  useEffect(() => {
    fetchOutbox();
  }, [outboxFilter, fetchOutbox]);

  useEffect(() => {
    let current = true;
    setRole(null);
    // Wrapped so a client without the request method falls back like a failed request.
    Promise.resolve()
      .then(() => getMyMembership(client, connection.workspace))
      .then((m) => current && setRole(String((m as unknown as { role?: string })?.role ?? 'admin')))
      .catch(() => current && setRole('admin'));
    return () => {
      current = false;
    };
  }, [client, connection.workspace]);
  const canEdit = role === 'admin' || role === 'editor';

  // The view lists only the routed workflow's webhooks, so the test target (and the signing
  // help that follows it) defaults to one of those, and moves when the filter changes.
  useEffect(() => {
    const visible = initialWorkflowId ? webhooks.filter((w) => w.workflow_id === initialWorkflowId) : webhooks;
    setTestEndpointId((current) =>
      current && visible.some((w) => w.endpoint_id === current) ? current : visible[0]?.endpoint_id ?? '',
    );
  }, [webhooks, initialWorkflowId]);

  // The routed workflow can change without a remount (the in-view "Show all triggers" link
  // keeps the view, and with it a one-time secret, mounted): preselect a newly routed workflow.
  const previousRoutedId = useRef(initialWorkflowId);
  useEffect(() => {
    if (previousRoutedId.current === initialWorkflowId) return;
    previousRoutedId.current = initialWorkflowId;
    if (initialWorkflowId && workflowOptions.some((o) => o.workflow_id === initialWorkflowId)) {
      setNewScheduleWfId(initialWorkflowId);
      setNewWebhookWfId(initialWorkflowId);
    }
  }, [initialWorkflowId, workflowOptions]);

  // Schedule Actions
  const handleCreateSchedule = async (): Promise<boolean> => {
    setFormError(null);
    if (!newScheduleId.trim()) {
      setFormError(t('validation.scheduleRequired'));
      return false;
    }
    if (!newScheduleWfId.trim()) {
      setFormError(t('validation.workflowRequired'));
      return false;
    }

    setActionLoading(true);
    try {
      // The SDK's legacy schedule DTO uses `cron`; the HTTP contract uses `cron_expr`.
      await client.request<ScheduleConfig>('POST', '/triggers/schedules', { body: {
        schedule_id: newScheduleId.trim(),
        workflow_id: newScheduleWfId.trim(),
        timezone: newTimezone.trim() || 'UTC',
        interval_seconds: newScheduleType === 'interval' ? Number(newIntervalSeconds) : undefined,
        cron_expr: newScheduleType === 'cron' ? newCronExpr.trim() : undefined,
        missed_run_policy: newMissedPolicy === 'burst' ? 'fire_all' : newMissedPolicy,
      } });
      await fetchSchedules();
      setNewScheduleId('');
      return true;
    } catch (err: unknown) {
      setFormError(formatApiError(err, t));
      return false;
    } finally {
      setActionLoading(false);
    }
  };

  const handlePauseSchedule = async (scheduleId: string) => {
    setActionLoading(true);
    setError(null);
    try {
      await client.triggers.pauseSchedule(scheduleId);
      await fetchSchedules();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  const handleResumeSchedule = async (scheduleId: string) => {
    setActionLoading(true);
    setError(null);
    try {
      await client.triggers.resumeSchedule(scheduleId);
      await fetchSchedules();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  const handleDeleteSchedule = async (scheduleId: string) => {
    setActionLoading(true);
    setError(null);
    try {
      await client.triggers.deleteSchedule(scheduleId);
      await fetchSchedules();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  // Webhook Actions
  const handleRegisterWebhook = async (): Promise<boolean> => {
    setFormError(null);
    if (!newEndpointId.trim()) {
      setFormError(t('validation.endpointRequired'));
      return false;
    }
    if (!newWebhookWfId.trim()) {
      setFormError(t('validation.workflowRequired'));
      return false;
    }

    setActionLoading(true);
    try {
      const registered = await client.triggers.registerWebhook({
        endpoint_id: newEndpointId.trim(),
        workflow_id: newWebhookWfId.trim(),
        replay_window_seconds: Number(newReplayWindow),
      });
      const endpointId = registered.endpoint_id || newEndpointId.trim();
      // Shown once (B39): the server never returns this secret again.
      setRegisteredWebhook({
        endpointId,
        workflowId: registered.workflow_id || newWebhookWfId.trim(),
        secret: registered.secret ?? '',
        deliveryUrl: webhookDeliveryUrl(deriveEndpoint(manager.baseUrl, window.location.origin), endpointId),
        deliveryToken: typeof registered.delivery_token === 'string' && registered.delivery_token ? registered.delivery_token : undefined,
      });
      if (registered.delivery_token) setDeliveryTokensSeen(true);
      await fetchWebhooks();
      setTestEndpointId(endpointId);
      setNewEndpointId('');
      return true;
    } catch (err: unknown) {
      setFormError(formatApiError(err, t));
      return false;
    } finally {
      setActionLoading(false);
    }
  };

  const handleDeleteWebhook = async (endpointId: string) => {
    setActionLoading(true);
    setError(null);
    try {
      await client.triggers.deleteWebhook(endpointId);
      // Deleting a webhook deletes its signing secret and delivery token on the service; drop any
      // one-time panel for it too (secret, token and snippets) (review M1).
      setIssuedDeliveryToken((current) => (current?.endpointId === endpointId ? null : current));
      setRegisteredWebhook((current) => (current?.endpointId === endpointId ? null : current));
      await fetchWebhooks();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  const handleTestWebhook = async (options: {
    invalid_signature?: boolean;
    expired_timestamp?: boolean;
    useExistingEventId?: boolean;
  } = {}) => {
    setWebhookTestResult(null);
    setWebhookTestError(null);
    if (!testEndpointId) {
      setWebhookTestError(t('validationMessages.selectWebhook'));
      return;
    }

    let parsedPayload: Record<string, any> = {};
    try {
      parsedPayload = JSON.parse(testPayloadJson);
    } catch {
      setWebhookTestError(t('validationMessages.webhookJson'));
      return;
    }

    const eventId = options.useExistingEventId && testEventId
      ? testEventId
      : `evt_${Date.now()}_${Math.floor(Math.random() * 1000)}`;

    if (!options.useExistingEventId) {
      setTestEventId(eventId);
    }

    setActionLoading(true);
    setError(null);
    try {
      const res = await client.triggers.testWebhook(testEndpointId, {
        event_id: eventId,
        payload: parsedPayload,
        invalid_signature: options.invalid_signature,
        expired_timestamp: options.expired_timestamp,
      });
      setWebhookTestResult(res);
      await fetchJobs();
    } catch (err: unknown) {
      // A refused simulated delivery is the expected result of a negative test, not a sign-in
      // problem; any other 401 from this call still says to sign in again.
      const refused = err instanceof NexuraApiError && (err.code === 'INVALID_SIGNATURE' || err.code === 'MISSING_SIGNATURE_HEADERS');
      setWebhookTestError(refused ? `${err.message.replace(/^\[\d+\]\s+[A-Za-z0-9_]+:\s*/, '')} (${err.code})` : formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  // Outbox Actions
  const handleTestDispatchOutbox = async () => {
    if (!outboxRecipient.trim()) {
      setError('Recipient is required.');
      return;
    }
    if (!outboxBody.trim()) {
      setError(t('validationMessages.messageBody'));
      return;
    }

    setActionLoading(true);
    setError(null);
    try {
      const res = await client.triggers.testDispatchOutbox({
        channel: outboxChannel,
        recipient: outboxRecipient.trim(),
        subject_or_template: outboxSubject.trim() || undefined,
        body: outboxBody.trim(),
        dedup_key: outboxDedupKey.trim() || undefined,
      });
      setLastDispatchResult(res);
      await fetchOutbox();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  const dismissRegisteredWebhook = useCallback(() => setRegisteredWebhook(null), []);

  /** Token mode: issue a new delivery token. The previous one stops working immediately. */
  const handleRotateDeliveryToken = async (endpointId: string) => {
    setRotatingEndpointId(endpointId);
    setRotateError(null);
    setIssuedDeliveryToken(null);
    try {
      const issued = await client.triggers.rotateDeliveryToken(endpointId);
      setIssuedDeliveryToken({
        endpointId: issued.endpoint_id || endpointId,
        token: issued.delivery_token,
        header: issued.delivery_token_header || 'Authorization: Bearer <delivery_token>',
      });
      setDeliveryTokensSeen(true);
    } catch (err: unknown) {
      setRotateError({ endpointId, message: formatApiError(err, t) });
    } finally {
      setRotatingEndpointId(null);
    }
  };
  const dismissIssuedDeliveryToken = useCallback(() => setIssuedDeliveryToken(null), []);
  // A service in token mode: Studio is sending an operator token to this service, or the service
  // returned a delivery token. Local mode never shows the rotate action.
  const deliveryTokenMode = deliveryTokensSeen || manager.hasAuthToken;

  /** Delivery URL of any registered endpoint (no secret involved). */
  const deliveryUrlFor = useCallback(
    (endpointId: string) => webhookDeliveryUrl(deriveEndpoint(manager.baseUrl, window.location.origin), endpointId),
    [manager],
  );

  return {
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
    // Schedule
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
    // Webhook
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
    webhookTestError,
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
    // Role
    role,
    canEdit,
    // Workflow picker
    workflowOptions,
    workflowsLoading,
    workflowsError,
    // Outbox
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
  };
}
