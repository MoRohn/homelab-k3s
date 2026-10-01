// Home: the command center (§6). Desktop/tablet: one status block, an attention strip only when
// something needs action (§39), and one activity list — everything else is a drill-down (§4).
// Compact widths get the Mobile Gateway instead (§12), loaded as its own chunk.
import { useSystemStatus } from '@/api/status';
import { applyDensity } from '@/pages/system/prefs';
import { useLazy } from '@/pages/system/shared';
import { useBreakpoint } from '@/shell/useBreakpoint';
import { usePageTitle } from '@/shell/usePageTitle';
import { HumanErrorCard, Skeleton } from '@/ui';
import { Attention } from './Attention';
import { RecentActivity } from './RecentActivity';
import { StatusPanel } from './StatusPanel';
import './home.css';

// Home is in the eager bundle, so the per-browser density preference applies before any page paints.
applyDensity();

function DesktopHome() {
  const status = useSystemStatus();
  const s = status.data;
  return (
    <div class="page lz-home">
      {!s && <h1 class="sr-only">Home</h1>}
      {!s && status.error ? (
        <HumanErrorCard error={status.error} onRetry={() => void status.refresh()} />
      ) : (
        <StatusPanel status={s} />
      )}
      {s && <Attention status={s} />}
      <RecentActivity />
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
