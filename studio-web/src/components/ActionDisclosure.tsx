// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/** A disclosure of ordinary buttons: Tab navigation, Escape and focus return. */
export function ActionDisclosure({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const trigger = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const id = useId();
  const close = () => { setOpen(false); trigger.current?.focus(); };
  useEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = trigger.current!.getBoundingClientRect();
      setPosition({ top: Math.max(12, Math.min(rect.bottom + 4, window.innerHeight - (content.current?.offsetHeight ?? 240) - 12)), left: Math.max(12, Math.min(rect.left, window.innerWidth - 252)) });
    };
    place();
    content.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
    const outside = (event: PointerEvent) => {
      if (!content.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); } };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape, true);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape, true);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);
  return <>
    <button type="button" ref={trigger} className="studio-disclosure-trigger" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(!open)}>{label}</button>
    {open && createPortal(<div id={id} ref={content} role="group" aria-label={label} className="studio-action-disclosure" style={position}
      onBlur={(event) => { if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false); }}
      onClick={(event) => { if ((event.target as HTMLElement).closest('button,a')) close(); }}>
      {children}
    </div>, document.body)}
  </>;
}
