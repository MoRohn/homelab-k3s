import { useId, useState } from 'preact/hooks';
import type { DecisionBadge as Decision } from '@/api/contracts.gen';
import { Icon } from './Icon';
import { percent } from './format';
import { cx } from './tone';

export interface DecisionBadgeProps {
  decision: Decision;
  /** Start with the reasons expanded. */
  defaultOpen?: boolean;
  /** Toggle text (default "Why this route?"; use "Why?" for non-routing decisions). */
  whyLabel?: string;
  class?: string;
}

const BY: Record<Decision['decided_by'], string> = {
  rules: 'Rules',
  jev: 'Jev',
  local_model: 'Local model',
  human: 'You',
  code: 'Code',
};

/** "Decision: Use fast local model · Confidence 97% · Jev" plus an expandable "Why this route?" list of
 *  structured reasons (§25). Never hidden reasoning: only the label/value facts the decider recorded.
 *  A non-actionable decision is labelled "Advice only" in words (Jev's `actionable: false`). */
export function DecisionBadge({ decision, defaultOpen = false, whyLabel = 'Why this route?', class: cls }: DecisionBadgeProps) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  const hasWhy = decision.why.length > 0;
  const conf = decision.confidence;
  return (
    <div class={cx('lz-decision', !decision.actionable && 'not-actionable', cls)}>
      <p class="lz-decision-head">
        <Icon name="decision" size={16} class="lz-decision-icon" />
        <span class="grow">
          <span class="muted">Decision: </span>
          <strong>{decision.decision}</strong>
          {conf !== null && conf !== undefined && <span class="num"> · Confidence {percent(conf)}</span>}
          <span class="muted"> · {BY[decision.decided_by]}</span>
          {!decision.actionable && (
            <span class="lz-decision-advice">
              {' '}
              · <Icon name="info" size={14} /> Advice only
            </span>
          )}
        </span>
      </p>
      {hasWhy && (
        <button type="button" class="lz-decision-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
          {whyLabel}
          <Icon name={open ? 'chevron-up' : 'chevron-down'} size={14} />
        </button>
      )}
      {hasWhy && (
        <dl id={id} class="lz-decision-why" hidden={!open}>
          {decision.why.map((w, i) => (
            <div key={`${w.label}-${i}`}>
              <dt>{w.label}</dt>
              <dd>{w.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
