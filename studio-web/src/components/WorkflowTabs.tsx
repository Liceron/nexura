// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useId, useState } from 'react';

export interface WorkflowTab { id: string; label: string; testId?: string }
/** Manual activation keeps expensive panels from loading just while arrowing. */
export function WorkflowTabs({ tabs, active, onChange, label, panelId }: {
  tabs: WorkflowTab[]; active: string; onChange: (id: string) => void; label: string; panelId: string;
}) {
  const id = useId();
  const [focused, setFocused] = useState(active);
  useEffect(() => { setFocused(active); }, [active]);
  return <div role="tablist" aria-label={label} className="studio-workflow-tabs" onKeyDown={(event) => {
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : event.key === 'ArrowRight' ? (index + 1) % buttons.length : event.key === 'ArrowLeft' ? (index - 1 + buttons.length) % buttons.length : -1;
    if (next < 0) return;
    event.preventDefault();
    buttons[next].focus();
    buttons[next].scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }}>
    {tabs.map((tab) => <button type="button" key={tab.id} id={`${id}-${tab.id}`} data-testid={tab.testId ?? `tab-${tab.id}`} role="tab"
      aria-selected={active === tab.id} aria-controls={panelId} tabIndex={focused === tab.id ? 0 : -1} onFocus={() => setFocused(tab.id)}
      onClick={(event) => { onChange(tab.id); event.currentTarget.scrollIntoView?.({ block: 'nearest', inline: 'nearest' }); }}>{tab.label}</button>)}
  </div>;
}
