// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useApi } from '../../api/context';
import { Button } from '../../components/Button';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { useI18n } from '../../i18n';

/** Capabilities Studio can describe; any other flag the service reports is listed by its name. */
const KNOWN_CAPABILITIES: { flag: string; labelKey: string; path: string }[] = [
  { flag: 'modules', labelKey: 'ui.moduleDescriptorsDiscovery', path: '/api/v1/modules' },
  { flag: 'validation', labelKey: 'ui.authoritativeGraphValidation', path: '/api/v1/workflows/validate' },
  { flag: 'downloadable_export', labelKey: 'ui.downloadablePortableBundleExport', path: '/export/download' },
  { flag: 'live_events', labelKey: 'ui.persistentLiveSseStream', path: '/api/v1/runs/{id}/events?live=true' },
];

export function SystemView() {
  const { connection, refreshConnection } = useApi();
  const { t } = useI18n();

  const caps = connection.capabilities;
  const flags: Record<string, boolean> = caps?.capabilities ?? {};
  const known = new Set(KNOWN_CAPABILITIES.map((c) => c.flag));
  const others = Object.keys(flags).filter((flag) => !known.has(flag)).sort();
  const profile = caps?.active_profile || connection.profile;
  // Only what the service reported: a missing flag is "Not reported", never Enabled or Disabled.
  const state = (flag: string) =>
    flags[flag] === true ? 'enabled' : flags[flag] === false ? 'disabled' : 'unknown';
  const stateLabel = (flag: string) =>
    ({ enabled: t('system.enabled'), disabled: t('system.disabled'), unknown: t('system.notReported') })[state(flag)];
  const stateClass = (flag: string) =>
    ({ enabled: 'run-status--success', disabled: 'run-status--neutral', unknown: 'run-status--neutral run-status--unknown' })[state(flag)];
  // Friendly names for the service's other built-in flags; unknown (extension) flags keep their key.
  const FLAG_LABELS = new Set(['export', 'run_diagnostics', 'diagnostic_trace_download', 'vector_storage', 'sandbox_execution', 'evaluations', 'triggers']);

  return (
    <PageLayout data-testid="system-view">
      <PageHeader
        title={t('system.title')}
        actions={
          <Button onClick={refreshConnection} data-testid="system-refresh">
            {t('ui.refreshDiagnostics2')}
          </Button>
        }
      />

      <dl className="quickstart-facts system-facts" aria-label={t('system.serviceDetails')}>
        <dt>{t('connection.serverUrl')}</dt>
        <dd><code>{connection.baseUrl}</code></dd>
        <dt>{t('system.sdkVersion')}</dt>
        <dd>
          {caps?.sdk_version || connection.sdkVersion ? <code>{caps?.sdk_version || connection.sdkVersion}</code> : t('system.notReported')}
        </dd>
        <dt>{t('system.activeProfile')}</dt>
        <dd>{profile ? t(`profileLabels.${profile}`, profile) : t('system.notReported')}</dd>
      </dl>

      <h2 style={{ margin: '24px 0 8px' }}>{t('ui.runtimeFeatureFlags')}</h2>
      <div className="inspection-table-scroll" role="region" aria-label={t('ui.runtimeFeatureFlags')} tabIndex={0}>
        <table className="inspection-table settings-table">
          <caption className="studio-sr-only">{t('ui.runtimeFeatureFlags')}</caption>
          <thead>
            <tr>
              <th scope="col">{t('system.capability')}</th>
              <th scope="col">{t('ui.status')}</th>
              <th scope="col">{t('system.details')}</th>
            </tr>
          </thead>
          <tbody>
            {KNOWN_CAPABILITIES.map((cap) => (
              <tr key={cap.flag} data-testid={`capability-${cap.flag}`}>
                <th scope="row" style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{t(cap.labelKey)}</th>
                <td>
                  <span className={`run-status ${stateClass(cap.flag)}`}>
                    <span aria-hidden="true">●</span> {stateLabel(cap.flag)}
                  </span>
                </td>
                <td><code>{cap.path}</code></td>
              </tr>
            ))}
            {others.map((flag) => (
              <tr key={flag} data-testid={`capability-${flag}`}>
                <th scope="row" style={{ fontWeight: 500 }}>
                  {FLAG_LABELS.has(flag) ? (
                    <>
                      {t(`system.flag.${flag}`)} <code className="resource-help">{flag}</code>
                    </>
                  ) : (
                    <code>{flag}</code>
                  )}
                </th>
                <td>
                  <span className={`run-status ${stateClass(flag)}`}>
                    <span aria-hidden="true">●</span> {stateLabel(flag)}
                  </span>
                </td>
                <td>—</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </PageLayout>
  );
}
