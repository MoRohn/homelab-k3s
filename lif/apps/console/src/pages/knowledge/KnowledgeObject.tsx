// A knowledge object as something a person reads (§34). Decisions render as a decision record —
// Decision, Status, Why, Evidence, Assumptions (with whether they still hold), Affected systems,
// Alternatives, History — and every referenced object is a link, so a record leads to its evidence.
// Anything else renders as a generic readable object. Route /knowledge/o/:objectKey+ (`key` is reserved).
import type { ComponentChildren } from 'preact';
import { get } from '@/api/client';
import type { DecisionRecord, KnowledgeObjectResponse, KnowledgeObjectView } from '@/api/contracts.gen';
import { useResource } from '@/api/store';
import { usePageTitle } from '@/shell/usePageTitle';
import { Badge, Card, FactList, HumanErrorCard, Icon, Markdown, Skeleton } from '@/ui';
import { LinkChips, dateOnly, objectHref, statusTone, typeLabel } from './HitList';
import './knowledge.css';

const NONE = <p class="muted small">None recorded.</p>;

function Section({ title, children, tone }: { title: string; children: ComponentChildren; tone?: 'warning' }) {
  return (
    <Card title={title} tone={tone}>
      {children}
    </Card>
  );
}

function Decision({ d }: { d: DecisionRecord }) {
  return (
    <article class="stack lz-k-record">
      <header class="page-header">
        <div class="stack-sm">
          <p class="muted small">Decision record</p>
          <h1>{d.title}</h1>
          <div class="row wrap">
            <Badge tone={statusTone(d.status)}>{d.status}</Badge>
          </div>
        </div>
      </header>

      {d.reconsideration && (
        <Section title="Needs review" tone="warning">
          <p>{d.reconsideration}</p>
        </Section>
      )}

      {d.question && (
        <Section title="Question">
          <p>{d.question}</p>
        </Section>
      )}

      <Section title="Decision">
        <p>{d.decision || d.title}</p>
      </Section>

      <Section title="Why">
        {d.why.length ? (
          <ul class="lz-k-plain">
            {d.why.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        ) : (
          NONE
        )}
      </Section>

      <Section title="Evidence">
        {d.evidence.length ? (
          <ul role="list" class="lz-k-refs">
            {d.evidence.map((e) => (
              <li key={e.key}>
                <a href={objectHref(e.key)}>{e.title}</a>
                {e.summary && <span class="muted small">{e.summary}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p class="muted small">No evidence is linked to this decision yet.</p>
        )}
      </Section>

      <Section title="Assumptions">
        {d.assumptions.length ? (
          <ul role="list" class="lz-k-refs">
            {d.assumptions.map((a) => (
              <li key={a.key}>
                <span class="row-between wrap">
                  <a href={objectHref(a.key)}>{a.title}</a>
                  <Badge size="sm" tone={statusTone(a.status)}>
                    {a.status}
                  </Badge>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          NONE
        )}
      </Section>

      <Section title="Affected systems">
        {d.affected.length ? (
          <ul role="list" class="lz-k-chips">
            {d.affected.map((s) => (
              <li key={s}>
                <Badge appearance="outline">{s}</Badge>
              </li>
            ))}
          </ul>
        ) : (
          NONE
        )}
      </Section>

      <Section title="Alternatives considered">
        {d.alternatives.length ? (
          <ul class="lz-k-plain">
            {d.alternatives.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        ) : (
          NONE
        )}
      </Section>

      <Section title="History">
        {d.history.length ? (
          <ol class="lz-k-history">
            {d.history.map((h, i) => (
              <li key={`${h.ts}-${i}`}>
                <time class="muted small num" dateTime={h.ts}>
                  {dateOnly(h.ts)}
                </time>
                <span>{h.change}</span>
              </li>
            ))}
          </ol>
        ) : (
          <p class="muted small">No changes recorded since it was decided.</p>
        )}
      </Section>
    </article>
  );
}

function GenericObject({ o }: { o: KnowledgeObjectView }) {
  // Group links by relation so "supported by" and "depends on" read as sentences, not a tag cloud.
  const rels = [...new Set(o.links.map((l) => l.rel))];
  return (
    <article class="stack">
      <header class="page-header">
        <div class="stack-sm">
          <p class="muted small">{typeLabel(o.type)}</p>
          <h1>{o.title}</h1>
          {(o.status || o.updated) && (
            <div class="row wrap">
              {o.status && <Badge tone={statusTone(o.status)}>{o.status}</Badge>}
              {o.updated && (
                <span class="muted small num">
                  Updated <time dateTime={o.updated}>{dateOnly(o.updated)}</time>
                </span>
              )}
            </div>
          )}
        </div>
      </header>

      {(o.summary || o.body) && (
        <Card>
          <div class="stack-sm">
            {o.summary && <p>{o.summary}</p>}
            {o.body && <Markdown text={o.body} />}
          </div>
        </Card>
      )}

      {o.fields.length > 0 && (
        <Card title="Details">
          <FactList items={o.fields.map((f) => ({ label: f.label, value: f.value }))} columns={2} />
        </Card>
      )}

      {rels.length > 0 && (
        <Card title="Connected">
          <div class="stack-sm">
            {rels.map((rel) => (
              <div key={rel} class="stack-sm">
                <h3 class="section-title">{rel.charAt(0).toUpperCase() + rel.slice(1)}</h3>
                <LinkChips links={o.links.filter((l) => l.rel === rel).map((l) => ({ ...l, rel: '' }))} max={20} />
              </div>
            ))}
          </div>
        </Card>
      )}
    </article>
  );
}

export default function KnowledgeObject({ objectKey }: { objectKey?: string }) {
  // preact-iso already decoded the route param; encode exactly once for the API path.
  const res = useResource(objectKey ? `knowledge/o/${objectKey}` : null, () => get<KnowledgeObjectResponse>(`/api/knowledge/objects/${encodeURIComponent(objectKey ?? '')}`), {
    maxAgeMs: 60_000,
  });
  const data = res.data;
  const title = data?.decision?.title ?? data?.object?.title;
  usePageTitle(title ?? 'Knowledge');

  return (
    <div class="page">
      <a class="lz-crumb small" href="/knowledge">
        <Icon name="chevron-left" size={16} />
        Knowledge
      </a>
      {res.error && !data && <HumanErrorCard error={res.error} onRetry={() => void res.refresh()} />}
      {res.loading && (
        <div aria-busy="true" class="stack">
          <Skeleton height="2rem" width="70%" />
          <Skeleton lines={6} />
        </div>
      )}
      {data?.kind === 'decision' && data.decision && <Decision d={data.decision} />}
      {data?.kind === 'object' && data.object && <GenericObject o={data.object} />}
      {data && !data.decision && !data.object && <p class="muted">This object has no readable content.</p>}
    </div>
  );
}
