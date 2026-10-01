import type { AgentRun } from '@/api/contracts.gen';
import { Badge } from './Badge';
import { Progress } from './Progress';
import type { IconName } from './Icon';
import { ago } from './format';
import { cx, type Tone } from './tone';

export interface AgentCardProps {
  run: AgentRun;
  /** Detail link (/agents/:id). */
  href?: string;
  compact?: boolean;
}

const STATUS: Record<AgentRun['status'], { label: string; tone: Tone; icon: IconName }> = {
  running: { label: 'Running', tone: 'brand', icon: 'play' },
  done: { label: 'Finished', tone: 'success', icon: 'success' },
  failed: { label: 'Failed', tone: 'danger', icon: 'error' },
  waiting_approval: { label: 'Needs approval', tone: 'warning', icon: 'warning' },
  queued: { label: 'Queued', tone: 'inactive', icon: 'clock' },
};

const SOURCE: Record<AgentRun['source'], string> = {
  discovery: 'From model discovery',
  benchmark: 'From benchmark activity',
  decision_cycle: 'From the decision improvement cycle',
  trace: 'From recorded traces',
};

/** Agent · Task · Status · Started · Progress (§22). The source line ("From model discovery") is always shown:
 *  runs are synthesized from real activity, and saying where each came from keeps that honest. */
export function AgentCard({ run, href, compact }: AgentCardProps) {
  const s = STATUS[run.status];
  const meta = [
    !compact && run.status === 'running' && run.current_step,
    !compact && (run.finished_at ? `Finished ${ago(run.finished_at)}` : run.started_at ? `Started ${ago(run.started_at)}` : null),
    SOURCE[run.source],
  ].filter(Boolean);
  const body = (
    <>
      <div class="row-between">
        <span class="lz-agent-name">{run.agent}</span>
        <Badge tone={s.tone} size="sm" icon={s.icon}>
          {s.label}
        </Badge>
      </div>
      <span class="lz-agent-task">{run.task}</span>
      {run.status === 'running' && <Progress value={run.progress} label={`${run.agent} progress`} size="sm" showValue={run.progress !== null && run.progress !== undefined} />}
      <span class="lz-agent-meta muted small">{meta.join(' · ')}</span>
    </>
  );
  return href ? (
    <a class={cx('lz-agent-card', 'interactive', compact && 'compact')} href={href}>
      {body}
    </a>
  ) : (
    <div class={cx('lz-agent-card', compact && 'compact')}>{body}</div>
  );
}
