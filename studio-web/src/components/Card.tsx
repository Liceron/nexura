// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React from 'react';

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  elevated?: boolean;
}

export function Card({ elevated = false, children, style, className, ...props }: CardProps) {
  return (
    <div
      className={['studio-card', className].filter(Boolean).join(' ')}
      style={{
        backgroundColor: elevated ? 'var(--bg-surface-elevated)' : 'var(--bg-surface)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--radius-lg)',
        padding: '20px',
        ...style,
        boxShadow: 'none',
      }}
      {...props}
    >
      {children}
    </div>
  );
}
