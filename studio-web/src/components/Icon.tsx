// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/** Local stroke icons: no system glyphs, fonts or external assets required. */
const paths = {
  workflows: 'M13 2 4 14h7l-1 8 10-13h-7z',
  templates: 'M8 4H5v18h14V4h-3M9 2h6v4H9zM8 11h8M8 16h6',
  approvals: 'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6zM8 12l3 3 5-6',
  playground: 'M3 3h18v14H9l-6 4zM7 8h10M7 12h6',
  knowledge: 'M12 5C8 2 4 3 2 4v16c4-2 7-1 10 1 3-2 6-3 10-1V4c-2-1-6-2-10 1v16',
  skills: 'M3 4h6V2h6v2h6v6h-2v6h2v6h-6v-2H9v2H3v-6h2v-6H3z',
  agents: 'M12 2v3M4 5h16v15H4zM8 10h1M15 10h1M8 16h8M1 9v7M23 9v7',
  credentials: 'M14 3a5 5 0 1 0 0 10 5 5 0 0 0 0-10M10 12 2 20v2h4v-3h3v-3l3-3',
  tools: 'M14 3a6 6 0 0 0-7 8L2 18l4 4 7-7a6 6 0 0 0 8-7l-5 4-4-4z',
  evaluations: 'M8 2h8M9 2v7L3 20v2h18v-2L15 9V2M7 15h10',
  triggers: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l4 2M2 4l3-3M19 1l3 3',
  traces: 'M3 3v18h18M6 16l5-6 4 3 6-9',
  onboarding: 'M14 3h7v7L10 21l-7-7zM16 7h1M5 18l-3 4 4-2',
  system: 'M4 5h16M4 12h16M4 19h16M8 2v6M16 9v6M8 16v6',
  organisation: 'M4 22V2h16v20M9 22v-5h6v5M8 6h1M15 6h1M8 11h1M15 11h1',
  pause: 'M7 4h3v16H7zM14 4h3v16h-3z',
  play: 'M6 3v18l15-9z',
  stop: 'M4 4h16v16H4z',
  check: 'M4 12l5 5L20 5',
  close: 'M5 5l14 14M19 5 5 19',
  warning: 'M12 2 1 22h22zM12 8v6M12 18v1',
  lock: 'M6 10V7a6 6 0 0 1 12 0v3M3 10h18v12H3zM12 14v4',
  sun: 'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10M12 1v3M12 20v3M1 12h3M20 12h3M4 4l2 2M18 18l2 2M4 20l2-2M18 6l2-2',
  moon: 'M20 15A9 9 0 0 1 9 3a9 9 0 1 0 11 12z',
  refresh: 'M20 8a9 9 0 1 0 1 8M20 2v6h-6',
  folder: 'M2 5h8l2 3h10v14H2z',
  compare: 'M12 2v20M5 22h14M3 6h18M5 6l-4 8h8zM19 6l-4 8h8z',
  sparkles: 'M12 2l3 7 7 3-7 3-3 7-3-7-7-3 7-3z',
  globe: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20M2 12h20M12 2c-6 6-6 14 0 20 6-6 6-14 0-20',
  user: 'M12 2a4 4 0 1 0 0 8 4 4 0 0 0 0-8M3 22v-3a9 9 0 0 1 18 0v3',
  plus: 'M12 3v18M3 12h18',
  trash: 'M3 6h18M9 6V2h6v4M5 6l1 16h12l1-16M10 10v8M14 10v8',
  search: 'M10 2a8 8 0 1 0 0 16 8 8 0 0 0 0-16M16 16l6 6',
  file: 'M3 2h12l6 6v14H3zM15 2v6h6M7 12h10M7 17h7',
  plug: 'M8 2v6M16 2v6M5 8h14v4a7 7 0 0 1-14 0zM12 19v4',
  hook: 'M18 2v13a7 7 0 0 1-14 0v-3l4 4',
  send: 'M2 2l20 10L2 22l4-10zM6 12h16',
  mail: 'M2 4h20v16H2zM2 4l10 9L22 4',
  database: 'M3 5c0-5 18-5 18 0s-18 5-18 0v14c0 5 18 5 18 0V5M3 12c0 5 18 5 18 0',
  left: 'M14 4l-8 8 8 8',
  right: 'M10 4l8 8-8 8',
  up: 'M4 14l8-8 8 8',
  down: 'M4 10l8 8 8-8',
  start: 'M4 3v18M20 3 7 12l13 9z',
  undo: 'M8 3 2 9l6 6M2 9h12a7 7 0 0 1 0 14',
  redo: 'M16 3l6 6-6 6M22 9H10a7 7 0 0 0 0 14',
  circle: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20',
} as const;

export type IconName = keyof typeof paths;

/** Unknown extension icon names get a font-independent fallback. Labels supply accessible names. */
export function Icon({ name, size = 16 }: { name: string; size?: number }) {
  return <svg aria-hidden="true" focusable="false" width={size} height={size} viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"
    style={{ display: 'inline-block', verticalAlign: 'middle', flexShrink: 0 }}>
    <path d={paths[name as IconName] ?? paths.system} />
  </svg>;
}
