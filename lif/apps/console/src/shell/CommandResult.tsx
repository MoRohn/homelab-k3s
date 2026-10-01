import { useState } from 'preact/hooks';
import { runProposedAction, toHumanError } from '@/api/client';
import type { CommandResolution, HumanError, PromptSuggestion, ProposedAction } from '@/api/contracts.gen';
import { can, useMe } from '@/api/session';
import { invalidate } from '@/api/store';
import { Badge } from '@/ui/Badge';
import { Button } from '@/ui/Button';
import { ConfirmDialog } from '@/ui/ConfirmDialog';
import { FactList } from '@/ui/FactList';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { Icon } from '@/ui/Icon';
import { Markdown } from '@/ui/Markdown';
import { toast } from '@/ui/Toast';

export interface CommandResultProps {
  resolution: CommandResolution;
  onClose: () => void;
  onNavigate: (href: string) => void;
  /** Hand the prompt to Ask (ai_prompt kind). */
  onAsk: (prompt: PromptSuggestion) => void;
  /** Called after a proposed action succeeded (the parent may close the sheet). */
  onActionDone?: (action: ProposedAction) => void;
  /** Label of the hand-off button (Ask uses "Ask the model instead"). */
  askLabel?: string;
}

const KIND_LABEL: Record<CommandResolution['kind'], string> = {
  ai_prompt: 'Prompt',
  system_query: 'System',
  agent_request: 'Agent',
  model_request: 'Models',
  knowledge_query: 'Knowledge',
  operational_command: 'Action',
  navigation: 'Go to',
};

/** What the command bar understood, the answer, and at most one action — whose impact is stated before it runs (§110). */
export function CommandResult({ resolution: r, onClose, onNavigate, onAsk, onActionDone, askLabel = 'Ask Labzilla' }: CommandResultProps) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<HumanError | null>(null);
  const action = r.proposed_action;
  const me = useMe();
  // The server refuses anyway (403); saying so up front beats a button that fails (§99: phones get safe ops).
  const allowed = !action || can(me.data, action.perm);
  const answer = allowed && action?.impact && r.answer?.startsWith(action.impact) ? r.answer.slice(action.impact.length).trim() : r.answer;

  const run = async (typed?: string) => {
    if (!action) return;
    setBusy(true);
    setError(null);
    try {
      const res = await runProposedAction<{ message?: string | null } | undefined>(action, typed);
      setConfirming(false);
      toast({ title: `${action.label}: done`, body: res?.message ?? undefined, tone: 'success' });
      invalidate('');
      onActionDone?.(action);
    } catch (e) {
      setError(toHumanError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="lz-cmd-result stack-sm">
      <div class="row">
        <Badge size="sm" tone="neutral">{KIND_LABEL[r.kind]}</Badge>
        <h2 class="grow lz-cmd-title">{r.title}</h2>
      </div>
      {/* The action button already states its impact: show only what the answer adds (e.g. a permission note). */}
      {answer && <Markdown text={answer} />}
      {r.facts.length > 0 && <FactList items={r.facts.map((f) => ({ label: f.label, value: f.value }))} />}
      {action && (
        <div class="lz-cmd-action stack-sm">
          {action.description && <p class="small">{action.description}</p>}
          {!allowed && (
            <p class="small muted">
              <Icon name="lock" size={16} /> {action.label} needs an admin session. Open Labzilla on your desktop to do this.
            </p>
          )}
        </div>
      )}
      {error && !confirming && <HumanErrorCard error={error} compact />}
      <div class="row wrap">
        {action && allowed && (
          <Button
            variant={action.reversible ? 'primary' : 'danger'}
            subtitle={action.impact}
            loading={busy && !confirming}
            onClick={() => (action.confirm === 'none' ? void run() : setConfirming(true))}
          >
            {action.label}
          </Button>
        )}
        {r.prompt && (
          <Button variant={action ? 'secondary' : 'primary'} icon="ask" onClick={() => r.prompt && onAsk(r.prompt)}>
            {askLabel}
          </Button>
        )}
        {r.navigate && (
          <Button variant="ghost" iconRight="arrow-right" onClick={() => r.navigate && onNavigate(r.navigate)}>
            Open
          </Button>
        )}
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      {action && action.confirm !== 'none' && (
        <ConfirmDialog
          open={confirming}
          preview={{
            title: action.label,
            changes: [action.description, action.impact].filter(Boolean),
            interrupts: [],
            rollback: action.reversible ? 'You can undo this.' : 'This cannot be undone.',
            confirm: action.confirm,
            confirm_text: action.confirm_text,
          }}
          busy={busy}
          error={error}
          tone={action.reversible ? 'primary' : 'danger'}
          confirmLabel={action.label}
          onConfirm={(typed) => void run(typed)}
          onCancel={() => setConfirming(false)}
        />
      )}
      <p class="xsmall faint">{r.decided_by === 'rules' ? 'Understood by Labzilla rules' : 'Understood by a local model'}</p>
    </div>
  );
}
