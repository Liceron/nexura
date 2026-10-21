// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '../i18n';
import { Icon } from './Icon';

// Portals keep a nested dialog out of the inert page. Restore pre-existing inert
// state only after the last dialog closes, including out-of-order unmounts.
const modalStack: HTMLElement[] = [];
const originalInert = new Map<HTMLElement, boolean>();
let originalOverflow = '';
function syncModalBackground() {
  const top = modalStack[modalStack.length - 1];
  if (top) {
    for (const element of Array.from(document.body.children)) {
      if (!(element instanceof HTMLElement)) continue;
      if (!originalInert.has(element)) originalInert.set(element, element.hasAttribute('inert'));
      element.toggleAttribute('inert', element !== top);
    }
    document.body.style.overflow = 'hidden';
  } else {
    for (const [element, inert] of originalInert) element.toggleAttribute('inert', inert);
    originalInert.clear();
    document.body.style.overflow = originalOverflow;
  }
}

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  maxWidth?: number;
  variant?: 'dialog' | 'drawer';
  footer?: React.ReactNode;
  initialFocus?: 'title';
  className?: string;
  closeDisabled?: boolean;
  /** Used only when the opener disappears, for example after deleting an edited node. */
  returnFocusTo?: () => HTMLElement | null;
}

export function Modal({ open, onClose, title, children, maxWidth, variant = 'dialog', footer, returnFocusTo, initialFocus, className = '', closeDisabled = false }: ModalProps) {
  const t = useT();
  const dialogRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  if (open && !wasOpenRef.current) {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }
  wasOpenRef.current = open;
  const titleId = useId();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const returnFocusRef = useRef(returnFocusTo);
  returnFocusRef.current = returnFocusTo;

  useEffect(() => {
    if (!open) return;
    const opener = openerRef.current;
    const dialog = dialogRef.current;
    const overlay = overlayRef.current;
    if (!dialog || !overlay) return;
    if (!modalStack.length) originalOverflow = document.body.style.overflow;
    modalStack.push(overlay);
    syncModalBackground();
    const focusableElements = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'a[href], button, input, select, textarea, summary, [tabindex]'
    )).filter((element) => element.tabIndex >= 0 && !element.matches(':disabled') &&
      !element.closest('[hidden], [inert]') && element.getClientRects().length > 0 &&
      getComputedStyle(element).visibility !== 'hidden');

    const elements = focusableElements();
    if (initialFocus === 'title') dialog.querySelector<HTMLElement>('h2')?.focus();
    if (initialFocus !== 'title') (elements.find((element) => element.matches('[autofocus], [data-autofocus]')) ??
      elements.find((element) => element === document.activeElement && bodyRef.current?.contains(element)) ??
      elements.find((element) => bodyRef.current?.contains(element)) ?? elements[0] ?? dialog).focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (modalStack[modalStack.length - 1] !== overlay) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
      }
      if (event.key === 'Tab') {
        const elements = focusableElements();
        const first = elements[0] ?? dialog;
        const last = elements[elements.length - 1] ?? dialog;
        if (!dialog?.contains(document.activeElement) || document.activeElement === dialog || document.activeElement === dialog.querySelector('h2') ||
          (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
          event.preventDefault();
          (event.shiftKey ? last : first)?.focus();
        }
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      const index = modalStack.indexOf(overlay);
      const wasTop = index === modalStack.length - 1;
      if (index !== -1) modalStack.splice(index, 1);
      syncModalBackground();
      if (wasTop) {
        const target = opener?.isConnected && !opener.closest('[hidden], [inert]') ? opener : returnFocusRef.current?.();
        if (target?.isConnected && !target.closest('[hidden], [inert]')) target.focus();
      }
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      ref={overlayRef}
      className={`studio-modal-overlay studio-modal-overlay--${variant} ${className}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="studio-modal-surface"
        style={{ maxWidth: maxWidth ?? (variant === 'drawer' ? 288 : 480) }}
      >
        <div
          className="studio-modal-header"
        >
          <h2 id={titleId} tabIndex={initialFocus === 'title' ? -1 : undefined} style={{ fontSize: 'var(--type-section)', lineHeight: 'var(--leading-section)', fontWeight: 600 }}>
            {title}
          </h2>
          <button
            onClick={onClose}
            aria-label={t('ui.closeDialog')}
            type="button"
            disabled={closeDisabled}
            className="studio-icon-button"
          >
            <Icon name="close" />
          </button>
        </div>
        <div ref={bodyRef} className="studio-modal-body">{children}</div>
        {footer && <div className="studio-modal-footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
