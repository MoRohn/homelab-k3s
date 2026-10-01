import type { ComponentChildren } from 'preact';
import { useId } from 'preact/hooks';
import { useBreakpoint } from '@/shell/useBreakpoint';
import { cx } from './tone';

export interface Column<Row> {
  key: string;
  header: string;
  render: (row: Row) => ComponentChildren;
  align?: 'start' | 'end';
  /** CSS width (e.g. '30%', '120px'). */
  width?: string;
  /** In card mode this column is the card title, and it names the row's open button (default: the first column). */
  primary?: boolean;
  /** Omit in card mode (secondary detail). */
  hideOnCompact?: boolean;
}

export interface TableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string;
  /** Row click — e.g. open detail. Keyboard and screen-reader users get a real button per row. */
  onRowClick?: (row: Row) => void;
  /** Render as stacked cards on compact widths: no horizontal scrolling for critical workflows (§55). Default true. */
  compactCards?: boolean;
  /** Visible or screen-reader caption. */
  caption?: string;
  hideCaption?: boolean;
  density?: 'normal' | 'compact';
  /** Shown instead of the table when rows is empty (use an EmptyState with an action, §95). */
  empty?: ComponentChildren;
  /** Accessible verb for the row button, read before the primary cell ("Open", "Compare"). Default "Open". */
  openLabel?: string;
  class?: string;
}

/** Tables only where comparison matters (models, jobs, benchmarks, agents — §55).
 *  Clickable rows get a real <button> labelled "Open <primary cell>" stretched over the primary cell
 *  (rows aren't interactive roles, so a tabindex'd <tr> is announced as plain text). Mouse clicks on the
 *  rest of the row are handled on the <tr>, except clicks that start on a control inside a cell. */
export function Table<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  compactCards = true,
  caption,
  hideCaption,
  density = 'compact',
  empty,
  openLabel = 'Open',
  class: cls,
}: TableProps<Row>) {
  const bp = useBreakpoint();
  const uid = useId();
  if (!rows.length && empty) return <>{empty}</>;
  const primary = columns.find((c) => c.primary) ?? columns[0];
  const verbId = `${uid}-verb`;
  const verb = onRowClick && (
    <span id={verbId} hidden>
      {openLabel}
    </span>
  );
  const rowClick = (row: Row) => (e: MouseEvent) => {
    if (!onRowClick || (e.target as Element).closest('a, button, input, select, textarea, summary, label')) return;
    onRowClick(row);
  };
  const openButton = (row: Row, titleId: string) =>
    onRowClick && <button type="button" class="lz-row-open" aria-labelledby={`${verbId} ${titleId}`} onClick={() => onRowClick(row)} />;

  if (compactCards && bp === 'compact') {
    const rest = columns.filter((c) => c !== primary && !c.hideOnCompact);
    return (
      <>
        {verb}
        {caption && !hideCaption && <p class="lz-table-caption">{caption}</p>}
        <ul role="list" class={cx('lz-table-cards', cls)} aria-label={caption}>
          {rows.map((row, i) => {
            const titleId = `${uid}-r${i}`;
            return (
              <li key={rowKey(row)} class={cx('lz-table-card', onRowClick && 'interactive')}>
                {primary && (
                  <div id={titleId} class="lz-table-card-title">
                    {primary.render(row)}
                  </div>
                )}
                {rest.length > 0 && (
                  <dl class="lz-table-card-fields">
                    {rest.map((c) => (
                      <div key={c.key} class={c.align === 'end' ? 'end' : undefined}>
                        <dt>{c.header}</dt>
                        <dd>{c.render(row)}</dd>
                      </div>
                    ))}
                  </dl>
                )}
                {openButton(row, titleId)}
              </li>
            );
          })}
        </ul>
      </>
    );
  }
  return (
    <div class={cx('lz-table-wrap', `density-${density}`, cls)}>
      {verb}
      <table class="lz-table">
        {caption && <caption class={hideCaption ? 'sr-only' : undefined}>{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} scope="col" style={c.width ? { width: c.width } : undefined} class={c.align === 'end' ? 'end' : undefined}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const titleId = `${uid}-r${i}`;
            return (
              <tr key={rowKey(row)} class={onRowClick ? 'interactive' : undefined} onClick={onRowClick ? rowClick(row) : undefined}>
                {columns.map((c) =>
                  c === primary ? (
                    <th key={c.key} scope="row" class={cx('lz-table-primary', c.align === 'end' && 'end num')}>
                      <span id={titleId}>{c.render(row)}</span>
                      {openButton(row, titleId)}
                    </th>
                  ) : (
                    <td key={c.key} class={c.align === 'end' ? 'end num' : undefined}>
                      {c.render(row)}
                    </td>
                  ),
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
