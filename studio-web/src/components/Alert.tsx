// SPDX-License-Identifier: MIT
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
    info: { bg: 'rgba(56, 189, 248, 0.1)', border: 'rgba(56, 189, 248, 0.3)', text: '#38bdf8' },
    warning: { bg: 'rgba(251, 191, 36, 0.1)', border: 'rgba(251, 191, 36, 0.3)', text: '#fbbf24' },
    danger: { bg: 'rgba(248, 113, 113, 0.1)', border: 'rgba(248, 113, 113, 0.3)', text: '#f87171' },
    success: { bg: 'rgba(74, 222, 128, 0.1)', border: 'rgba(74, 222, 128, 0.3)', text: '#4ade80' },
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
        fontSize: 13,
        lineHeight: 1.5,
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
