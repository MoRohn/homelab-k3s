import { useEffect } from 'preact/hooks';

/** Sets document.title to "<title> · Labzilla" (screen readers announce it on navigation). */
export function usePageTitle(title: string | null | undefined): void {
  useEffect(() => {
    document.title = title ? `${title} · Labzilla` : 'Labzilla';
  }, [title]);
}
