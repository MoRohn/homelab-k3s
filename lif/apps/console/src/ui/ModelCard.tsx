import type { ModelDeployment, ModelRole } from '@/api/contracts.gen';
import { Badge } from './Badge';
import { Icon } from './Icon';
import { StatusBadge } from './StatusBadge';
import { cx } from './tone';

interface Common {
  /** Detail link (/models/roles/:role or /models/deployments/:id). */
  href?: string;
  compact?: boolean;
}

/** Either a role (portfolio row, §26) or a deployment (candidate/standby), never both. */
export type ModelCardProps =
  | (Common & { role: ModelRole; deployment?: undefined; hint?: undefined })
  | (Common & { deployment: ModelDeployment; role?: undefined; hint?: string });

/** ROLE / MODEL / STATE — e.g. "Fast · Qwen3 4B · Ready" with the fallback cause in words when relevant. */
export function ModelCard(props: ModelCardProps) {
  const { href, compact } = props;
  let title: string;
  let model: string;
  let blurb: string | null | undefined;
  let state;
  if (props.role) {
    const r = props.role;
    title = r.label;
    model = r.model_name || 'Not deployed';
    blurb = r.fallback_active || r.degraded ? r.cause_label : r.blurb;
    state = <StatusBadge health={r.state} label={r.state_label || undefined} size="sm" />;
  } else {
    const d = props.deployment;
    title = d.name;
    model = [d.params_b ? `${d.params_b}B` : null, d.runtime].filter(Boolean).join(' · ');
    blurb = props.hint ?? d.state_label;
    state = <StatusBadge health={d.health} label={d.state_label || undefined} size="sm" />;
  }
  const fallback = props.role?.fallback_active;
  const body = (
    <>
      <div class="lz-model-main">
        <span class="lz-model-role">{title}</span>
        <span class="lz-model-name">{model}</span>
        {!compact && blurb && (
          <span class={cx('lz-model-blurb', fallback && 'is-fallback')}>
            {fallback && <Icon name="warning" size={14} label="Fallback active:" />}
            {blurb}
          </span>
        )}
      </div>
      <div class="lz-model-side">
        {state}
        {props.role?.canary && <Badge size="sm" tone="info">Canary {props.role.canary.percent}%</Badge>}
        {href && <Icon name="chevron-right" size={16} class="faint" />}
      </div>
    </>
  );
  return href ? (
    <a class={cx('lz-model-card', 'interactive', compact && 'compact')} href={href}>
      {body}
    </a>
  ) : (
    <div class={cx('lz-model-card', compact && 'compact')}>{body}</div>
  );
}
