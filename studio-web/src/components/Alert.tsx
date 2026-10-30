// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import React from 'react';

export interface AlertProps {
  variant?: 'info' | 'warning' | 'danger' | 'success';
  title?: string;
  children: React.ReactNode;
  style?: React.CSSProperties;
  'data-testid'?: string;
}

export function Alert({ variant = 'info', title, children, style, 'data-testid': testId }: AlertProps) {
  const colors = {
    info: { bg: 'var(--status-info-bg)', border: 'var(--border-subtle)', text: 'var(--status-info)' },
    warning: { bg: 'var(--status-warning-bg)', border: 'var(--status-warning)', text: 'var(--status-warning)' },
    danger: { bg: 'var(--status-danger-bg)', border: 'var(--status-danger)', text: 'var(--status-danger)' },
    success: { bg: 'var(--status-success-bg)', border: 'var(--status-success)', text: 'var(--status-success)' },
  }[variant];

  return (
    <div
      data-testid={testId}
      style={{
        padding: '12px 16px',
        borderRadius: 'var(--radius-md)',
        backgroundColor: colors.bg,
        border: `1px solid ${colors.border}`,
        color: 'var(--text-primary)',
        fontSize: 'var(--type-ui)',
        lineHeight: 'var(--leading-ui)',
        ...style,
      }}
    >
      {title && (
        <div style={{ fontWeight: 600, color: colors.text, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
          {title}
        </div>
      )}
      <div>{children}</div>
    </div>
  );
}
