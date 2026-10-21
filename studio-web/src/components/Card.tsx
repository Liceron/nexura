// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React from 'react';

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  elevated?: boolean;
}

export function Card({ elevated = false, children, style, ...props }: CardProps) {
  return (
    <div
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
