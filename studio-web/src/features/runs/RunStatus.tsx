// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useT } from '../../i18n';
import { runStatusLabel, runStatusVariant } from './runStatus';

/** Text is always present; colour conveys only recorded execution state. */
export function RunStatus({ status }: { status: string }) {
  const t = useT();
  return <span className={`run-status run-status--${runStatusVariant(status)}`} data-status={status}>
    <span aria-hidden="true">●</span> {runStatusLabel(t, status)}
  </span>;
}
