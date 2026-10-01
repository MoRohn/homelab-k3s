// Knowledge results that connect objects (§33): each hit links to its page and shows the objects it is
// tied to ("supported by", "depends on", "affects") as chips, so an answer leads to its evidence.
import type { KnowledgeHit, KnowledgeLink } from '@/api/contracts.gen';
import { Badge, type Tone } from '@/ui';

export const objectHref = (key: string) => `/knowledge/o/${encodeURIComponent(key)}`;

const TYPE: Record<string, string> = {
  decision: 'Decision',
  assumption: 'Assumption',
  evidence: 'Evidence',
  source: 'Source',
  project: 'Project',
  incident: 'Incident',
  change: 'Change',
  task: 'Task',
  question: 'Question',
  lesson: 'Lesson',
  service: 'Service',
  host: 'Host',
  review: 'Review',
  session: 'Session',
};

export function typeLabel(type: string): string {
  return TYPE[type] ?? type.charAt(0).toUpperCase() + type.slice(1).replace(/[-_]/g, ' ');
}

/** Status words come from the server already humanized; tone only reinforces them (never color alone, §57). */
export function statusTone(status: string): Tone {
  const s = status.toLowerCase();
  if (/no longer|invalid|rejected|failed|ongoing|blocked/.test(s)) return 'danger';
  if (/question|review|proposed|replaced|deprecated|challenged|open/.test(s)) return 'warning';
  if (/in effect|holding|done|resolved|answered|accepted|still valid/.test(s)) return 'success';
  return 'inactive';
}

/** Knowledge records dates, not times ("2026-09-30"): show the date, never "5 min ago". */
export function dateOnly(isoDate: string | null | undefined): string {
  if (!isoDate) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(isoDate);
  if (!m) return isoDate;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

export function LinkChips({ links, max = 4 }: { links: KnowledgeLink[]; max?: number }) {
  if (!links.length) return null;
  const shown = links.slice(0, max);
  return (
    <ul role="list" class="lz-k-chips" aria-label="Connected">
      {shown.map((l) => (
        <li key={`${l.rel}-${l.key}`}>
          <a class="lz-k-chip" href={objectHref(l.key)}>
            {l.rel && <span class="muted">{l.rel}</span>}
            {l.title}
          </a>
        </li>
      ))}
      {links.length > shown.length && <li class="muted xsmall lz-k-more">+{links.length - shown.length} more</li>}
    </ul>
  );
}

export interface HitListProps {
  hits: KnowledgeHit[];
  label: string;
  /** Show the updated date (Recent changes). */
  showDate?: boolean;
  /** Hide the type badge when the section already says it ("Decisions"). */
  hideType?: boolean;
}

export function HitList({ hits, label, showDate, hideType }: HitListProps) {
  return (
    <ul role="list" class="lz-k-hits" aria-label={label}>
      {hits.map((h) => (
        <li key={h.key} class="lz-k-hit">
          <div class="row-between wrap">
            <a class="lz-k-hit-title" href={objectHref(h.key)}>
              {h.title}
            </a>
            <span class="row wrap">
              {!hideType && (
                <Badge size="sm" appearance="outline">
                  {typeLabel(h.type)}
                </Badge>
              )}
              {h.status && (
                <Badge size="sm" tone={statusTone(h.status)}>
                  {h.status}
                </Badge>
              )}
            </span>
          </div>
          {h.summary && <p class="lz-k-summary small">{h.summary}</p>}
          {showDate && h.updated && (
            <p class="muted xsmall num">
              Updated <time dateTime={h.updated}>{dateOnly(h.updated)}</time>
            </p>
          )}
          <LinkChips links={h.links} />
        </li>
      ))}
    </ul>
  );
}
