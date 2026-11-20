import { useSyncExternalStore } from 'react';
import type { AnchorHTMLAttributes } from 'react';
import { domainSchema, emptyFilters } from './domain';
import type { Domain, Filters } from './domain';

const locationChange = 'spi:navigation';
const readLocation = () => window.location.pathname + window.location.search;

function subscribe(listener: () => void) {
  window.addEventListener('popstate', listener);
  window.addEventListener(locationChange, listener);
  return () => {
    window.removeEventListener('popstate', listener);
    window.removeEventListener(locationChange, listener);
  };
}

export function useLocation() {
  const address = useSyncExternalStore(subscribe, readLocation);
  return new URL(address, window.location.origin);
}

export function navigate(href: string, options: { replace?: boolean; scroll?: boolean } = {}) {
  const { replace = false, scroll = !replace } = options;
  if (href === readLocation()) return;
  if (replace) window.history.replaceState(null, '', href);
  else window.history.pushState(null, '', href);
  window.dispatchEvent(new Event(locationChange));
  if (scroll) window.scrollTo(0, 0);
}

export function Link({ href, className, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  return <a {...props} href={href} className={className} onClick={(event) => {
    props.onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || props.target) return;
    event.preventDefault();
    navigate(href);
  }}>{children}</a>;
}

export type Section = 'results' | 'documents' | 'checks' | 'export';
export type Route =
  | { section: Section; view: 'list' }
  | { section: 'results'; view: 'detail' | 'pdf'; id: string }
  | { section: Section; view: 'document-pdf'; documentId: string; page: number }
  | { section: 'results'; view: 'not-found' };

function decoded(segment: string) {
  try { return decodeURIComponent(segment); } catch { return ''; }
}

export function parseRoute(url: URL): Route {
  const { pathname, searchParams } = url;
  const [section, item, action, ...rest] = pathname.replace(/^\/|\/$/g, '').split('/');
  if (!section) return { section: 'results', view: 'list' };
  if (rest.length || !['results', 'documents', 'checks', 'export'].includes(section)) return { section: 'results', view: 'not-found' };
  if (!item) return { section: section as Section, view: 'list' };
  if (section === 'results' && (!action || action === 'source') && decoded(item)) {
    return { section, view: action ? 'pdf' : 'detail', id: decoded(item) };
  }
  if ((section === 'documents' || section === 'checks') && action === 'source' && decoded(item)) {
    const page = Number(searchParams.get('page') ?? 1);
    if (Number.isSafeInteger(page) && page > 0) return { section, view: 'document-pdf', documentId: decoded(item), page };
  }
  return { section: 'results', view: 'not-found' };
}

export function activeDomain(params: URLSearchParams): Domain {
  return domainSchema.safeParse(params.get('domain')).data ?? 'solar';
}

export function getFilters(params: URLSearchParams): Filters {
  return {
    query: params.get('q') ?? '', metric: params.get('metric') ?? '', subject: params.get('subject') ?? '',
    status: params.get('status') ?? '', source: params.get('source') ?? '',
  };
}

export function sectionUrl(section: Section, domain: Domain, filters: Filters = emptyFilters, selected?: string | null) {
  const params = new URLSearchParams({ domain });
  if (filters.query) params.set('q', filters.query);
  for (const key of ['metric', 'subject', 'status', 'source'] as const) if (filters[key]) params.set(key, filters[key]);
  if (selected) params.set('selected', selected);
  return `/${section}?${params}`;
}

export function resultUrl(domain: Domain, id: string, filters = emptyFilters, source = false) {
  const url = sectionUrl('results', domain, filters, id);
  return url.replace('/results?', `/results/${encodeURIComponent(id)}${source ? '/source' : ''}?`);
}

export function documentSourceUrl(section: 'checks' | 'documents', domain: Domain, id: string, page: number, selected?: string) {
  const url = sectionUrl(section, domain, emptyFilters, selected);
  return `${url.replace(`/${section}?`, `/${section}/${encodeURIComponent(id)}/source?`)}&page=${page}`;
}
