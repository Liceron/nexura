// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT } from '../../i18n';
import { useState, useEffect, useRef } from 'react';
import type { ReplayStep } from './types';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { formatEventTitle } from './events';

interface StateReplayerProps {
  steps: ReplayStep[];
  currentStepIndex: number;
  onSelectStepIndex: (index: number) => void;
}

export function StateReplayer({
  steps,
  currentStepIndex,
  onSelectStepIndex,
}: StateReplayerProps) {
  const t = useT();
  const [isPlaying, setIsPlaying] = useState(false);
  const currentIndexRef = useRef(currentStepIndex);
  currentIndexRef.current = currentStepIndex;

  // Playback timer loop
  useEffect(() => {
    if (!isPlaying) return;
    const interval = window.setInterval(() => {
      const next = currentIndexRef.current + 1;
      if (next >= steps.length) {
        setIsPlaying(false);
      } else {
        onSelectStepIndex(next);
      }
    }, 900);

    return () => window.clearInterval(interval);
  }, [isPlaying, steps.length, onSelectStepIndex]);

  if (steps.length === 0) {
    return (
      <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)', fontSize: 13 }}>
        {t('ui.noReplayStepsAvailable')}
      </div>
    );
  }

  const safeIndex = Math.max(0, Math.min(currentStepIndex, steps.length - 1));
  const activeStep = steps[safeIndex];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* Control Bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: 12,
          padding: 12,
          backgroundColor: 'var(--bg-surface)',
          borderRadius: 'var(--radius-sm)',
          border: '1px solid var(--border-subtle)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setIsPlaying(false);
              onSelectStepIndex(0);
            }}
            disabled={safeIndex === 0}
            title={t('ui.resetToInitialState')}
          >
            <Icon name="start" /> {t('ui.start')}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setIsPlaying(false);
              onSelectStepIndex(Math.max(0, safeIndex - 1));
            }}
            disabled={safeIndex === 0}
            title={t('ui.stepBack')}
          >
            <Icon name="left" /> {t('ui.prev')}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => setIsPlaying(!isPlaying)}
            title={isPlaying ? t('ui.pauseReplay') : t('ui.playReplay')}
          >
            {isPlaying ? <><Icon name="pause" /> {t('ui.pause')}</> : <><Icon name="play" /> {t('ui.play')}</>}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setIsPlaying(false);
              onSelectStepIndex(Math.min(steps.length - 1, safeIndex + 1));
            }}
            disabled={safeIndex === steps.length - 1}
            title={t('ui.stepForward')}
          >
            {t('ui.next') + " "}<Icon name="play" />
          </Button>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 }}>
          <span style={{ color: 'var(--text-secondary)' }}>
            {t('ui.step2') + " "}<strong>{safeIndex}</strong> of <strong>{steps.length - 1}</strong>
          </span>
          <Badge variant="info">{formatEventTitle(activeStep.eventType)}</Badge>
          {activeStep.nodeId && (
            <Badge variant="neutral">
              {t('ui.node') + " "}<code>{activeStep.nodeId}</code>
            </Badge>
          )}
        </div>
      </div>

      {/* Scrubber Range Slider */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <input
          type="range"
          min={0}
          max={steps.length - 1}
          value={safeIndex}
          onChange={(e) => {
            setIsPlaying(false);
            onSelectStepIndex(parseInt(e.target.value, 10));
          }}
          aria-label={t('ui.executionReplayStepScrubber')}
          style={{ width: '100%', cursor: 'pointer' }}
        />
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: 'var(--type-meta)',
            color: 'var(--text-muted)',
          }}
        >
          <span>{t('ui.step0Initialized')}</span>
          <span>{t('ui.step2') + " "}{steps.length - 1} {t('ui.final')}</span>
        </div>
      </div>

      {/* Step Details & State Inspection */}
      <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {/* Left: Step Delta */}
        <div
          style={{
            padding: 12,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>
            {t('ui.stepTransitionDelta')}
          </div>
          {activeStep.error ? (
            <div style={{ color: 'var(--status-danger)', fontSize: 12 }}>
              {t('ui.error2') + " "}{activeStep.error}
            </div>
          ) : activeStep.delta && Object.keys(activeStep.delta).length > 0 ? (
            <pre
              style={{
                margin: 0,
                fontSize: 'var(--type-meta)',
                fontFamily: 'var(--font-mono)',
                color: 'var(--status-success)',
                overflowX: 'auto',
              }}
            >
              {JSON.stringify(activeStep.delta, null, 2)}
            </pre>
          ) : (
            <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
              {safeIndex === 0 ? t('ui.initialWorkflowStateLoaded') : t('ui.noStateDeltaProduced')}
            </div>
          )}
        </div>

        {/* Right: Cumulative State Snapshot */}
        <div
          style={{
            padding: 12,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>
            {t('ui.cumulativeStateSnapshotAtStep') + " "}{safeIndex}:
          </div>
          <pre
            style={{
              margin: 0,
              fontSize: 'var(--type-meta)',
              fontFamily: 'var(--font-mono)',
              overflowX: 'auto',
              maxHeight: 220,
            }}
          >
            {JSON.stringify(activeStep.state, null, 2)}
          </pre>
        </div>
      </div>
    </div>
  );
}
