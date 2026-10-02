/**
 * A link target from API data, or null when it could run script: only relative paths and http(s)
 * URLs are rendered as links (a `javascript:` or `data:` href would execute on click).
 */
export function safeHref(href: string | null | undefined): string | null {
  if (!href) return null;
  try {
    const url = new URL(href, location.origin);
    return url.protocol === 'http:' || url.protocol === 'https:' ? href : null;
  } catch {
    return null;
  }
}
