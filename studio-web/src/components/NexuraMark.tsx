// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
/** Canonical checkpoint N geometry. Public/Studio copies are generated and checked. */
export function NexuraMark({ variant = 'static', className = '' }: {
  variant?: 'static' | 'intro' | 'working';
  className?: string;
}) {
  return <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"
    className={`nexura-mark nexura-mark--${variant} ${className}`} aria-hidden="true" focusable="false">
    <path className="nexura-path nexura-path--before" pathLength="1" d="M4 19V5L9 10" />
    <path className="nexura-gate" pathLength="1" d="M10 10H14V14H10Z" />
    <path className="nexura-path nexura-path--after" pathLength="1" d="M15 14L20 19V5" />
  </svg>;
}
