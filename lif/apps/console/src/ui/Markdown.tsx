// Markdown-lite renderer for model answers and command-bar answers: paragraphs, headings, lists,
// **bold**, *italic*, `code`, ``` fences and [links](https://…). Builds VNodes — never innerHTML —
// so model output can't inject markup. Unclosed fences render as code while streaming.
import type { ComponentChildren } from 'preact';
import { CodeBlock } from './CodeBlock';
import { cx } from './tone';

export interface MarkdownProps {
  text: string;
  /** Show a blinking caret after the last block (§59: motion only for streaming). */
  streaming?: boolean;
  class?: string;
}

// Links: absolute http(s) or same-origin app paths only. `\/(?![\/\\])` rejects protocol-relative "//host" and
// "/\host" (browsers treat both as off-origin); javascript:, data: and other schemes never match and stay plain text.
const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*|\[[^\]]+\]\((?:https?:\/\/|\/(?![\/\\]))[^)\s\\]+\))/g;

/** Inline markdown only (`code`, **bold**, *italic*, safe links): for server text such as a decision question,
 *  where `model.id` placeholders should read as code instead of raw backticks. */
export function inlineMarkdown(text: string): ComponentChildren[] {
  return inline(text);
}

function inline(text: string): ComponentChildren[] {
  const out: ComponentChildren[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const tok = m[0];
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    if (tok.startsWith('`')) out.push(<code>{tok.slice(1, -1)}</code>);
    else if (tok.startsWith('**')) out.push(<strong>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith('*')) out.push(<em>{tok.slice(1, -1)}</em>);
    else {
      const [, label, href] = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok) ?? [];
      out.push(
        <a href={href} target={href?.startsWith('/') ? undefined : '_blank'} rel="noopener noreferrer">
          {label}
        </a>,
      );
    }
    last = at + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text, streaming, class: cls }: MarkdownProps) {
  const blocks: ComponentChildren[] = [];
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i] ?? '';
    const fence = /^```\s*([\w+-]*)/.exec(line);
    if (fence) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i] ?? '')) body.push(lines[i++] ?? '');
      i += 1;
      blocks.push(<CodeBlock key={key++} code={body.join('\n')} language={fence[1] || undefined} />);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const H = (heading[1]?.length ?? 1) <= 2 ? 'h3' : 'h4';
      blocks.push(<H key={key++}>{inline(heading[2] ?? '')}</H>);
      i += 1;
      continue;
    }
    if (/^\s*([-*]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i] ?? '')) items.push((lines[i++] ?? '').replace(/^\s*([-*]|\d+[.)])\s+/, ''));
      const L = ordered ? 'ol' : 'ul';
      blocks.push(<L key={key++}>{items.map((t, j) => <li key={j}>{inline(t)}</li>)}</L>);
      continue;
    }
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && (lines[i] ?? '').trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+[.)])\s+)/.test(lines[i] ?? '')) para.push(lines[i++] ?? '');
    blocks.push(<p key={key++}>{inline(para.join('\n'))}</p>);
  }
  return (
    <div class={cx('lz-md', cls)}>
      {blocks}
      {streaming && <span class="lz-caret" aria-hidden="true" />}
    </div>
  );
}
