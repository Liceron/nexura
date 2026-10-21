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
    },
    warning: {
      backgroundColor: 'var(--status-warning-bg)',
      color: 'var(--status-warning)',
    },
    danger: {
      backgroundColor: 'var(--status-danger-bg)',
      color: 'var(--status-danger)',
    },
    info: {
      backgroundColor: 'var(--status-info-bg)',
      color: 'var(--status-info)',
    },
    neutral: {
      backgroundColor: 'var(--bg-muted)',
      color: 'var(--text-secondary)',
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
        fontSize: 'var(--type-meta)',
        lineHeight: 'var(--leading-meta)',
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
