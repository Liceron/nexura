// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useT } from '../i18n';
export function Spinner({ size = 16 }: { size?: number }) {
  const t = useT();
  return (
    <svg
      role="status"
      aria-label={t('ui.loading')}
      style={{
        width: size,
        height: size,
        animation: 'spin 1s linear infinite',
        display: 'inline-block',
        verticalAlign: 'middle',
      }}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
    >
      <style>
        {`@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}
      </style>
      <circle cx="12" cy="12" r="10" strokeOpacity="0.25" />
      <path d="M12 2a10 10 0 0 1 10 10" strokeLinecap="round" />
    </svg>
  );
}
