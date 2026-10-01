// Unknown path inside the app: say so and offer the way back (§95).
import { EmptyState } from '@/ui/EmptyState';
import { usePageTitle } from '@/shell/usePageTitle';

export default function NotFound() {
  usePageTitle('Not found');
  return (
    <div class="page">
      <EmptyState icon="search" title="This page doesn't exist" body="The link may be old, or the item was removed." action={{ label: 'Go to Home', href: '/', icon: 'home' }} />
    </div>
  );
}
