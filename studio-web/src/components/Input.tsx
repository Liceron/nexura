// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { useId } from 'react';

/** Shared theme-aware select appearance, matching the credential provider control. */
export const selectStyle: React.CSSProperties = {
  padding: '8px 10px',
  borderRadius: 'var(--radius-md)',
  backgroundColor: 'var(--bg-surface-elevated)',
  border: '1px solid var(--border-strong)',
  color: 'var(--text-primary)',
  fontSize: 'var(--type-ui)',
  lineHeight: 'var(--leading-ui)',
  minHeight: 'var(--field-height)',
  maxWidth: '100%',
};

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  helperText?: string;
}

export function Input({ label, error, helperText, id, style, 'aria-describedby': describedBy, ...props }: InputProps) {
  const generatedId = useId();
  const inputId = id || generatedId;
  const helpId = `${inputId}-help`;
  const errorId = `${inputId}-error`;

  return (
    <div className="studio-field" style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 16, minWidth: 0 }}>
      {label && (
        <label
          htmlFor={inputId}
          style={{ fontSize: 'var(--type-label)', lineHeight: 'var(--leading-label)', fontWeight: 500, color: 'var(--text-secondary)' }}
        >
          {label}
        </label>
      )}
      <input
        id={inputId}
        style={{
          padding: '8px 12px',
          borderRadius: 'var(--radius-md)',
          border: `1px solid ${error ? 'var(--status-danger)' : 'var(--border-strong)'}`,
          backgroundColor: 'var(--bg-surface-elevated)',
          color: 'var(--text-primary)',
          minHeight: 'var(--field-height)',
          minWidth: 0,
          width: '100%',
          transition: 'border-color var(--motion-fast) var(--motion-ease)',
          ...style,
        }}
        {...props}
        aria-invalid={error ? true : props['aria-invalid']}
        aria-describedby={[describedBy, error ? errorId : helperText ? helpId : undefined].filter(Boolean).join(' ') || undefined}
      />
      {error && (
        <span id={errorId} style={{ fontSize: 'var(--type-meta)', lineHeight: 'var(--leading-meta)', color: 'var(--status-danger)' }}>{error}</span>
      )}
      {!error && helperText && (
        <span id={helpId} style={{ fontSize: 'var(--type-meta)', lineHeight: 'var(--leading-meta)', color: 'var(--text-muted)' }}>{helperText}</span>
      )}
    </div>
  );
}
