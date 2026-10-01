import type { TimelineStep } from '@/api/contracts.gen';
import { Icon, type IconName } from './Icon';
import { clock, iso } from './format';
import { cx, type Tone } from './tone';

export interface TimelineProps {
  steps: TimelineStep[];
  /** The last step is in progress (shows a live marker; announced politely). */
  live?: boolean;
  emptyLabel?: string;
  class?: string;
}

const KIND: Record<TimelineStep['kind'], { icon: IconName; tone: Tone; word: string }> = {
  info: { icon: 'info', tone: 'inactive', word: 'Step' },
  success: { icon: 'success', tone: 'success', word: 'Finished' },
  warning: { icon: 'warning', tone: 'warning', word: 'Warning' },
  error: { icon: 'error', tone: 'danger', word: 'Error' },
  decision: { icon: 'decision', tone: 'info', word: 'Decision' },
};

/** "10:32 Repository scanned / 10:33 4 candidate issues found / 10:35 Task complete" (§24).
 *  Plain sentences a non-expert can follow; when live, new steps are announced politely as they arrive. */
export function Timeline({ steps, live, emptyLabel = 'No steps recorded yet.', class: cls }: TimelineProps) {
  if (!steps.length) return <p class="muted small">{emptyLabel}</p>;
  return (
    <ol role="list" class={cx('lz-timeline', live && 'is-live', cls)} aria-live={live ? 'polite' : undefined} aria-relevant={live ? 'additions' : undefined}>
      {steps.map((s, i) => {
        const k = KIND[s.kind];
        const current = live && i === steps.length - 1;
        return (
          <li key={`${s.ts}-${i}`} class={cx('lz-timeline-step', current && 'current')}>
            <time class="lz-timeline-time num" dateTime={iso(s.ts)}>
              {clock(s.ts)}
            </time>
            <Icon name={k.icon} size={16} class={`lz-tone-${k.tone} lz-timeline-icon`} label={k.word} />
            <div class="lz-timeline-body">
              <span class="lz-timeline-label">{s.label}</span>
              {s.detail && <span class="lz-timeline-detail">{s.detail}</span>}
              {current && <span class="sr-only">In progress</span>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
