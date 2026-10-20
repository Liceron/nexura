// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React from 'react';

export type BadgeVariant = 'success' | 'warning' | 'danger' | 'info' | 'neutral';

interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
  icon?: React.ReactNode;
}

export function Badge({
  variant = 'neutral',
  icon,
  children,
  style,
  ...props
}: BadgeProps) {
  const variantStyles: Record<BadgeVariant, React.CSSProperties> = {
    success: {
      backgroundColor: 'var(--status-success-bg)',
      color: 'var(--status-success)',
      border: '1px solid rgba(16, 185, 129, 0.2)',
    },
    warning: {
      backgroundColor: 'var(--status-warning-bg)',
      color: 'var(--status-warning)',
      border: '1px solid rgba(245, 158, 11, 0.2)',
    },
    danger: {
      backgroundColor: 'var(--status-danger-bg)',
      color: 'var(--status-danger)',
      border: '1px solid rgba(239, 68, 68, 0.2)',
    },
    info: {
      backgroundColor: 'var(--status-info-bg)',
      color: 'var(--status-info)',
      border: '1px solid rgba(14, 165, 233, 0.2)',
    },
    neutral: {
      backgroundColor: 'var(--bg-muted)',
      color: 'var(--text-secondary)',
      border: '1px solid var(--border-subtle)',
    },
  };

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '2px 8px',
        borderRadius: 'var(--radius-sm)',
        fontSize: 12,
        fontWeight: 500,
        ...variantStyles[variant],
        ...style,
      }}
      {...props}
    >
      {icon}
      {children}
    </span>
  );
}
