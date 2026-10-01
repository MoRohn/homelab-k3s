// Knowledge: project memory, not a graph database (§32). One search box that takes plain questions (§33);
// with no query, the home shows projects, decisions, assumptions, what needs review and recent changes.
// The mode banner is honest about where answers come from: the knowledge service, a read-only bundled
// copy of the public repos, or nothing at all.
import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso/router';
import { get, qs } from '@/api/client';
import type { KnowledgeHit, KnowledgeHome } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { Button, Card, EmptyState, HumanErrorCard, Icon, Input, Skeleton } from '@/ui';
import { HitList } from './HitList';
import './knowledge.css';

const EXAMPLES = [
  'Why are we using K3s?',
  'What depends on the GPU reserve?',
  'What happened in the last OOM incident?',
  'Which decisions changed this week?',
];

/** Home sections in the order of §32 / the brief; empty ones are hidden. */
const SECTIONS: { key: 'projects' | 'decisions' | 'assumptions' | 'needs_review' | 'recent_changes'; title: string; subtitle?: string; type?: string }[] = [
  { key: 'projects', title: 'Projects', type: 'project' },
  { key: 'decisions', title: 'Decisions', subtitle: 'Choices in effect and why they were made.', type: 'decision' },
  { key: 'assumptions', title: 'Assumptions', subtitle: 'What the decisions rely on still being true.', type: 'assumption' },
  { key: 'needs_review', title: 'Needs review', subtitle: 'Something these rely on changed.' },
  { key: 'recent_changes', title: 'Recent changes', subtitle: 'By date: knowledge records the day of a change, not the time.' },
];

const SHOWN = 6;

function ModeBanner({ home }: { home: KnowledgeHome }) {
  // 'unavailable' is explained by the empty state below; one message, not two.
  if (home.mode !== 'bundled') return null;
  const text =
    home.note ?? 'Read-only: showing the public knowledge bundled with Labzilla. Saving to Knowledge needs the knowledge service.';
  return (
    <p class="lz-k-banner" role="status">
      <Icon name="info" size={16} class="lz-tone-info" />
      <span>{text}</span>
    </p>
  );
}

function Section({ title, subtitle, hits, type, recent }: { title: string; subtitle?: string; hits: KnowledgeHit[]; type?: string; recent: boolean }) {
  const [all, setAll] = useState(false);
  const shown = all ? hits : hits.slice(0, SHOWN);
  return (
    <Card title={title} subtitle={subtitle} padded={false}>
      <HitList hits={shown} label={title} hideType={!!type} showDate={recent} />
      {hits.length > shown.length && (
        <div class="lz-k-hit">
          <Button size="sm" variant="ghost" onClick={() => setAll(true)}>
            Show all {hits.length}
          </Button>
        </div>
      )}
    </Card>
  );
}

function Results({ q }: { q: string }) {
  const { route } = useLocation();
  const res = useResource(`knowledge/search?${q}`, () => get<KnowledgeHit[]>(`/api/knowledge/search${qs({ q })}`), { maxAgeMs: 30_000 });
  if (res.error && !res.data) return <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />;
  if (!res.data)
    return (
      <div aria-busy="true">
        <Skeleton lines={4} />
      </div>
    );
  if (!res.data.length)
    return (
      <EmptyState
        icon="search"
        title={`Nothing found for “${q}”`}
        body="Try fewer words, or the name of a system or decision."
        action={{ label: 'Clear search', onClick: () => route('/knowledge', true) }}
      />
    );
  return (
    <Card title={`${res.data.length} result${res.data.length === 1 ? '' : 's'}`} padded={false}>
      <HitList hits={res.data} label="Search results" />
    </Card>
  );
}

export default function Knowledge() {
  usePageTitle('Knowledge');
  const { query, route } = useLocation();
  const q = (query.q ?? '').trim();
  const [text, setText] = useState(q);
  const input = useRef<HTMLInputElement>(null);
  const home = useResource('knowledge/home', () => get<KnowledgeHome>('/api/knowledge'), { maxAgeMs: 60_000 });
  const unavailable = home.data?.mode === 'unavailable';

  useEffect(() => setText(q), [q]);
  // The palette's "Search Knowledge" lands on ?search=1.
  useEffect(() => {
    if (query.search === '1') input.current?.focus();
  }, [query.search]);

  // replace, not push: Back from a result returns to the search, not through every query typed.
  const search = (value: string) => route(value.trim() ? `/knowledge${qs({ q: value.trim() })}` : '/knowledge', true);

  const data = home.data;
  const sections = data ? SECTIONS.filter((s) => data[s.key].length > 0) : [];

  return (
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Knowledge</h1>
          <p>Project memory: decisions, the evidence behind them, and what they assume.</p>
        </div>
      </header>

      <form
        role="search"
        class="lz-k-search"
        onSubmit={(e) => {
          e.preventDefault();
          search(text);
        }}
      >
        <Input
          type="search"
          label="Search knowledge"
          hideLabel
          icon="search"
          placeholder={`Try “${EXAMPLES[0]}”`}
          value={text}
          inputRef={input}
          enterKeyHint="search"
          autoComplete="off"
          disabled={unavailable}
          // A disabled input can't take focus: let "/" fall back to the command bar.
          data-slash-focus={unavailable ? undefined : ''}
          onInput={(e) => setText((e.currentTarget as HTMLInputElement).value)}
        />
        <Button type="submit" variant="secondary" disabled={unavailable || !text.trim()}>
          Search
        </Button>
      </form>

      {!q && !text && !unavailable && (
        <ul role="list" class="lz-k-examples" aria-label="Example questions">
          {EXAMPLES.slice(1).map((ex) => (
            <li key={ex}>
              <button type="button" class="lz-k-example" onClick={() => search(ex)}>
                {ex}
              </button>
            </li>
          ))}
        </ul>
      )}

      {data && <ModeBanner home={data} />}

      {q && !unavailable ? (
        <Results q={q} />
      ) : (
        <>
          {home.error && !data && <HumanErrorCard error={home.error} onRetry={() => void home.refresh()} />}
          {home.loading && (
            <div aria-busy="true" class="stack">
              <Skeleton height="6rem" />
              <Skeleton height="6rem" />
            </div>
          )}
          {data &&
            (unavailable ? (
              <EmptyState
                icon="knowledge"
                title="Knowledge isn't available"
                body={data.note ?? 'Neither the knowledge service nor a bundled copy can be read right now.'}
                action={{ label: 'Retry', icon: 'refresh', onClick: () => void home.refresh() }}
              />
            ) : sections.length === 0 ? (
              <EmptyState
                icon="knowledge"
                title="No project knowledge yet"
                body="Decisions, assumptions and evidence appear here once they are recorded in the knowledge repos."
                action={{ label: 'Ask Labzilla', href: '/ask', icon: 'ask' }}
              />
            ) : (
              sections.map((s) => <Section key={s.key} title={s.title} subtitle={s.subtitle} hits={data[s.key]} type={s.type} recent={s.key === 'recent_changes'} />)
            ))}
        </>
      )}
    </div>
  );
}
