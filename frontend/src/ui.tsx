import { useEffect, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { errorMessage } from './data';
import { domainName, statusLabel } from './domain';
import type { Domain, Status } from './domain';
import { Link, navigate, sectionUrl } from './navigation';
import type { Section } from './navigation';

type IconName = 'grid' | 'file' | 'shield' | 'down' | 'search' | 'next' | 'back' | 'open' | 'alert' | 'layers' | 'check' | 'filter' | 'clock';
export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const style = { '--icon': `url(${import.meta.env.BASE_URL}assets/${name}.svg)`, '--icon-size': `${size}px` } as CSSProperties;
  return <span className="icon" style={style} aria-hidden="true" />;
}

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'info' | 'good' | 'warn' | 'error' }) {
  const glyph: Record<typeof tone, IconName> = { neutral: 'clock', info: 'file', good: 'check', warn: 'alert', error: 'alert' };
  return <span className={`badge ${tone}`}><Icon name={glyph[tone]} size={13} />{children}</span>;
}

export function StatusBadge({ status }: { status: Status }) {
  return <Badge tone={status === 'verified' ? 'good' : status === 'needs_review' ? 'warn' : 'info'}>{statusLabel[status]}</Badge>;
}

export function InfoList({ items }: { items: [string, ReactNode][] }) {
  return <dl className="info-list">{items.map(([label, value], index) => <div key={`${label}-${index}`}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

const navigation: { id: Section; label: string; icon: IconName }[] = [
  { id: 'results', label: 'Результаты', icon: 'grid' },
  { id: 'documents', label: 'Документы', icon: 'file' },
  { id: 'checks', label: 'Проверки', icon: 'shield' },
  { id: 'export', label: 'Экспорт', icon: 'down' },
];

export function Shell({ section, domain, mock, children }: { section: Section; domain: Domain; mock: boolean; children: ReactNode }) {
  const current = navigation.find((item) => item.id === section);
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">К содержимому</a>
    <aside className="sidebar">
      <Link className="brand" href={sectionUrl('results', domain)}>
        <span className="brand-icon"><Icon name="layers" size={24} /></span>
        <span>Scientific PDF<br className="brand-break" /> Intelligence</span>
      </Link>
      <div className="sidebar-label">Рабочая область</div>
      <nav aria-label="Главная навигация">
        {navigation.map((item) => <Link key={item.id} href={sectionUrl(item.id, domain)} aria-current={section === item.id ? 'page' : undefined}>
          <Icon name={item.icon} />{item.label}
        </Link>)}
      </nav>
      <div className="sidebar-footer"><strong>Корпус научных PDF</strong><span>Solar и RAG</span></div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <div className="breadcrumb"><span>Рабочая область</span><Icon name="next" size={13} /><strong>{current?.label}</strong></div>
        {mock ? <span className="demo-flag"><Icon name="file" size={13} />Демонстрационные данные</span>
          : <span className="workbench-label">Scientific Evidence Workbench</span>}
      </header>
      <main id="main-content" className="main" tabIndex={-1}>{children}</main>
    </div>
  </div>;
}

export function PageHeading({ title, subtitle, artwork = false }: { title: string; subtitle: string; artwork?: boolean }) {
  return <div className="page-heading">
    <h1>{title}</h1><p>{subtitle}</p>
    {artwork && <img className="theme-waves" alt="" src={`${import.meta.env.BASE_URL}assets/waves.svg`} width="440" height="100" />}
  </div>;
}

export function TrackTabs({ section, domain, children }: { section: Section; domain: Domain; children: ReactNode }) {
  function change(track: Domain, focus = false) {
    navigate(sectionUrl(section, track), { scroll: false });
    if (focus) document.getElementById(`track-${track}`)?.focus();
  }
  return <>
    <div className="track-tabs" role="tablist" aria-label="Направление анализа">
      {(['solar', 'rag'] as const).map((track) => <button key={track} type="button" id={`track-${track}`} role="tab" aria-controls="track-panel"
        aria-selected={domain === track} tabIndex={domain === track ? 0 : -1} onClick={() => change(track)}
        onKeyDown={(event) => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 'solar' : event.key === 'End' ? 'rag' : track === 'solar' ? 'rag' : 'solar';
          change(next, true);
        }}>{domainName[track]}</button>)}
    </div>
    <div id="track-panel" role="tabpanel" aria-labelledby={`track-${domain}`} tabIndex={0}>{children}</div>
  </>;
}

export function Ribbon({ title, children, warning = false }: { title: string; children: ReactNode; warning?: boolean }) {
  return <aside className={`panel ribbon${warning ? ' warning' : ''}`}>
    <span className="brand-icon"><Icon name={warning ? 'alert' : 'shield'} size={20} /></span><div><strong>{title}</strong><p>{children}</p></div>
  </aside>;
}

export type LoadState<T> = { kind: 'loading' } | { kind: 'error'; message: string } | { kind: 'ready'; data: T };

export function useData<T>(load: (signal: AbortSignal) => Promise<T>) {
  const [attempt, setAttempt] = useState(0);
  const [response, setResponse] = useState<{ load: typeof load; attempt: number; state: LoadState<T> }>();
  useEffect(() => {
    const controller = new AbortController();
    Promise.resolve().then(() => load(controller.signal)).then(
      (data) => { if (!controller.signal.aborted) setResponse({ load, attempt, state: { kind: 'ready', data } }); },
      (error: unknown) => { if (!controller.signal.aborted) setResponse({ load, attempt, state: { kind: 'error', message: errorMessage(error) } }); },
    );
    return () => controller.abort();
  }, [load, attempt]);
  const state: LoadState<T> = response?.load === load && response.attempt === attempt ? response.state : { kind: 'loading' };
  return { state, retry: () => setAttempt((value) => value + 1) };
}

export function Feedback({ kind, title, children, retry }: { kind: 'empty' | 'error' | 'loading'; title: string; children?: ReactNode; retry?: () => void }) {
  const glyph = kind === 'loading' ? 'clock' : kind === 'error' ? 'alert' : 'search';
  return <div className={`panel feedback ${kind}`} role={kind === 'error' ? 'alert' : 'status'} aria-busy={kind === 'loading'}>
    <span className="brand-icon"><Icon name={glyph} size={20} /></span>
    <div><h2>{title}</h2>{children && <p>{children}</p>}{retry && <button className="button" type="button" onClick={retry}>Повторить</button>}</div>
  </div>;
}

export function LoadBoundary<T>({ state, retry, children }: { state: LoadState<T>; retry: () => void; children: (data: T) => ReactNode }) {
  if (state.kind === 'loading') return <Feedback kind="loading" title="Загрузка данных…" />;
  if (state.kind === 'error') return <Feedback kind="error" title="Не удалось загрузить данные" retry={retry}>{state.message}</Feedback>;
  return children(state.data);
}
