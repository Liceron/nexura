// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useApi } from '../../api/context';
import { Card } from '../../components/Card';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { useI18n } from '../../i18n';

export function SystemView() {
  const { connection, refreshConnection } = useApi();
  const { t } = useI18n();

  const caps = connection.capabilities;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <h2 style={{ fontSize: 18, fontWeight: 600 }}>{t('system.title')}</h2>
        <Button variant="secondary" size="sm" onClick={refreshConnection}>
          {t('ui.refreshDiagnostics2')}
        </Button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16 }}>
        <Card>
          <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 4 }}>
            {t('system.sdkVersion')}
          </div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'var(--font-mono)' }}>
            {caps?.sdk_version || connection.sdkVersion || t('ui.unknown2')}
          </div>
        </Card>

        <Card>
          <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 4 }}>
            {t('system.activeProfile')}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
            <Badge variant="success">
              {t(`profileLabels.${caps?.active_profile || connection.profile || 'local-owner'}`, caps?.active_profile || connection.profile)}
            </Badge>
          </div>
        </Card>

        <Card>
          <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 4 }}>
            {t('connection.serverUrl')}
          </div>
          <div style={{ fontSize: 14, fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            <code>{connection.baseUrl}</code>
          </div>
        </Card>
      </div>

      <Card>
        <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 16 }}>{t('ui.runtimeFeatureFlags')}</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-subtle)', paddingBottom: 8 }}>
            <span>{t('ui.moduleDescriptorsDiscovery')}<code>/api/v1/modules</code>)</span>
            <Badge variant={caps?.capabilities?.modules ? 'success' : 'neutral'}>
              {caps?.capabilities?.modules ? t('system.enabled') : t('system.disabled')}
            </Badge>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-subtle)', paddingBottom: 8 }}>
            <span>{t('ui.authoritativeGraphValidation')}<code>/api/v1/workflows/validate</code>)</span>
            <Badge variant={caps?.capabilities?.validation ? 'success' : 'neutral'}>
              {caps?.capabilities?.validation ? t('system.enabled') : t('system.disabled')}
            </Badge>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-subtle)', paddingBottom: 8 }}>
            <span>{t('ui.downloadablePortableBundleExport')}<code>/export/download</code>)</span>
            <Badge variant={caps?.capabilities?.downloadable_export ? 'success' : 'neutral'}>
              {caps?.capabilities?.downloadable_export ? t('system.enabled') : t('system.disabled')}
            </Badge>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span>{t('ui.persistentLiveSseStream')}<code>/api/v1/runs/&#123;id&#125;/events?live=true</code>)</span>
            <Badge variant="success">{t('system.enabled')}</Badge>
          </div>
        </div>
      </Card>
    </div>
  );
}
