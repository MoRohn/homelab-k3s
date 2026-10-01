import { useState } from 'preact/hooks';
import type { TechDetail } from '@/api/contracts.gen';
import { Button } from './Button';
import { useCopy } from './clipboard';
import { CodeBlock } from './CodeBlock';
import { Drawer } from './Drawer';
import { IconButton } from './IconButton';

export interface TechDetailsProps {
  items: TechDetail[];
  /** Drawer title (default "Technical details"). */
  title?: string;
  /** Trigger text (default "Technical details"). */
  triggerLabel?: string;
  /** Render inline as a <details> disclosure instead of a drawer (inside cards/receipts). */
  inline?: boolean;
  /** Extra raw text (e.g. a log tail) shown as a code block under the items. */
  raw?: string;
  /** Short sentence above the values in the drawer ("Raw states as reported by Kubernetes"). */
  intro?: string;
}

function Row({ item }: { item: TechDetail }) {
  const [copied, copy] = useCopy();
  return (
    <div class="lz-tech-row">
      <dt>{item.label}</dt>
      <dd>
        <span class="mono lz-tech-value">{item.value}</span>
        <IconButton
          icon={copied === 'ok' ? 'check' : 'copy'}
          label={copied === 'ok' ? `Copied ${item.label}` : `Copy ${item.label}`}
          size="sm"
          class="lz-tech-copy"
          onClick={() => copy(item.value)}
        />
      </dd>
    </div>
  );
}

/** The one place raw states, pod names, namespaces, revisions and request ids appear (§42, §79, §97).
 *  Nothing renders when there is nothing to show, so callers can always pass `tech`. */
export function TechDetails({ items, title = 'Technical details', triggerLabel = 'Technical details', inline, raw, intro }: TechDetailsProps) {
  const [open, setOpen] = useState(false);
  const [copiedAll, copyAll] = useCopy();
  if (!items.length && !raw) return null;
  const allText = [...items.map((t) => `${t.label}: ${t.value}`), ...(raw ? ['', raw] : [])].join('\n');
  const body = (
    <div class="lz-tech-body">
      {items.length > 0 && (
        <dl class="lz-tech">
          {items.map((t, i) => (
            <Row key={`${t.label}-${i}`} item={t} />
          ))}
        </dl>
      )}
      {raw && <CodeBlock code={raw} maxHeight="40vh" />}
    </div>
  );
  if (inline)
    return (
      <details class="lz-tech-inline">
        <summary>{triggerLabel}</summary>
        {body}
      </details>
    );
  return (
    <>
      <Button size="sm" variant="ghost" icon="info" onClick={() => setOpen(true)} aria-haspopup="dialog">
        {triggerLabel}
      </Button>
      <Drawer
        open={open}
        onClose={() => setOpen(false)}
        title={title}
        subtitle={intro}
        footer={
          <Button size="sm" icon={copiedAll === 'ok' ? 'check' : 'copy'} onClick={() => copyAll(allText)}>
            {copiedAll === 'ok' ? 'Copied' : 'Copy all'}
          </Button>
        }
      >
        {body}
      </Drawer>
    </>
  );
}
