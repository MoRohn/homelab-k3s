import type { ActivityEvent } from '@/api/contracts.gen';
import { Icon } from './Icon';
import { ago, iso } from './format';
import { cx, SEVERITY } from './tone';

export interface ActivityRowProps {
  event: ActivityEvent;
  /** One line, no detail (Home's Recent Activity). */
  compact?: boolean;
  /** Open technical details or the related object; defaults to following event.href. */
  onOpen?: (event: ActivityEvent) => void;
}

/** "Model refresh completed — 2 better candidates found · 10:32" (§6). Renders an <li>; wrap in a List. */
export function ActivityRow({ event, compact, onOpen }: ActivityRowProps) {
  const s = SEVERITY[event.severity];
  const body = (
    <>
      <Icon name={s.icon} size={18} label={s.label} class={`lz-tone-${s.tone} lz-activity-icon`} />
      <span class="lz-activity-main">
        <span class="lz-activity-title">{event.title}</span>
        {!compact && event.detail && <span class="lz-activity-detail">{event.detail}</span>}
      </span>
      <time class="lz-activity-time num" dateTime={iso(event.ts)}>
        {ago(event.ts)}
      </time>
    </>
  );
  return (
    <li class={cx('lz-activity', compact && 'compact')}>
      {onOpen ? (
        <button type="button" class="lz-activity-row interactive" onClick={() => onOpen(event)}>
          {body}
        </button>
      ) : event.href ? (
        <a class="lz-activity-row interactive" href={event.href}>
          {body}
        </a>
      ) : (
        <div class="lz-activity-row">{body}</div>
      )}
    </li>
  );
}
