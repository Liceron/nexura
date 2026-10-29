// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { InlineConfirm } from '../../components/InlineConfirm';
import React, { useEffect, useId, useRef, useState } from 'react';
import { formatApiError } from '../../api/errors';
import { useApi } from '../../api/context';
import { moduleCredentialTypes } from '../modules/connectors';
import { useCredentials } from './useCredentials';
import {
  CREDENTIAL_PROVIDERS,
  CREDENTIAL_TYPES,
  ROLE_DEFINITIONS,
  CredentialProvider,
  CredentialType,
  WorkspaceRole,
} from './types';
import { Button } from '../../components/Button';
import { Input } from '../../components/Input';
import { Modal } from '../../components/Modal';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { WorkflowTabs } from '../../components/WorkflowTabs';
import { useI18n } from '../../i18n';

/** The service's credential ID rule (service/api/credentials.py). */
const CREDENTIAL_ID = /^[a-zA-Z0-9_.-]+$/;

export function CredentialsView() {
  const { t, locale } = useI18n();
  const {
    credentials,
    members,
    membersAvailable,
    myMembership,
    loading,
    error,
    verificationMap,
    createCredential,
    deleteCredential,
    verifyCredential,
    revokeCredential,
    addMember,
    updateMemberRole,
    removeMember,
  } = useCredentials();

  const { manager } = useApi();
  const [moduleTypes, setModuleTypes] = useState<string[]>([]);
  useEffect(() => {
    let alive = true;
    manager.client.modules
      .list()
      .then((res) => {
        if (alive) setModuleTypes(moduleCredentialTypes(res.items || res.modules || []));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [manager]);
  const genericIds = new Set<string>(CREDENTIAL_TYPES.map((c) => c.id));
  const extraTypes = moduleTypes.filter((m) => !genericIds.has(m));
  const [activeTab, setActiveTab] = useState<'credentials' | 'members' | 'matrix'>('credentials');
  useEffect(() => {
    if (!membersAvailable && activeTab === 'members') setActiveTab('credentials');
  }, [membersAvailable, activeTab]);
  const [modalOpen, setModalOpen] = useState(false);
  const ids = useId();
  const panelId = `${ids}-panel`;
  const addButtonRef = useRef<HTMLButtonElement>(null);

  // Transient form state — cleared immediately upon submit/close
  const [formId, setFormId] = useState('');
  const [formType, setFormType] = useState<CredentialType>('api_key');
  const [formProvider, setFormProvider] = useState<CredentialProvider>('openai');
  const [formDesc, setFormDesc] = useState('');
  // The secret field is uncontrolled: its value never goes through React state or the DOM value
  // attribute, only the live input (read once on submit, then cleared) (review minor 1).
  const secretRef = useRef<HTMLInputElement>(null);
  const [hasSecret, setHasSecret] = useState(false);
  const [referenceTaken, setReferenceTaken] = useState(false);
  const [idInvalid, setIdInvalid] = useState(false);
  const [formEnv, setFormEnv] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // New member form
  const [newPrincipalId, setNewPrincipalId] = useState('');
  const [newRole, setNewRole] = useState<WorkspaceRole>('viewer');
  const [memberSubmitting, setMemberSubmitting] = useState(false);

  const isViewer = myMembership.role === 'viewer';
  const isAdmin = myMembership.role === 'admin';

  const handleOpenModal = () => {
    setFormId('');
    setFormType('api_key');
    setFormProvider('openai');
    setFormDesc('');
    setHasSecret(false);
    setReferenceTaken(false);
    setFormEnv('');
    setActionError(null);
    setModalOpen(true);
  };

  const clearSecret = () => {
    if (secretRef.current) secretRef.current.value = '';
    setHasSecret(false);
  };

  const handleCloseModal = () => {
    // SECURITY: Purge transient secret immediately
    clearSecret();
    setModalOpen(false);
  };

  const handleCreateCredential = async (e: React.FormEvent) => {
    e.preventDefault();
    const secret = secretRef.current?.value.trim() ?? '';
    if (!formId.trim() || !secret) return;
    if (!CREDENTIAL_ID.test(formId.trim())) {
      setActionError(t('credentials.errorBadId'));
      setIdInvalid(true);
      return;
    }

    try {
      setSubmitting(true);
      setActionError(null);
      setReferenceTaken(false);
      await createCredential({
        credential_id: formId.trim(),
        credential_type: formType,
        provider: formProvider,
        description: formDesc.trim(),
        secret,
        environment_variable: formEnv.trim() || undefined,
      });
      // SECURITY: Immediate zeroing of the secret field
      clearSecret();
      setModalOpen(false);
    } catch (err: unknown) {
      // One plain-language error, at the top of the dialog (review minor 2).
      const code = (err as { code?: string })?.code;
      if (code === 'INVALID_CREDENTIAL_ID') {
        setIdInvalid(true);
        setActionError(t('credentials.errorBadId'));
      } else if (code === 'CREDENTIAL_EXISTS' || (err as { statusCode?: number })?.statusCode === 409) {
        setReferenceTaken(true);
        setActionError(t('credentials.errorExists'));
      } else {
        setActionError(err instanceof Error || code ? formatApiError(err, t) : t('credentials.registerFailed'));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleAddMember = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newPrincipalId.trim()) return;

    try {
      setMemberSubmitting(true);
      setActionError(null);
      await addMember(newPrincipalId.trim(), newRole);
      setNewPrincipalId('');
    } catch (err: any) {
      setActionError(err?.message || t('credentials.memberFailed'));
    } finally {
      setMemberSubmitting(false);
    }
  };

  const statusVariant = (status: string) =>
    status === 'revoked' || status === 'invalid' ? 'danger' : status === 'expired' ? 'warning' : status === 'active' ? 'success' : 'neutral';
  const roleLabel = (role: WorkspaceRole) => t(`credentialLabels.role.${role}`);
  const matrix: { key: string; admin: boolean; editor: boolean; viewer: boolean }[] = [
    { key: 'ui.executeWorkflowsRun', admin: true, editor: true, viewer: false },
    { key: 'ui.modifyWorkflowsModulesEdit', admin: true, editor: true, viewer: false },
    { key: 'ui.approvalGatesDecisionApprove', admin: true, editor: false, viewer: false },
    { key: 'ui.registerDeleteCredentials', admin: true, editor: true, viewer: false },
    { key: 'ui.revokeCredentialsFailClosed', admin: true, editor: false, viewer: false },
    { key: 'ui.manageMembersRolesAdmin', admin: true, editor: false, viewer: false },
  ];
  const storageHelp = (
    <details className="resource-help-disclosure" data-testid="credential-storage-help">
      <summary>{t('ui.zeroSecretVaultArchitecture')}</summary>
      <p>{t('credentials.storageHelp')}</p>
    </details>
  );
  const tabs = [
    { id: 'credentials', label: `${t('credentials.tabCredentials')} (${credentials.length})` },
    ...(membersAvailable ? [{ id: 'members', label: `${t('credentials.tabMembers')} (${members.length})` }] : []),
    { id: 'matrix', label: t('credentials.tabMatrix') },
  ];
  const activeLabel = tabs.find((tab) => tab.id === activeTab)?.label;

  return (
    <PageLayout data-testid="credentials-view">
      <PageHeader
        title={t('credentials.title')}
        metadata={
          <span>
            {t('credentials.yourRole')}{' '}
            <span data-testid="current-role-badge">{roleLabel(myMembership.role)}</span>
            {' · '}
            {myMembership.profile === 'authenticated-shared-service' ? t('ui.sharedService') : t('ui.localOwner')}
          </span>
        }
        description={t('credentials.subtitle')}
        actions={
          activeTab === 'credentials' ? (
            <Button
              ref={addButtonRef}
              variant="primary"
              data-testid="register-credential-button"
              disabled={isViewer || loading}
              onClick={handleOpenModal}
            >
              {t('credentials.registerNew')}
            </Button>
          ) : undefined
        }
      />

      <div className="resource-notices">
        {isViewer && (
          <div className="inline-notice inline-notice--warning" data-testid="viewer-restriction-banner" role="note">
            <strong className="inline-notice-title">{t('ui.viewerModeReadOnlyAccess')}</strong>
            {t('credentials.viewerHelp')}
          </div>
        )}
        {error && !isViewer && !modalOpen && (
          <div role="alert" className="inline-notice inline-notice--danger">
            <span>{error}</span>
          </div>
        )}
        {actionError && !modalOpen && (
          <div role="alert" className="inline-notice inline-notice--danger">
            <span>{actionError}</span>
          </div>
        )}
      </div>

      <WorkflowTabs tabs={tabs} active={activeTab} onChange={(id) => setActiveTab(id as typeof activeTab)} label={t('credentials.tabsLabel')} panelId={panelId} />

      <div role="tabpanel" id={panelId} aria-label={activeLabel} tabIndex={0} style={{ minWidth: 0 }}>
        {activeTab === 'credentials' && (
          <>
            <div style={{ marginBottom: 16 }}>{storageHelp}</div>
            {credentials.length === 0 ? (
              <p className="inspection-empty">{t('credentials.noCredentials')}</p>
            ) : (
              <div className="inspection-table-scroll" role="region" aria-label={t('credentials.registeredCredentials')} tabIndex={0}>
                <table className="inspection-table settings-table settings-table--credentials settings-table--stack" role="table">
                  <caption className="studio-sr-only">{t('credentials.registeredCredentials')}</caption>
                  <thead>
                    <tr role="row">
                      <th role="columnheader" scope="col">{t('ui.credentialId')}</th>
                      <th role="columnheader" scope="col">{t('ui.kindType')}</th>
                      <th role="columnheader" scope="col">{t('ui.status')}</th>
                      <th role="columnheader" scope="col">{t('credentials.maskedValue')}</th>
                      <th role="columnheader" scope="col">{t('ui.envFallback')}</th>
                      <th role="columnheader" scope="col">{t('ui.verificationStatus')}</th>
                      <th role="columnheader" scope="col"><span className="studio-sr-only">{t('ui.actions')}</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {credentials.map((cred) => {
                      const verification = verificationMap[cred.credential_id];
                      return (
                        <tr role="row" key={cred.credential_id} data-testid={`cred-row-${cred.credential_id}`}>
                          <td role="cell" data-label={t('ui.credentialId')}>
                            <code data-testid={`cred-id-${cred.credential_id}`} className="settings-table-name">{cred.credential_id}</code>
                            {cred.description && <div className="resource-help">{cred.description}</div>}
                          </td>
                          <td role="cell" data-label={t('ui.kindType')}>
                            <code>{cred.credential_type}</code>
                            <div className="resource-help">{t(`credentialLabels.provider.${cred.provider}`, cred.provider)}</div>
                          </td>
                          <td role="cell" data-label={t('ui.status')}>
                            <span className={`run-status run-status--${statusVariant(cred.status)}`} data-testid={`status-${cred.credential_id}`}>
                              <span aria-hidden="true">●</span> {t(`credentialLabels.status.${cred.status}`, cred.status)}
                            </span>
                          </td>
                          <td role="cell" data-label={t('credentials.maskedValue')}>
                            <code data-testid={`cred-hint-${cred.credential_id}`}>{cred.masked_hint || '—'}</code>
                          </td>
                          <td role="cell" data-label={t('ui.envFallback')}>{cred.environment_variable ? <code>${cred.environment_variable}</code> : '—'}</td>
                          <td role="cell" data-label={t('ui.verificationStatus')}>
                            {verification ? (
                              <>
                                <span className={`run-status run-status--${statusVariant(verification.status)}`} data-testid={`verification-badge-${cred.credential_id}`}>
                                  <span aria-hidden="true">●</span> {verification.status === 'stored' || verification.status === 'valid' ? t('livefixUi.savedUnverified') : t(`credentialLabels.status.${verification.status}`, verification.status)}
                                </span>
                                <div className="resource-help">{verification.status === 'stored' || verification.status === 'valid' ? t('livefixUi.providerUnchecked') : verification.message}</div>
                              </>
                            ) : cred.status === 'revoked' ? (
                              // Revoking clears this session's result; an earlier check time says nothing now.
                              <span className="resource-help" data-testid={`verification-none-${cred.credential_id}`}>—</span>
                            ) : cred.last_verified_at ? (
                              <span className="resource-help">
                                {t('ui.verified') + ' '}
                                {new Date(cred.last_verified_at * 1000).toLocaleTimeString(locale)}
                              </span>
                            ) : (
                              <span className="resource-help">{t('ui.notYetChecked')}</span>
                            )}
                          </td>
                          <td role="cell" data-label={t('ui.actions')}>
                            <div className="settings-row-actions">
                              <Button
                                size="sm"
                                data-testid={`verify-cred-${cred.credential_id}`}
                                aria-label={`${t('ui.verify')} ${cred.credential_id}`}
                                onClick={() => verifyCredential(cred.credential_id)}
                              >
                                {t('ui.verify')}
                              </Button>
                              {isAdmin && cred.status !== 'revoked' && (
                                <InlineConfirm key={`revoke-${cred.credential_id}`} consequence={t('confirmation.revokeCredential')}>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    className="workflow-danger-action"
                                    data-testid={`revoke-cred-${cred.credential_id}`}
                                    aria-label={`${t('ui.revoke')} ${cred.credential_id}`}
                                    onClick={() => revokeCredential(cred.credential_id)}
                                  >
                                    {t('ui.revoke')}
                                  </Button>
                                </InlineConfirm>
                              )}
                              <InlineConfirm key={`delete-${cred.credential_id}`} consequence={t('confirmation.deleteCredential')}>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  className="workflow-danger-action"
                                  data-testid={`delete-cred-${cred.credential_id}`}
                                  aria-label={`${t('ui.delete')} ${cred.credential_id}`}
                                  disabled={isViewer}
                                  onClick={() => deleteCredential(cred.credential_id)}
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
          </>
        )}

        {membersAvailable && activeTab === 'members' && (
          <>
            <p className="knowledge-panel-help">
              {t('ui.principalsAuthorizedToOperateWithinWorkspace') + ' '}
              <code>{myMembership.workspace_id}</code>
            </p>
            {isAdmin && (
              <form onSubmit={handleAddMember} className="resource-inline-form" style={{ marginBottom: 24 }} aria-label={t('ui.addPrincipalToWorkspace')}>
                <div className="resource-field">
                  <label htmlFor={`${ids}-principal`}>{t('ui.principalUserId')}</label>
                  <input
                    id={`${ids}-principal`}
                    value={newPrincipalId}
                    onChange={(e) => setNewPrincipalId(e.target.value)}
                    placeholder={t('ui.eGDevSarahOrSrvIndexer')}
                    data-testid="new-member-id-input"
                    required
                  />
                </div>
                <div className="resource-field" style={{ flex: '0 1 180px' }}>
                  <label htmlFor={`${ids}-role`}>{t('ui.role')}</label>
                  <select id={`${ids}-role`} value={newRole} onChange={(e) => setNewRole(e.target.value as WorkspaceRole)} data-testid="new-member-role-select">
                    <option value="admin">{t('ui.admin')}</option>
                    <option value="editor">{t('ui.editor')}</option>
                    <option value="viewer">{t('ui.viewer')}</option>
                  </select>
                </div>
                <Button type="submit" variant="primary" data-testid="add-member-button" loading={memberSubmitting} disabled={!newPrincipalId.trim()}>
                  {t('ui.addMember')}
                </Button>
              </form>
            )}
            <div className="inspection-table-scroll" role="region" aria-label={t('credentials.tabMembers')} tabIndex={0}>
              <table className="inspection-table settings-table settings-table--stack" role="table">
                <caption className="studio-sr-only">{t('credentials.tabMembers')}</caption>
                <thead>
                  <tr role="row">
                    <th role="columnheader" scope="col">{t('ui.principalId')}</th>
                    <th role="columnheader" scope="col">{t('ui.role')}</th>
                    <th role="columnheader" scope="col">{t('ui.permissions')}</th>
                    <th role="columnheader" scope="col">{t('ui.joinedAt')}</th>
                    {isAdmin && <th role="columnheader" scope="col"><span className="studio-sr-only">{t('ui.actions')}</span></th>}
                  </tr>
                </thead>
                <tbody>
                  {members.map((member) => (
                    <tr role="row" key={member.principal_id} data-testid={`member-row-${member.principal_id}`}>
                      <td role="cell" data-label={t('ui.principalId')}>
                        <code data-testid={`member-id-${member.principal_id}`}>{member.principal_id}</code>
                        {member.principal_id === myMembership.principal_id && <span className="resource-help"> {t('ui.you2')}</span>}
                      </td>
                      <td role="cell" data-label={t('ui.role')}>{roleLabel(member.role)}</td>
                      <td role="cell" data-label={t('ui.permissions')} className="resource-help">{ROLE_DEFINITIONS[member.role].permissions.join(', ')}</td>
                      <td role="cell" data-label={t('ui.joinedAt')}>{new Date(member.joined_at * 1000).toLocaleDateString(locale)}</td>
                      {isAdmin && (
                        <td role="cell" data-label={t('ui.actions')}>
                          <div className="settings-row-actions">
                            <select
                              value={member.role}
                              aria-label={`${t('ui.role')} ${member.principal_id}`}
                              onChange={(e) => updateMemberRole(member.principal_id, e.target.value as WorkspaceRole)}
                              data-testid={`role-select-${member.principal_id}`}
                            >
                              <option value="admin">{t('ui.admin')}</option>
                              <option value="editor">{t('ui.editor')}</option>
                              <option value="viewer">{t('ui.viewer')}</option>
                            </select>
                            <InlineConfirm key={member.principal_id} consequence={t('confirmation.removeMember')}>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="workflow-danger-action"
                                data-testid={`remove-member-${member.principal_id}`}
                                onClick={() => removeMember(member.principal_id)}
                              >
                                {t('ui.remove')}
                              </Button>
                            </InlineConfirm>
                          </div>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        {activeTab === 'matrix' && (
          <div className="inspection-table-scroll" role="region" aria-label={t('ui.authoritativeRolePermissionMatrix')} tabIndex={0}>
            <table className="inspection-table settings-table settings-table--stack" role="table">
              <caption className="studio-sr-only">{t('ui.authoritativeRolePermissionMatrix')}</caption>
              <thead>
                <tr role="row">
                  <th role="columnheader" scope="col">{t('ui.capabilityAction')}</th>
                  <th role="columnheader" scope="col">{t('ui.admin')}</th>
                  <th role="columnheader" scope="col">{t('ui.editor')}</th>
                  <th role="columnheader" scope="col">{t('ui.viewer')}</th>
                </tr>
              </thead>
              <tbody>
                {matrix.map((row) => (
                  <tr role="row" key={row.key}>
                    <th role="rowheader" scope="row" style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{t(row.key)}</th>
                    {(['admin', 'editor', 'viewer'] as const).map((role) => (
                      <td role="cell" data-label={t(`ui.${role}`)} key={role} style={row[role] ? undefined : { color: 'var(--text-muted)' }}>
                        {row[role] ? t('ui.yes') : t('ui.denied')}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Modal
        open={modalOpen}
        onClose={handleCloseModal}
        title={t('ui.registerCredentialReference')}
        maxWidth={640}
        footer={
          <>
            <Button type="button" variant="ghost" onClick={handleCloseModal}>
              {t('ui.cancel')}
            </Button>
            <Button
              type="submit"
              form={`${ids}-credential-form`}
              variant="primary"
              data-testid="submit-credential-button"
              loading={submitting}
              disabled={!formId.trim() || !hasSecret}
            >
              {t('ui.registerSecurely')}
            </Button>
          </>
        }
      >
        <form id={`${ids}-credential-form`} onSubmit={handleCreateCredential} data-testid="register-credential-form" className="resource-form" style={{ gap: 16 }}>
          {actionError && (
            <div role="alert" id={`${ids}-form-error`} className="inline-notice inline-notice--danger" data-testid="credential-form-error">
              <span>{actionError}</span>
            </div>
          )}
          {storageHelp}
          <Input
            label={t('ui.credentialId')}
            value={formId}
            onChange={(e) => {
              setFormId(e.target.value);
              setReferenceTaken(false);
              setIdInvalid(false);
            }}
            aria-invalid={referenceTaken || idInvalid || undefined}
            aria-describedby={referenceTaken || idInvalid ? `${ids}-form-error` : undefined}
            placeholder={t('ui.eGOpenaiProdOrWhatsappApi')}
            helperText={t('ui.credentialIdHelp')}
            data-testid="cred-id-input"
            required
          />
          <div className="resource-field">
            <label htmlFor={`${ids}-type`}>{t('ui.credentialKindType')}</label>
            <select id={`${ids}-type`} value={formType} onChange={(e) => setFormType(e.target.value as CredentialType)} data-testid="cred-type-select">
              {CREDENTIAL_TYPES.map((kind) => (
                <option key={kind.id} value={kind.id}>
                  {t(`credentialLabels.type.${kind.id}`, kind.label)}
                </option>
              ))}
              {extraTypes.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div className="resource-field">
            <label htmlFor={`${ids}-provider`}>{t('ui.serviceProvider')}</label>
            <select id={`${ids}-provider`} value={formProvider} onChange={(e) => setFormProvider(e.target.value as CredentialProvider)} data-testid="cred-provider-select">
              {CREDENTIAL_PROVIDERS.map((p) => (
                <option key={p.id} value={p.id}>
                  {t(`credentialLabels.provider.${p.id}`, p.label)}
                </option>
              ))}
            </select>
          </div>
          <Input
            label={`${t('ui.description')} (${t('credentials.optional')})`}
            value={formDesc}
            onChange={(e) => setFormDesc(e.target.value)}
            placeholder={t('ui.scopePurposeOrEnvironment')}
            data-testid="cred-desc-input"
          />
          <div className="resource-field">
            <label htmlFor={`${ids}-secret`}>{t('ui.secretValueApiKeyToken')}</label>
            <input
              ref={secretRef}
              id={`${ids}-secret`}
              type="password"
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setHasSecret(e.target.value.trim().length > 0)}
              placeholder={t('ui.pasteRawSecretToken')}
              aria-describedby={`${ids}-secret-help`}
              data-testid="cred-secret-input"
              required
            />
            <span id={`${ids}-secret-help`} className="resource-help">{t('ui.secretHelp')}</span>
          </div>
          <Input
            label={t('ui.environmentVariableFallbackOptional')}
            value={formEnv}
            onChange={(e) => setFormEnv(e.target.value)}
            placeholder={t('ui.eGOpenaiApiKey')}
            helperText={t('ui.envHelp')}
            data-testid="cred-env-input"
          />
        </form>
      </Modal>
    </PageLayout>
  );
}
