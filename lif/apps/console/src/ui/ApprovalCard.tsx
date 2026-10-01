import type { Approval } from '@/api/contracts.gen';
import { Badge } from './Badge';
import { Button } from './Button';
import { Card } from './Card';
import { FactList } from './FactList';
import type { IconName } from './Icon';
import { inlineMarkdown } from './Markdown';
import { ago, iso } from './format';
import { TechDetails } from './TechDetails';
import type { Tone } from './tone';

export interface ApprovalCardProps {
  approval: Approval;
  /** Called with the chosen option value; the parent posts it and refreshes. */
  onAnswer: (value: string) => void | Promise<void>;
  /** The option value currently being submitted (shows progress on that button). */
  busy?: string | null;
  /** False when this session lacks the permission; whyNot explains (e.g. pairings need an admin session). */
  canAnswer?: boolean;
  whyNot?: string;
}

const STATUS: Record<Approval['status'], { label: string; tone: Tone; icon: IconName }> = {
  pending: { label: 'Waiting for you', tone: 'warning', icon: 'clock' },
  approved: { label: 'Approved', tone: 'success', icon: 'check' },
  rejected: { label: 'Rejected', tone: 'inactive', icon: 'x' },
  answered: { label: 'Answered', tone: 'success', icon: 'check' },
  expired: { label: 'Expired', tone: 'inactive', icon: 'clock' },
};

const KIND: Record<Approval['kind'], string> = {
  review: 'Decision review',
  device_pairing: 'Device pairing',
  action: 'Agent requests approval',
};

/** "Agent requests approval / Action / Why / Impact / [Reject] [Approve]" — consequences explicit (§40).
 *  Says in words whether anything is blocked on the answer, so a review never feels like an emergency. */
export function ApprovalCard({ approval, onAnswer, busy = null, canAnswer = true, whyNot }: ApprovalCardProps) {
  const pending = approval.status === 'pending';
  const status = STATUS[approval.status];
  return (
    <Card
      as="article"
      level={3}
      tone={approval.blocking && pending ? 'warning' : 'neutral'}
      title={approval.title}
      subtitle={
        <>
          {KIND[approval.kind]} · <time dateTime={iso(approval.created_at)}>{ago(approval.created_at)}</time>
        </>
      }
      actions={
        <Badge tone={status.tone} size="sm" icon={pending && approval.blocking ? 'paused' : status.icon}>
          {status.label}
        </Badge>
      }
      class="lz-approval"
    >
      <div class="stack-sm">
        <FactList
          items={[
            { label: 'Action', value: approval.action && inlineMarkdown(approval.action) },
            { label: 'Why', value: approval.why && inlineMarkdown(approval.why) },
            { label: 'Impact', value: approval.impact && inlineMarkdown(approval.impact) },
          ].filter((f) => f.value)}
        />
        {/* Said once: a review's Impact line already says nothing is waiting; blocking work is always called out. */}
        {pending && (approval.blocking || !approval.impact) && (
          <p class="lz-approval-blocking small">
            {approval.blocking ? 'Work is waiting on this answer.' : 'Nothing is waiting on this answer — answer when convenient.'}
          </p>
        )}
        {pending &&
          (canAnswer ? (
            <div class="lz-approval-options" role="group" aria-label={`Answer: ${approval.title}`}>
              {approval.options.map((o) => (
                <Button
                  key={o.value}
                  variant={o.tone === 'primary' ? 'primary' : o.tone === 'danger' ? 'danger' : 'secondary'}
                  subtitle={o.description ?? undefined}
                  loading={busy === o.value}
                  disabled={!!busy && busy !== o.value}
                  onClick={() => void onAnswer(o.value)}
                >
                  {o.label}
                </Button>
              ))}
            </div>
          ) : (
            <p class="muted small">{whyNot ?? 'This session cannot answer this request.'}</p>
          ))}
        <TechDetails items={approval.tech} inline />
      </div>
    </Card>
  );
}
