// "Run an agent" (§22, §95, §110): only agents the backend can really start are offered, with what they do
// stated before the button is pressed. Params stay minimal; the server validates and enforces permission —
// the UI checks can() only to explain instead of offering something that will be refused.
import { useEffect, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { get, post, toHumanError } from '@/api/client';
import type { AgentCatalogItem, AgentParam, AgentRun, CandidateItem, HumanError, OkResponse, User } from '@/api/contracts.gen';
import { can } from '@/api/session';
import { invalidate, useResource } from '@/api/store';
import { agentTaskHandoff } from '@/pages/ask/state';
import { Button, HumanErrorCard, Input, Select, Sheet, Skeleton, toast, type SelectOption } from '@/ui';
import { runHref } from './labels';

type Params = Record<string, string | string[]>;

export interface RunAgentSheetProps {
  open: boolean;
  onClose: () => void;
  catalog: AgentCatalogItem[];
  user: User | null | undefined;
}

/** Candidate pickers without server-supplied options list the models waiting for evaluation (fetched only while open). */
function useCandidates(enabled: boolean) {
  return useResource<SelectOption[]>(enabled ? 'models/candidates/options' : null, async () =>
    (await get<CandidateItem[]>('/api/models/candidates')).map((c) => ({
      value: c.deployment.id,
      label: c.deployment.name,
      description: c.comparison_hint,
    })),
  );
}

function defaults(item: AgentCatalogItem | undefined): Params {
  const p: Params = {};
  for (const param of item?.params ?? []) {
    if (param.type === 'multiselect') p[param.key] = [];
    else if (param.type === 'select' && param.options?.[0]) p[param.key] = param.options[0].value;
    else p[param.key] = '';
  }
  return p;
}

function ParamField({ param, value, onChange, candidates }: { param: AgentParam; value: string | string[] | undefined; onChange: (v: string | string[]) => void; candidates: ReturnType<typeof useCandidates> }) {
  const label = param.required ? param.label : `${param.label} (optional)`;
  if (param.type === 'multiselect') {
    const chosen = Array.isArray(value) ? value : [];
    return (
      <fieldset class="lz-run-checks">
        <legend class="lz-label">{label}</legend>
        {(param.options ?? []).map((o) => (
          <label key={o.value} class="lz-run-check">
            <input
              type="checkbox"
              checked={chosen.includes(o.value)}
              onChange={(e) => onChange((e.currentTarget as HTMLInputElement).checked ? [...chosen, o.value] : chosen.filter((v) => v !== o.value))}
            />
            <span>{o.label}</span>
          </label>
        ))}
        {!param.required && <p class="lz-hint">Leave all unchecked for the default.</p>}
      </fieldset>
    );
  }
  if (param.type === 'select' || (param.type === 'candidate' && param.options?.length)) {
    const options: SelectOption[] = (param.options ?? []).map((o) => ({ value: o.value, label: o.label }));
    return <Select label={label} value={typeof value === 'string' ? value : ''} options={param.required ? options : [{ value: '', label: 'Default' }, ...options]} onChange={onChange} />;
  }
  if (param.type === 'candidate') {
    if (candidates.loading) return <Skeleton height="2.75rem" />;
    if (candidates.error) return <HumanErrorCard error={candidates.error} compact onRetry={() => void candidates.refresh()} />;
    const options = candidates.data ?? [];
    if (!options.length) return <p class="muted small">No model candidates are waiting for evaluation. Model Scout finds new ones.</p>;
    const current = typeof value === 'string' && value ? value : '';
    const hint = options.find((o) => o.value === current)?.description;
    return <Select label={label} value={current} options={[{ value: '', label: 'Choose a candidate…' }, ...options]} onChange={onChange} hint={hint} />;
  }
  return <Input label={label} value={typeof value === 'string' ? value : ''} onInput={(e) => onChange((e.currentTarget as HTMLInputElement).value)} />;
}

const missing = (item: AgentCatalogItem, params: Params) =>
  item.params.some((p) => {
    if (!p.required) return false;
    const v = params[p.key];
    return Array.isArray(v) ? v.length === 0 : !v;
  });

export function RunAgentSheet({ open, onClose, catalog, user }: RunAgentSheetProps) {
  const { route } = useLocation();
  const runnable = catalog.filter((c) => c.runnable);
  const unavailable = catalog.filter((c) => !c.runnable);
  const [agentId, setAgentId] = useState<string>('');
  const [params, setParams] = useState<Params>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<HumanError | null>(null);
  // "Run as Agent" from an Ask answer hands its prompt over in memory (never the URL). No installed agent takes a
  // free-form task, so the sheet says that plainly instead of pretending to queue the prompt somewhere.
  const [handoff, setHandoff] = useState<string | null>(null);

  const item = runnable.find((c) => c.id === agentId) ?? runnable[0];
  const needsCandidates = open && !!item?.params.some((p) => p.type === 'candidate' && !p.options?.length);
  const candidates = useCandidates(needsCandidates);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setAgentId(runnable[0]?.id ?? '');
    setParams(defaults(runnable[0]));
    setHandoff(agentTaskHandoff.get());
    agentTaskHandoff.set(null);
    // Reset only when the sheet opens; the catalog refreshing underneath must not wipe the form.
  }, [open]);

  const choose = (c: AgentCatalogItem) => {
    setAgentId(c.id);
    setParams(defaults(c));
    setError(null);
  };

  const allowed = item ? can(user, item.perm) : false;
  const incomplete = item ? missing(item, params) : true;

  const start = async () => {
    if (!item) return;
    setBusy(true);
    setError(null);
    try {
      // Response shape isn't pinned: a started run (AgentRun) or an acknowledgement (OkResponse).
      const res = await post<Partial<AgentRun> & Partial<OkResponse>>('/api/agents/run', { agent: item.id, params });
      invalidate('agents/');
      invalidate('jobs/');
      onClose();
      if (res?.id && res.agent) route(runHref(res as AgentRun));
      else toast({ title: `${item.name} started`, body: res?.message ?? 'It shows up under Running in a moment.', tone: 'success' });
    } catch (e) {
      setError(toHumanError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Run an agent"
      footer={
        item && (
          <>
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" icon="play" loading={busy} disabled={!allowed || incomplete} onClick={() => void start()} subtitle="Runs in the background · shows under Running">
              Start {item.name}
            </Button>
          </>
        )
      }
    >
      <div class="stack">
        {handoff && (
          <div class="lz-run-handoff stack-sm" role="note">
            <p class="small">
              <strong>From Ask:</strong> “{handoff.length > 140 ? `${handoff.slice(0, 140)}…` : handoff}”
            </p>
            <p class="muted small">
              No agent here takes a free-form task yet: Model Scout and the Evaluator run fixed model checks. To keep working
              on this request, continue it in Ask.
            </p>
            <div>
              <Button size="sm" variant="secondary" icon="ask" onClick={() => { onClose(); history.back(); }}>
                Back to Ask
              </Button>
            </div>
          </div>
        )}
        {runnable.length === 0 ? (
          <p>No agent can be started right now.{unavailable.length ? ' The reasons are below.' : ''}</p>
        ) : (
          <fieldset class="lz-run-agents">
            <legend class="sr-only">Agent</legend>
            {runnable.map((c) => (
              <label key={c.id} class="lz-run-agent">
                <input type="radio" name="agent" checked={item?.id === c.id} onChange={() => choose(c)} />
                <span class="stack-sm">
                  <span class="lz-run-agent-name">{c.name}</span>
                  <span class="muted small">{c.description}</span>
                </span>
              </label>
            ))}
          </fieldset>
        )}

        {item && !allowed && <p class="small lz-tone-warning">This session can't start {item.name}. Sign in with an admin session to run it.</p>}

        {item && allowed && item.params.length > 0 && (
          <div class="stack-sm">
            {item.params.map((p) => (
              <ParamField key={p.key} param={p} value={params[p.key]} candidates={candidates} onChange={(v) => setParams((prev) => ({ ...prev, [p.key]: v }))} />
            ))}
          </div>
        )}

        {error && <HumanErrorCard error={error} compact />}

        {unavailable.length > 0 && (
          <section class="stack-sm">
            <h3 class="section-title">Not available</h3>
            <ul class="lz-run-unavailable">
              {unavailable.map((c) => (
                <li key={c.id}>
                  <span class="lz-run-agent-name">{c.name}</span>
                  <span class="muted small">{c.why_not ?? 'Not available yet.'}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </Sheet>
  );
}
