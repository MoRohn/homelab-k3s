// Home: the command center (§6). Desktop/tablet: the status block (eager, first paint), then the cockpit
// as its own chunk: KPI tiles with 6-hour trends, recent conversations, attention, services and grouped
// activity — each a drill-down into its area (§4). Compact widths get the Mobile Gateway instead (§12).
import { useSystemStatus } from '@/api/status';
import { applyDensity } from '@/pages/system/prefs';
import { useLazy } from '@/pages/system/shared';
import { useBreakpoint } from '@/shell/useBreakpoint';
import { usePageTitle } from '@/shell/usePageTitle';
import { HumanErrorCard, Skeleton } from '@/ui';
import { StatusPanel } from './StatusPanel';
import './home.css';

// Home is in the eager bundle, so the per-browser density preference applies before any page paints.
applyDensity();

function DesktopHome() {
  const status = useSystemStatus();
  const s = status.data;
  // The cockpit is its own chunk: the status block paints first, inside the initial-JS budget (§92).
  const Cockpit = useLazy(() => import('./Cockpit').then((m) => m.default));
  return (
    <div class="page page-wide lz-home">
      {!s && <h1 class="sr-only">Home</h1>}
      {!s && status.error ? (
        <HumanErrorCard error={status.error} onRetry={() => void status.refresh()} />
      ) : (
        <StatusPanel status={s} />
      )}
      {s && Cockpit ? (
        <Cockpit status={s} />
      ) : (
        <div class="lz-tiles" aria-hidden="true">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} height="112px" radius="var(--radius)" />
          ))}
        </div>
      )}
    </div>
  );
}

/** The Mobile Gateway (§12) is its own chunk, so desktop and tablet never download it. */
function CompactHome() {
  const MobileHome = useLazy(() => import('./MobileHome').then((m) => m.default));
  if (MobileHome) return <MobileHome />;
  return (
    <div class="page" aria-busy="true">
      <span class="sr-only">Loading Labzilla</span>
      <Skeleton height="48px" radius="var(--radius)" />
      <Skeleton lines={3} />
    </div>
  );
}

export default function Home() {
  usePageTitle('Home');
  return useBreakpoint() === 'compact' ? <CompactHome /> : <DesktopHome />;
}
