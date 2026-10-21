// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { HTMLAttributes, ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  breadcrumbs?: ReactNode;
  metadata?: ReactNode;
  description?: ReactNode;
  /** At most one primary; the active page owns action priority. */
  actions?: ReactNode;
  filters?: ReactNode;
}

export function PageHeader({ title, breadcrumbs, metadata, description, actions, filters }: PageHeaderProps) {
  return (
    <header className={`studio-page-header${filters ? ' studio-page-header--filtered' : ''}`}>
      {breadcrumbs && <div className="studio-page-breadcrumbs">{breadcrumbs}</div>}
      <div className="studio-page-heading-row">
        <div className="studio-page-heading">
          <h1>{title}</h1>
          {metadata && <div className="studio-page-metadata">{metadata}</div>}
          {description && <p className="studio-page-description">{description}</p>}
        </div>
        {actions && <div className="studio-page-actions">{actions}</div>}
      </div>
      {filters && <div className="studio-page-filters">{filters}</div>}
    </header>
  );
}

/** Shell supplies gutters; feature layouts only limit width and internal spacing. */
export function PageLayout({ width = 'standard', className, ...props }: HTMLAttributes<HTMLDivElement> & {
  width?: 'standard' | 'form' | 'full';
}) {
  return <div className={['studio-page', `studio-page--${width}`, className].filter(Boolean).join(' ')} {...props} />;
}
