// Words for agent runs shared by the Agents list and the run page. Runs are synthesized from real
// sources (brief §5): the source label says which, so nothing reads as a fabricated "agent".
import type { AgentRun } from '@/api/contracts.gen';
import type { Tone } from '@/ui';

export const RUN_STATUS: Record<AgentRun['status'], { label: string; tone: Tone }> = {
  running: { label: 'Running', tone: 'brand' },
  done: { label: 'Finished', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  waiting_approval: { label: 'Needs approval', tone: 'warning' },
  queued: { label: 'Queued', tone: 'inactive' },
};

/** A status this build doesn't know yet (a tab older than the server) renders as Unknown, never a crash. */
export const runStatus = (s: AgentRun['status']): { label: string; tone: Tone } =>
  RUN_STATUS[s] ?? { label: 'Unknown', tone: 'inactive' };

export const RUN_SOURCE: Record<AgentRun['source'], string> = {
  discovery: 'Recorded by model discovery',
  benchmark: 'Recorded from benchmark activity',
  decision_cycle: 'Recorded by the decision improvement cycle',
  trace: 'Mined from decision traces',
};

export const runHref = (run: AgentRun) => `/agents/${encodeURIComponent(run.id)}`;

export const APPROVALS_KEY = 'agents/approvals';
export const AGENTS_KEY = 'agents/overview';
