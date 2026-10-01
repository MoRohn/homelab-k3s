# Labzilla UI kit (`src/ui`)

A small library (spec §58, §85): pages import from `@/ui`. Preact only, no dependencies, inline SVG icons.
Styles live in `src/styles/components.css` (loaded once through `ui.css` from `main.tsx`), using tokens from `tokens.css`.

## Rules every component follows

| Rule | How |
|---|---|
| Color is never the only signal (§57) | Every tone ships with an icon and/or words (`HEALTH`, `SEVERITY` in `tone.ts`); StatusDot also changes shape. |
| Green is for brand, healthy, active, and primary actions only (§51) | Use `tone="brand"` or `success` only for those; neutral/inactive otherwise. |
| Touch targets ≥ 44px (§48) | `@media (pointer: coarse)` grows buttons, icon buttons, tabs, toggles, switch hit area. |
| Motion only for state (§59) | Spinner, progress, streaming caret, live pulse; all zeroed under `prefers-reduced-motion`. |
| Consequence before action (§110) | `Button subtitle`, ApprovalCard option descriptions, ConfirmDialog previews. |
| Raw terms only on demand (§42, §79) | Every domain object's `tech` goes to `TechDetails`; main UI uses human labels. |
| Honest unknowns (§56) | `fmt.*` render `—` for null; ResourceBar draws no segment for an unknown value. |

## Primitives

| Component | Notes |
|---|---|
| `Button` | `primary \| secondary \| ghost \| danger`, `sm \| md \| lg`, `loading` (aria-busy), `icon`/`iconRight`, `subtitle` = consequence line. |
| `IconButton` | Required `label` (aria-label + title), optional `badge` count, `pressed` (aria-pressed). |
| `Input`, `Textarea`, `Select` | Visible or sr-only label, hint/error wired with `aria-describedby`/`aria-invalid`; Textarea `autoGrow`; Select is native. |
| `Switch` | `role="switch"` button; label is a `<label for>` (tap the words); `busy` blocks input. |
| `Card`, `List`/`ListItem`, `FactList` | Sectioning with real headings; rows become links/buttons with `href`/`onClick`. |
| `Table` | Cards on compact widths (no horizontal scroll, §55); clickable rows use one stretched `<button>` named by the primary cell. |
| `Modal` → `Dialog`, `Drawer`, `Sheet` | Native `<dialog>.showModal()`: focus containment, Esc, inert background; plus aria-modal, return focus to opener, scroll lock, `[autofocus]` honored. |
| `Tabs` + `TabPanel` | Roving tabindex, ←/→/Home/End with automatic activation; panel ids `${idBase}-panel-${id}`. |
| `Badge`, `Tooltip` | Tooltip shows on hover **and** keyboard focus, Esc hides (WCAG 1.4.13); supplementary text only. |
| `Progress` | `aria-valuenow` when known; indeterminate omits it (still bar under reduced motion). |
| `CodeBlock` | Copy (works on insecure LAN origins, `clipboard.ts`), wrap toggle, no highlighter (§93). |
| `Toast` (`toast()`, `<Toaster/>`) | One polite live region; hover/focus pauses auto-dismiss; errors and toasts with actions stay 8 s. |
| `Skeleton`, `EmptyState` | Skeleton is aria-hidden (put `aria-busy` on the region); EmptyState always offers an action (§95). |
| `Icon` | 92 names incl. aliases (`spark`, `cube`, `queue`, `book`, `paperclip`, `chip`, `alert`, `dot`); 1.75 stroke, `currentColor`, decorative unless `label`. |
| `CommandInput`, `Markdown` | Command bar / gateway prompt; markdown-lite as VNodes (no innerHTML, only http(s) and same-origin links). |

## Domain components

| Component | Spec | What it shows |
|---|---|---|
| `StatusDot`, `StatusBadge` | §38, §57 | Health in words + icon/shape: Healthy, Busy, Degraded, Paused, Needs attention, Offline, Unknown. |
| `ResourceBar` | §36 | Segmented bar + legend; full breakdown as accessible text. |
| `ActivityRow` | §6 | Severity icon (labelled), title, detail, relative time. Renders an `<li>`. |
| `ApprovalCard` | §40 | Action / Why / Impact (inline `code` rendered), status in words, "Work is waiting" when blocking (said once: a review's Impact already says nothing waits), options with consequence lines. |
| `ModelCard` | §26 | Role · model · state; fallback cause with warning icon + words. |
| `AgentCard` | §22 | Agent, task, status, progress, and always the source line (runs are synthesized from real activity). |
| `JobRow` | §31 | Status + "Reason: …" + "Resumes automatically"; safe actions labelled per job. Renders an `<li>`. |
| `DecisionBadge` | §25 | Decision · confidence · decider, "Advice only" when not actionable, expandable "Why this route?". |
| `RouteTrail` | §11 | `Auto › Jev › Local Fast` inline; stacked (or `expandable`) with kind words; external hops flagged in words. |
| `PrivacyBadge` | §20, §64 | Local only / Local + Jev / External model used. |
| `TechDetails` | §42, §79 | Drawer (or inline `<details>`) of monospaced values with per-value copy and Copy all. |
| `ConfirmDialog` | §41, §98 | What changes / what may be interrupted / undo (or "No rollback is offered"); `typed` requires exact text; focus starts on Cancel. |
| `HumanErrorCard` | §81, §82 | Title, impact, next step, server-provided actions (`retry`, `login`, `reload`, `/path`), Technical details. |
| `Timeline` | §24 | `10:32 Repository scanned` steps with labelled kind icons; `live` announces new steps politely. |

## Helpers

`tone.ts` (`HEALTH`, `SEVERITY`, `cx`, `Tone`), `format.ts` as `fmt` (`num`, `percent`, `gb`, `ms`, `duration`, `ago`, `clock`, `iso`),
`clipboard.ts` (`copyText`, `useCopy`).
