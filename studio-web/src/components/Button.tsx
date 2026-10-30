// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import React from 'react';
import { Spinner } from './Spinner';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  loading?: boolean;
}

/** Forwards its ref so callers can restore focus to the button after a disclosure closes. */
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  disabled,
  children,
  style,
  className,
  ...props
}, ref) {
  const baseStyle: React.CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
    borderRadius: 'var(--radius-md)',
    fontWeight: 500,
    fontSize: size === 'lg' ? 'var(--type-body)' : 'var(--type-ui)',
    lineHeight: 'var(--leading-ui)',
    minHeight: size === 'sm' ? 28 : size === 'lg' ? 40 : 'var(--control-height)',
    padding: size === 'sm' ? '4px 10px' : size === 'lg' ? '10px 20px' : '6px 14px',
    transition: 'background-color var(--motion-fast) var(--motion-ease), color var(--motion-fast) var(--motion-ease)',
    cursor: disabled || loading ? 'not-allowed' : 'pointer',
    ...style,
  };

  const variantStyles: Record<string, React.CSSProperties> = {
    primary: {
      backgroundColor: 'var(--button-primary)',
      color: 'var(--button-primary-text)',
    },
    secondary: {
      backgroundColor: 'var(--bg-surface-elevated)',
      color: 'var(--text-primary)',
      border: '1px solid var(--border-strong)',
    },
    danger: {
      backgroundColor: 'var(--button-danger)',
      color: 'var(--button-danger-text)',
    },
    ghost: {
      backgroundColor: 'transparent',
      color: 'var(--text-secondary)',
    },
  };

  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={['studio-button', `studio-button--${variant}`, className].filter(Boolean).join(' ')}
      data-size={size}
      style={{ ...baseStyle, ...variantStyles[variant] }}
      {...props}
    >
      {loading && <Spinner size={size === 'sm' ? 12 : 14} />}
      {children}
    </button>
  );
});
