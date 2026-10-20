// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { selectStyle } from '../../components/Input';
import { InlineConfirm } from '../../components/InlineConfirm';
import { Icon } from '../../components/Icon';
import React, { useEffect, useState } from 'react';
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
import { Badge, BadgeVariant } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Input } from '../../components/Input';
import { Modal } from '../../components/Modal';
import { Alert } from '../../components/Alert';
import { useI18n } from '../../i18n';

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

  // Transient form state — cleared immediately upon submit/close
  const [formId, setFormId] = useState('');
  const [formType, setFormType] = useState<CredentialType>('api_key');
  const [formProvider, setFormProvider] = useState<CredentialProvider>('openai');
  const [formDesc, setFormDesc] = useState('');
  const [formSecret, setFormSecret] = useState('');
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
    setFormSecret('');
    setFormEnv('');
    setActionError(null);
    setModalOpen(true);
  };

  const handleCloseModal = () => {
    // SECURITY: Purge transient secret immediately
    setFormSecret('');
    setModalOpen(false);
  };

  const handleCreateCredential = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formId.trim() || !formSecret.trim()) return;

    try {
      setSubmitting(true);
      setActionError(null);
      await createCredential({
        credential_id: formId.trim(),
        credential_type: formType,
        provider: formProvider,
        description: formDesc.trim(),
        secret: formSecret.trim(),
        environment_variable: formEnv.trim() || undefined,
      });
      // SECURITY: Immediate zeroing of secret in state
      setFormSecret('');
      setModalOpen(false);
    } catch (err: any) {
      setActionError(err?.message || t('credentials.registerFailed'));
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

  const roleBadgeVariant: Record<WorkspaceRole, BadgeVariant> = {
    admin: 'success',
    editor: 'info',
    viewer: 'neutral',
  };

  const statusBadgeVariant: Record<string, BadgeVariant> = {
    active: 'success',
    revoked: 'danger',
    expired: 'warning',
  };

  return (
    <div data-testid="credentials-view" style={{ padding: '24px 32px', maxWidth: 1280, margin: '0 auto' }}>
      {/* Header & Role/Profile Bar */}
      <div className="studio-mobile-wrap" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20, rowGap: 12 }}>
        <div className="studio-mobile-content">
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 600 }}>
            {t('credentials.title', 'Credential References & Access Control')}
          </h1>
          <p style={{ margin: '6px 0 0', color: 'var(--text-secondary)', fontSize: 14 }}>
            {t('credentials.subtitle', 'Workspace-scoped secret references, role-based capabilities, and identity governance.')}
          </p>
        </div>

        <div className="studio-mobile-wrap" style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <Badge variant="neutral">
            <Icon name="organisation" /> {t('credentials.workspace', 'Workspace')}: <strong>{myMembership.workspace_id}</strong>
          </Badge>
          <Badge variant={myMembership.profile === 'authenticated-shared-service' ? 'warning' : 'neutral'}>
            <Icon name="globe" /> {myMembership.profile === 'authenticated-shared-service' ? t('ui.sharedService') : t('ui.localOwner')}
          </Badge>
          <Badge variant={roleBadgeVariant[myMembership.role]} data-testid="current-role-badge">
            <Icon name="user" /> {t(`credentialLabels.role.${myMembership.role}`)}
          </Badge>
        </div>
      </div>

      {/* Security & Secrecy Notice */}
      <Alert
        variant="info"
        title={t('ui.zeroSecretVaultArchitecture')}
        style={{ marginBottom: 20 }}
      >
        {t('ui.rawSecretsAreSubmittedTransientlyOverSecureChannelsStoredExclusivelyInTheIsola') + " "}<strong>{t('ui.neverWrittenToLocalstorageBrowserCachesExportArchivesOrExecutionLogs')}</strong>{t('ui.clientComponentsOnlyHoldOpaqueReferences')}
      </Alert>

      {/* Viewer Restriction Alert */}
      {isViewer && (
        <Alert
          variant="warning"
          title={t('ui.viewerModeReadOnlyAccess')}
          style={{ marginBottom: 20 }}
          data-testid="viewer-restriction-banner"
        >
          {t('ui.yourCurrentIdentityHasThe') + " "}<strong>{t('ui.viewer')}</strong> {t('ui.roleInThisWorkspaceYouCanSeeMemberRostersButListingCreatingRevokingOrDeletin')}
        </Alert>
      )}

      {error && !isViewer && (
        <Alert variant="danger" title={t('ui.error')} style={{ marginBottom: 20 }}>
          {error}
        </Alert>
      )}

      {actionError && (
        <Alert variant="danger" title={t('ui.actionFailed')} style={{ marginBottom: 20 }}>
          {actionError}
        </Alert>
      )}

      {/* Tab Navigation */}
      <div role="tablist" style={{ display: 'flex', gap: 8, borderBottom: '1px solid var(--border-subtle)', marginBottom: 20 }}>
        <button
          data-testid="tab-credentials"
          role="tab"
          aria-selected={activeTab === 'credentials'}
          onClick={() => setActiveTab('credentials')}
          style={{
            padding: '10px 16px',
            border: 'none',
            background: 'none',
            cursor: 'pointer',
            fontSize: 14,
            fontWeight: 500,
            borderBottom: activeTab === 'credentials' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'credentials' ? 'var(--text-primary)' : 'var(--text-secondary)',
          }}
        >
          <Icon name="credentials" /> {t('credentials.tabCredentials', 'Credential References')} ({credentials.length})
        </button>
        {membersAvailable && <button
          data-testid="tab-members"
          role="tab"
          aria-selected={activeTab === 'members'}
          onClick={() => setActiveTab('members')}
          style={{
            padding: '10px 16px',
            border: 'none',
            background: 'none',
            cursor: 'pointer',
            fontSize: 14,
            fontWeight: 500,
            borderBottom: activeTab === 'members' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'members' ? 'var(--text-primary)' : 'var(--text-secondary)',
          }}
        >
          <Icon name="user" /> {t('credentials.tabMembers', 'Workspace Members')} ({members.length})
        </button>}
        <button
          data-testid="tab-matrix"
          role="tab"
          aria-selected={activeTab === 'matrix'}
          onClick={() => setActiveTab('matrix')}
          style={{
            padding: '10px 16px',
            border: 'none',
            background: 'none',
            cursor: 'pointer',
            fontSize: 14,
            fontWeight: 500,
            borderBottom: activeTab === 'matrix' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'matrix' ? 'var(--text-primary)' : 'var(--text-secondary)',
          }}
        >
          <Icon name="approvals" /> {t('credentials.tabMatrix', 'Role & Permission Matrix')}
        </button>
      </div>

      {/* Tab 1: Credential References */}
      {activeTab === 'credentials' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <h3 style={{ margin: 0, fontSize: 16 }}>{t('credentials.registeredCredentials', 'Registered Credential References')}</h3>
            <Button
              variant="primary"
              data-testid="register-credential-button"
              disabled={isViewer || loading}
              onClick={handleOpenModal}
            >
              <Icon name="plus" /> {t('credentials.registerNew', 'Register Credential Reference')}
            </Button>
          </div>

          {credentials.length === 0 ? (
            <div
              style={{
                padding: 40,
                textAlign: 'center',
                backgroundColor: 'var(--bg-surface)',
                borderRadius: 'var(--radius-lg)',
                border: '1px dashed var(--border-subtle)',
              }}
            >
              <p style={{ margin: '0 0 12px', color: 'var(--text-secondary)' }}>
                {t('credentials.noCredentials', 'No credential references registered in this workspace yet.')}
              </p>
              {!isViewer && (
                <Button variant="secondary" onClick={handleOpenModal}>
                  <Icon name="plus" /> {t('credentials.registerFirst', 'Register First Credential')}
                </Button>
              )}
            </div>
          ) : (
            <div style={{ overflowX: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: 13 }}>
                <thead>
                  <tr style={{ backgroundColor: 'var(--bg-surface-elevated)', borderBottom: '1px solid var(--border-subtle)' }}>
                    <th style={{ padding: '12px 16px' }}>{t('ui.credentialId')}</th>
                    <th style={{ padding: '12px 16px' }}>{t('ui.provider')}</th>
                    <th style={{ padding: '12px 16px' }}>{t('ui.kindType')}</th>
                    <th style={{ padding: '12px 16px' }}>{t('ui.status')}</th>
                    <th style={{ padding: '12px 16px' }}>{t('credentials.maskedValue', 'Stored value (masked)')}</th>
                    <th style={{ padding: '12px 16px' }}>{t('ui.envFallback')}</th>
                    <th style={{ padding: '12px 16px' }}>{t('ui.verificationStatus')}</th>
                    <th style={{ padding: '12px 16px', textAlign: 'right' }}>{t('ui.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {credentials.map((cred) => {
                    const verification = verificationMap[cred.credential_id];
                    return (
                      <tr
                        key={cred.credential_id}
                        data-testid={`cred-row-${cred.credential_id}`}
                        style={{ borderBottom: '1px solid var(--border-subtle)' }}
                      >
                        <td style={{ padding: '12px 16px', fontWeight: 600 }}>
                          <span data-testid={`cred-id-${cred.credential_id}`}>{cred.credential_id}</span>
                          {cred.description && (
                            <div style={{ fontSize: 11, color: 'var(--text-secondary)', fontWeight: 400 }}>
                              {cred.description}
                            </div>
                          )}
                        </td>
                        <td style={{ padding: '12px 16px' }}>
                          <Badge variant="neutral">
                            {cred.provider.toUpperCase()}
                          </Badge>
                        </td>
                        <td style={{ padding: '12px 16px' }}>
                          <code>{cred.credential_type}</code>
                        </td>
                        <td style={{ padding: '12px 16px' }}>
                          <Badge variant={statusBadgeVariant[cred.status] || 'neutral'} data-testid={`status-${cred.credential_id}`}>
                            {t(`credentialLabels.status.${cred.status}`, cred.status.toUpperCase())}
                          </Badge>
                        </td>
                        <td style={{ padding: '12px 16px' }}>
                          <code data-testid={`cred-hint-${cred.credential_id}`}>{cred.masked_hint || '—'}</code>
                        </td>
                        <td style={{ padding: '12px 16px', color: 'var(--text-secondary)' }}>
                          {cred.environment_variable ? <code>${cred.environment_variable}</code> : '—'}
                        </td>
                        <td style={{ padding: '12px 16px' }}>
                          {verification ? (
                            <div>
                              <Badge
                                variant={verification.status === 'valid' ? 'success' : 'danger'}
                                data-testid={`verification-badge-${cred.credential_id}`}
                              >
                                {t(`credentialLabels.status.${verification.status}`, verification.status.toUpperCase())}
                              </Badge>
                              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>
                                {verification.message}
                              </div>
                            </div>
                          ) : cred.last_verified_at ? (
                            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                              {t('ui.verified') + " "}{new Date(cred.last_verified_at * 1000).toLocaleTimeString(locale)}
                            </span>
                          ) : (
                            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{t('ui.notYetChecked')}</span>
                          )}
                        </td>
                        <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                          <div style={{ display: 'inline-flex', gap: 6 }}>
                            <Button
                              variant="secondary"
                              size="sm"
                              data-testid={`verify-cred-${cred.credential_id}`}
                              onClick={() => verifyCredential(cred.credential_id)}
                            >
                              <Icon name="check" /> {t('ui.verify')}
                            </Button>
                            {isAdmin && cred.status !== 'revoked' && (
                              <InlineConfirm key={`revoke-${cred.credential_id}`} consequence={t('confirmation.revokeCredential')}>
                                <Button
                                  variant="secondary"
                                  size="sm"
                                  data-testid={`revoke-cred-${cred.credential_id}`}
                                  onClick={() => revokeCredential(cred.credential_id)}
                                >
                                  <Icon name="stop" /> {t('ui.revoke')}
                                </Button>
                              </InlineConfirm>
                            )}
                            <InlineConfirm key={`delete-${cred.credential_id}`} consequence={t('confirmation.deleteCredential')}>
                              <Button
                                variant="danger"
                                size="sm"
                                data-testid={`delete-cred-${cred.credential_id}`}
                                disabled={isViewer}
                                onClick={() => deleteCredential(cred.credential_id)}
                              >
                                <Icon name="trash" /> {t('ui.delete')}
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
        </div>
      )}

      {/* Tab 2: Workspace Members */}
      {membersAvailable && activeTab === 'members' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20 }}>
            <div>
              <h3 style={{ margin: 0, fontSize: 16 }}>{t('ui.workspaceTeamMembership')}</h3>
              <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
                {t('ui.principalsAuthorizedToOperateWithinWorkspace') + " "}<code>{myMembership.workspace_id}</code>.
              </p>
            </div>
          </div>

          {isAdmin && (
            <div
              style={{
                padding: 16,
                marginBottom: 20,
                backgroundColor: 'var(--bg-surface-elevated)',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--border-subtle)',
              }}
            >
              <h4 style={{ margin: '0 0 12px', fontSize: 14 }}>{t('ui.addPrincipalToWorkspace')}</h4>
              <form onSubmit={handleAddMember} style={{ display: 'flex', gap: 12, alignItems: 'flex-end' }}>
                <div style={{ flex: 1 }}>
                  <Input
                    label={t('ui.principalUserId')}
                    value={newPrincipalId}
                    onChange={(e) => setNewPrincipalId(e.target.value)}
                    placeholder={t('ui.eGDevSarahOrSrvIndexer')}
                    data-testid="new-member-id-input"
                    required
                  />
                </div>
                <div style={{ width: 180 }}>
                  <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 4 }}>{t('ui.role')}</label>
                  <select
                    value={newRole}
                    onChange={(e) => setNewRole(e.target.value as WorkspaceRole)}
                    data-testid="new-member-role-select"
                    style={{
                      width: '100%',
                      padding: '8px 10px',
                      borderRadius: 'var(--radius-md)',
                      backgroundColor: 'var(--bg-surface)',
                      border: '1px solid var(--border-subtle)',
                      color: 'var(--text-primary)',
                      fontSize: 13,
                    }}
                  >
                    <option value="admin">{t('ui.admin')}</option>
                    <option value="editor">{t('ui.editor')}</option>
                    <option value="viewer">{t('ui.viewer')}</option>
                  </select>
                </div>
                <Button
                  type="submit"
                  variant="primary"
                  data-testid="add-member-button"
                  disabled={memberSubmitting || !newPrincipalId.trim()}
                >
                  <Icon name="plus" /> {t('ui.addMember')}
                </Button>
              </form>
            </div>
          )}

          <div style={{ overflowX: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: 13 }}>
              <thead>
                <tr style={{ backgroundColor: 'var(--bg-surface-elevated)', borderBottom: '1px solid var(--border-subtle)' }}>
                  <th style={{ padding: '12px 16px' }}>{t('ui.principalId')}</th>
                  <th style={{ padding: '12px 16px' }}>{t('ui.role')}</th>
                  <th style={{ padding: '12px 16px' }}>{t('ui.permissions')}</th>
                  <th style={{ padding: '12px 16px' }}>{t('ui.joinedAt')}</th>
                  {isAdmin && <th style={{ padding: '12px 16px', textAlign: 'right' }}>{t('ui.actions')}</th>}
                </tr>
              </thead>
              <tbody>
                {members.map((member) => (
                  <tr
                    key={member.principal_id}
                    data-testid={`member-row-${member.principal_id}`}
                    style={{ borderBottom: '1px solid var(--border-subtle)' }}
                  >
                    <td style={{ padding: '12px 16px', fontWeight: 600 }}>
                      <span data-testid={`member-id-${member.principal_id}`}>{member.principal_id}</span>
                      {member.principal_id === myMembership.principal_id && (
                        <span style={{ marginLeft: 8, fontSize: 11, color: 'var(--accent-primary)' }}>{t('ui.you2')}</span>
                      )}
                    </td>
                    <td style={{ padding: '12px 16px' }}>
                      <Badge variant={roleBadgeVariant[member.role]}>{t(`credentialLabels.roleUpper.${member.role}`)}</Badge>
                    </td>
                    <td style={{ padding: '12px 16px', color: 'var(--text-secondary)' }}>
                      {ROLE_DEFINITIONS[member.role].permissions.join(', ')}
                    </td>
                    <td style={{ padding: '12px 16px', color: 'var(--text-secondary)' }}>
                      {new Date(member.joined_at * 1000).toLocaleDateString(locale)}
                    </td>
                    {isAdmin && (
                      <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                        <div style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                          <select
                            value={member.role}
                            onChange={(e) => updateMemberRole(member.principal_id, e.target.value as WorkspaceRole)}
                            data-testid={`role-select-${member.principal_id}`}
                            style={{
                              padding: '4px 8px',
                              borderRadius: 'var(--radius-sm)',
                              backgroundColor: 'var(--bg-surface)',
                              border: '1px solid var(--border-subtle)',
                              fontSize: 12,
                              color: 'var(--text-primary)',
                            }}
                          >
                            <option value="admin">{t('ui.admin')}</option>
                            <option value="editor">{t('ui.editor')}</option>
                            <option value="viewer">{t('ui.viewer')}</option>
                          </select>
                          <InlineConfirm key={member.principal_id} consequence={t('confirmation.removeMember')} >
                            <Button
                              variant="danger"
                              size="sm"
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
        </div>
      )}

      {/* Tab 3: Security & Role Matrix */}
      {activeTab === 'matrix' && (
        <div>
          <h3 style={{ margin: '0 0 16px', fontSize: 16 }}>{t('ui.authoritativeRolePermissionMatrix')}</h3>
          <div style={{ overflowX: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: 13 }}>
              <thead>
                <tr style={{ backgroundColor: 'var(--bg-surface-elevated)', borderBottom: '1px solid var(--border-subtle)' }}>
                  <th style={{ padding: '12px 16px' }}>{t('ui.capabilityAction')}</th>
                  <th style={{ padding: '12px 16px' }}>{t('ui.admin')}</th>
                  <th style={{ padding: '12px 16px' }}>{t('ui.editor')}</th>
                  <th style={{ padding: '12px 16px' }}>{t('ui.viewer')}</th>
                </tr>
              </thead>
              <tbody>
                <tr style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                  <td style={{ padding: '12px 16px', fontWeight: 500 }}>{t('ui.executeWorkflowsRun')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                </tr>
                <tr style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                  <td style={{ padding: '12px 16px', fontWeight: 500 }}>{t('ui.modifyWorkflowsModulesEdit')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                </tr>
                <tr style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                  <td style={{ padding: '12px 16px', fontWeight: 500 }}>{t('ui.approvalGatesDecisionApprove')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                </tr>
                <tr style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                  <td style={{ padding: '12px 16px', fontWeight: 500 }}>{t('ui.registerDeleteCredentials')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                </tr>
                <tr style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                  <td style={{ padding: '12px 16px', fontWeight: 500 }}>{t('ui.revokeCredentialsFailClosed')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                </tr>
                <tr>
                  <td style={{ padding: '12px 16px', fontWeight: 500 }}>{t('ui.manageMembersRolesAdmin')}</td>
                  <td style={{ padding: '12px 16px' }}><Icon name="check" /> {t('ui.yes')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                  <td style={{ padding: '12px 16px', color: 'var(--text-muted)' }}><Icon name="close" /> {t('ui.denied')}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Register Credential Modal */}
      <Modal
        open={modalOpen}
        onClose={handleCloseModal}
        title={t('ui.registerCredentialReference')}
      >
        <form onSubmit={handleCreateCredential} data-testid="register-credential-form">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Input
              label={t('ui.credentialId')}
              value={formId}
              onChange={(e) => setFormId(e.target.value)}
              placeholder={t('ui.eGOpenaiProdOrWhatsappApi')}
              helperText={t('ui.credentialIdHelp')}
              data-testid="cred-id-input"
              required
            />

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 4 }}>
                {t('ui.credentialKindType')}
              </label>
              <select
                value={formType}
                onChange={(e) => setFormType(e.target.value as CredentialType)}
                data-testid="cred-type-select"
                style={{
                  width: '100%',
                  ...selectStyle,
                }}
              >
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

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 4 }}>
                {t('ui.serviceProvider')}
              </label>
              <select
                value={formProvider}
                onChange={(e) => setFormProvider(e.target.value as CredentialProvider)}
                data-testid="cred-provider-select"
                style={{
                  width: '100%',
                  ...selectStyle,
                }}
              >
                {CREDENTIAL_PROVIDERS.map((p) => (
                  <option key={p.id} value={p.id}>
                    {t(`credentialLabels.provider.${p.id}`, p.label)}
                  </option>
                ))}
              </select>
            </div>

            <Input
              label={t('ui.description')}
              value={formDesc}
              onChange={(e) => setFormDesc(e.target.value)}
              placeholder={t('ui.scopePurposeOrEnvironment')}
              data-testid="cred-desc-input"
            />

            <Input
              label={t('ui.secretValueApiKeyToken')}
              type="password"
              value={formSecret}
              onChange={(e) => setFormSecret(e.target.value)}
              placeholder={t('ui.pasteRawSecretToken')}
              helperText={t('ui.secretHelp')}
              data-testid="cred-secret-input"
              required
            />

            <Input
              label={t('ui.environmentVariableFallbackOptional')}
              value={formEnv}
              onChange={(e) => setFormEnv(e.target.value)}
              placeholder={t('ui.eGOpenaiApiKey')}
              helperText={t('ui.envHelp')}
              data-testid="cred-env-input"
            />

            {actionError && (
              <div style={{ color: 'var(--danger)', fontSize: 12 }}>
                {actionError}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 10 }}>
              <Button type="button" variant="secondary" onClick={handleCloseModal}>
                {t('ui.cancel')}
              </Button>
              <Button
                type="submit"
                variant="primary"
                data-testid="submit-credential-button"
                disabled={submitting || !formId.trim() || !formSecret.trim()}
              >
                {submitting ? t('ui.registering') : t('ui.registerSecurely')}
              </Button>
            </div>
          </div>
        </form>
      </Modal>
    </div>
  );
}
