import { useLocation } from 'preact-iso/router';
import type { HumanError } from '@/api/contracts.gen';
import { Button } from './Button';
import { Icon } from './Icon';
import { TechDetails } from './TechDetails';
import { cx } from './tone';

export interface HumanErrorCardProps {
  error: HumanError;
  /** Handles client verbs from error.actions ("retry", "details", "login", "reload"); paths starting with "/" navigate. */
  onAction?: (action: string) => void;
  /** Shortcut for the common case: renders/handles a Retry button when no "retry" action is listed. */
  onRetry?: () => void;
  compact?: boolean;
  class?: string;
}

/** What failed, the impact, the next step (§81) — never "HTTP 503". Recovery actions come from the server
 *  (Retry, Use fallback, Sign in…, §82); raw detail stays behind Technical details (§42).
 *  role="alert" so an error that appears after an action is announced without moving focus. */
export function HumanErrorCard({ error, onAction, onRetry, compact, class: cls }: HumanErrorCardProps) {
  const { route } = useLocation();
  const run = (action: string) => {
    if (action === 'retry' && onRetry) return onRetry();
    if (action === 'login') return route(`/login?next=${encodeURIComponent(location.pathname)}`);
    if (action === 'reload') return location.reload();
    if (action.startsWith('/')) return route(action);
    onAction?.(action);
  };
  const actions = [...error.actions];
  if (onRetry && !actions.some((a) => a.action === 'retry')) actions.push({ label: 'Retry', action: 'retry' });
  return (
    <div class={cx('lz-error-card', compact && 'compact', cls)} role="alert">
      <Icon name="alert" size={20} class="lz-tone-danger lz-error-icon" label="Problem" />
      <div class="grow stack-sm">
        <p class="lz-error-title">{error.title}</p>
        {error.impact && <p class="lz-error-impact">{error.impact}</p>}
        {error.next_step && !compact && <p class="lz-error-next">{error.next_step}</p>}
        {(actions.length > 0 || error.tech.length > 0) && (
          <div class="row wrap">
            {actions.map((a, i) => (
              <Button key={`${a.action}-${i}`} size="sm" variant={i === 0 ? 'secondary' : 'ghost'} onClick={() => run(a.action)}>
                {a.label}
              </Button>
            ))}
            {error.tech.length > 0 && <TechDetails items={error.tech} title="Error details" />}
          </div>
        )}
      </div>
    </div>
  );
}
