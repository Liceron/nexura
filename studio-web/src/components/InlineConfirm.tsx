// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { cloneElement, useState, type ReactElement, type ButtonHTMLAttributes } from 'react';
import { Button } from './Button';
import { useT } from '../i18n';

interface InlineConfirmProps {
  consequence: string;
  children: ReactElement<ButtonHTMLAttributes<HTMLButtonElement> & { loading?: boolean; 'data-testid'?: string }>;
}

/** Keep the original action's label, permissions and handler; require a second click. */
export function InlineConfirm({ consequence, children }: InlineConfirmProps) {
  const t = useT();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const disabled = children.props.disabled || children.props.loading || busy;

  if (!confirming) return cloneElement(children, {
    onClick: (event) => {
      event.stopPropagation();
      setFailed(false);
      setConfirming(true);
    },
  });

  return (
    <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }} onClick={(event) => event.stopPropagation()}>
      <span role="status" style={{ fontSize: 12 }}>{consequence}</span>
      <Button
        type="button"
        variant="danger"
        size="sm"
        disabled={disabled}
        loading={busy}
        data-testid={children.props['data-testid'] ? `${children.props['data-testid']}-confirm` : undefined}
        onClick={async (event) => {
          setBusy(true);
          setFailed(false);
          try {
            await children.props.onClick?.(event);
            setConfirming(false);
          } catch {
            setFailed(true);
          } finally {
            setBusy(false);
          }
        }}
      >
        {t('common.confirm', 'Confirm')}
      </Button>
      <Button type="button" size="sm" disabled={busy} onClick={() => setConfirming(false)}>
        {t('common.cancel', 'Cancel')}
      </Button>
      {failed && <span role="alert">{t('confirmation.failed', 'Action failed. Please try again.')}</span>}
    </span>
  );
}
