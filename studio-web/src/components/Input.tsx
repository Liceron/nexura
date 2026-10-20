// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React from 'react';

/** Shared theme-aware select appearance, matching the credential provider control. */
export const selectStyle: React.CSSProperties = {
  padding: '8px 10px',
  borderRadius: 'var(--radius-md)',
  backgroundColor: 'var(--bg-surface-elevated)',
  border: '1px solid var(--border-subtle)',
  color: 'var(--text-primary)',
  fontSize: 13,
};

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  helperText?: string;
}

export function Input({ label, error, helperText, id, style, ...props }: InputProps) {
  const inputId = id || (label ? label.toLowerCase().replace(/\s+/g, '-') : undefined);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
      {label && (
        <label
          htmlFor={inputId}
          style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)' }}
        >
          {label}
        </label>
      )}
      <input
        id={inputId}
        style={{
          padding: '8px 12px',
          borderRadius: 'var(--radius-md)',
          border: `1px solid ${error ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
          backgroundColor: 'var(--bg-canvas)',
          color: 'var(--text-primary)',
          outline: 'none',
          transition: 'border-color 0.15s ease',
          ...style,
        }}
        {...props}
      />
      {error && (
        <span style={{ fontSize: 12, color: 'var(--status-danger)' }}>{error}</span>
      )}
      {!error && helperText && (
        <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{helperText}</span>
      )}
    </div>
  );
}
