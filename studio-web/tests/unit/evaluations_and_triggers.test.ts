import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NexuraClient } from '@liceron/nexura-sdk';
import type {
  EvaluationDataset,
  EvaluationSummary,
  EvaluationComparison,
  ScheduleConfig,
  WebhookConfig,
  DurableTriggerJob,
  TestWebhookResult,
  TestDispatchOutboxResult,
} from '@liceron/nexura-sdk';

describe('Evaluations, Schedules & Dry-Run Outbox Unit Tests (F13)', () => {
  let client: NexuraClient;

  beforeEach(() => {
    vi.restoreAllMocks();
    client = new NexuraClient({
      baseUrl: 'http://localhost:8000',
      workspaceId: 'default',
    });
  });

  describe('EvaluationsClient', () => {
    it('lists datasets and retrieves dataset details', async () => {
      const mockDatasets = {
        datasets: [
          {
            dataset_id: 'benchmark_customer_support',
            version: '1.0.0',
            description: 'Built-in support benchmark',
            example_count: 3,
            is_builtin: true,
          },
        ],
        total: 1,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockDatasets,
      } as Response);

      const res = await client.evaluations.listDatasets();
      expect(res.total).toBe(1);
      expect(res.datasets[0].dataset_id).toBe('benchmark_customer_support');
      expect(res.datasets[0].is_builtin).toBe(true);
    });

    it('creates custom dataset with examples and deletes custom dataset', async () => {
      const mockCreated: EvaluationDataset = {
        dataset_id: 'custom_eval_1',
        version: '1.0.0',
        description: 'Custom test dataset',
        examples: [
          {
            example_id: 'ex_1',
            input_data: { query: 'test' },
            expected_output: 'response',
            tags: ['test'],
          },
        ],
        created_at: 1700000000,
        updated_at: 1700000000,
        is_builtin: false,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockCreated,
      } as Response);

      const created = await client.evaluations.createDataset({
        dataset_id: 'custom_eval_1',
        version: '1.0.0',
        description: 'Custom test dataset',
        examples: mockCreated.examples,
      });

      expect(created.dataset_id).toBe('custom_eval_1');
      expect(created.examples.length).toBe(1);

      // Delete dataset
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => ({ deleted: true, dataset_id: 'custom_eval_1' }),
      } as Response);

      const delRes = await client.evaluations.deleteDataset('custom_eval_1');
      expect(delRes.deleted).toBe(true);
      expect(delRes.dataset_id).toBe('custom_eval_1');
    });

    it('runs evaluation with deterministic fake model and graders', async () => {
      const mockRunSummary: EvaluationSummary = {
        run_id: 'eval_run_9988',
        dataset_id: 'benchmark_customer_support',
        model_id: 'local-eval-fake',
        total_examples: 3,
        passed_examples: 3,
        pass_rate: 1.0,
        mean_score: 1.0,
        mean_latency_ms: 15.2,
        created_at: 1700000000,
        results: [
          {
            example_id: 'support_01',
            passed: true,
            score: 1.0,
            latency_ms: 14.5,
            output: 'For order tracking, visit /track with your order ID.',
            expected_output: 'For order tracking, visit /track with your order ID.',
            grader_results: { exact_match: { passed: true, score: 1.0 } },
          },
        ],
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockRunSummary,
      } as Response);

      const summary = await client.evaluations.runEvaluation({
        dataset_id: 'benchmark_customer_support',
        model_id: 'local-eval-fake',
        graders: ['exact_match', 'substring'],
      });

      expect(summary.run_id).toBe('eval_run_9988');
      expect(summary.pass_rate).toBe(1.0);
      expect(summary.passed_examples).toBe(3);
      expect(summary.results?.[0].passed).toBe(true);
    });

    it('compares two evaluation runs and identifies regressions/improvements', async () => {
      const mockComparison: EvaluationComparison = {
        base_run_id: 'eval_run_01',
        candidate_run_id: 'eval_run_02',
        pass_rate_diff: 0.25,
        mean_score_diff: 0.20,
        latency_diff_ms: -2.5,
        regressions: [],
        improvements: ['support_02'],
        total_compared: 3,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockComparison,
      } as Response);

      const cmp = await client.evaluations.compareRuns('eval_run_01', 'eval_run_02');
      expect(cmp.pass_rate_diff).toBe(0.25);
      expect(cmp.improvements).toContain('support_02');
      expect(cmp.regressions.length).toBe(0);
    });
  });

  describe('TriggersClient (Schedules, Webhooks & Observable Outbox)', () => {
    it('creates schedule with Asia/Kolkata timezone and manages pause/resume', async () => {
      const mockSchedule: ScheduleConfig = {
        schedule_id: 'sched_kolkata_daily',
        workspace_id: 'default',
        workflow_id: 'order_support_v1',
        interval_seconds: 3600,
        timezone: 'Asia/Kolkata',
        missed_run_policy: 'fire_once',
        paused: false,
        created_at: 1700000000,
        updated_at: 1700000000,
        next_run_at: 1700003600,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockSchedule,
      } as Response);

      const created = await client.triggers.createSchedule({
        schedule_id: 'sched_kolkata_daily',
        workflow_id: 'order_support_v1',
        interval_seconds: 3600,
        timezone: 'Asia/Kolkata',
        missed_run_policy: 'fire_once',
      });

      expect(created.schedule_id).toBe('sched_kolkata_daily');
      expect(created.timezone).toBe('Asia/Kolkata');
      expect(created.missed_run_policy).toBe('fire_once');

      // Pause schedule
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => ({ schedule_id: 'sched_kolkata_daily', paused: true }),
      } as Response);
      const pauseRes = await client.triggers.pauseSchedule('sched_kolkata_daily');
      expect(pauseRes.paused).toBe(true);

      // Resume schedule
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => ({ schedule_id: 'sched_kolkata_daily', paused: false }),
      } as Response);
      const resumeRes = await client.triggers.resumeSchedule('sched_kolkata_daily');
      expect(resumeRes.paused).toBe(false);
    });

    it('registers signed webhook and detects duplicate deliveries', async () => {
      const mockWebhook: WebhookConfig = {
        endpoint_id: 'order_webhook',
        workspace_id: 'default',
        workflow_id: 'order_support_v1',
        secret: 'sec_0123456789abcdef0123456789abcdef',
        replay_window_seconds: 300,
        created_at: 1700000000,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockWebhook,
      } as Response);

      const registered = await client.triggers.registerWebhook({
        endpoint_id: 'order_webhook',
        workflow_id: 'order_support_v1',
        replay_window_seconds: 300,
      });

      expect(registered.endpoint_id).toBe('order_webhook');
      expect(registered.secret.length).toBeGreaterThanOrEqual(32);

      // Test valid delivery
      const validTestRes: TestWebhookResult = {
        success: true,
        job_id: 'job_wh_1001',
        is_duplicate: false,
        status: 'enqueued',
        timestamp_used: 1700000000,
        signature_used: 'sig_valid_hex',
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => validTestRes,
      } as Response);

      const testRes1 = await client.triggers.testWebhook('order_webhook', {
        event_id: 'evt_1001',
        payload: { order_id: 'ord_123' },
      });
      expect(testRes1.is_duplicate).toBe(false);
      expect(testRes1.job_id).toBe('job_wh_1001');

      // Test duplicate delivery with exact same event_id
      const dupTestRes: TestWebhookResult = {
        success: true,
        job_id: 'job_wh_1001',
        is_duplicate: true,
        status: 'enqueued',
        timestamp_used: 1700000000,
        signature_used: 'sig_valid_hex',
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => dupTestRes,
      } as Response);

      const testRes2 = await client.triggers.testWebhook('order_webhook', {
        event_id: 'evt_1001',
        payload: { order_id: 'ord_123' },
      });
      expect(testRes2.is_duplicate).toBe(true);
    });

    it('captures email & whatsapp in observable dry-run outbox with deduplication', async () => {
      const mockOutboxResult: TestDispatchOutboxResult = {
        success: true,
        receipt: {
          receipt_id: 'rcpt_email_01',
          message_id: 'msg_01',
          channel: 'email',
          recipient: 'customer@example.com',
          status: 'dry_run',
          dry_run: true,
          rendered_content: {
            subject_or_template: 'Order Update #9988',
            body: 'Your package is on its way.',
          },
          created_at: 1700000000,
        },
        is_duplicate: false,
        dry_run: true,
        guarantee: 'Simulated in local dry-run outbox without external API dispatch.',
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockOutboxResult,
      } as Response);

      const dispatchRes = await client.triggers.testDispatchOutbox({
        channel: 'email',
        recipient: 'customer@example.com',
        subject_or_template: 'Order Update #9988',
        body: 'Your package is on its way.',
        dedup_key: 'dedup_order_9988',
      });

      expect(dispatchRes.dry_run).toBe(true);
      expect(dispatchRes.receipt.status).toBe('dry_run');
      expect(dispatchRes.is_duplicate).toBe(false);

      // Verify listing outbox guarantees
      const mockOutboxList = {
        outbox: [mockOutboxResult.receipt],
        total: 1,
        guarantee: 'DRY_RUN_ONLY: All outbound messages are captured in outbox and never transmitted to real carriers.',
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockOutboxList,
      } as Response);

      const outboxList = await client.triggers.listOutbox('email');
      expect(outboxList.total).toBe(1);
      expect(outboxList.guarantee).toContain('DRY_RUN_ONLY');
      expect(outboxList.outbox[0].channel).toBe('email');
    });

    it('lists durable trigger jobs enqueued by schedules and webhooks', async () => {
      const mockJobs: { jobs: DurableTriggerJob[]; total: number } = {
        jobs: [
          {
            job_id: 'job_sched_01',
            workspace_id: 'default',
            workflow_id: 'order_support_v1',
            trigger_type: 'schedule',
            trigger_source_id: 'sched_kolkata_daily',
            status: 'pending',
            payload: {},
            scheduled_for: 1700003600,
            created_at: 1700000000,
          },
        ],
        total: 1,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockJobs,
      } as Response);

      const jobsRes = await client.triggers.listJobs();
      expect(jobsRes.total).toBe(1);
      expect(jobsRes.jobs[0].trigger_type).toBe('schedule');
      expect(jobsRes.jobs[0].status).toBe('pending');
    });
  });
});
