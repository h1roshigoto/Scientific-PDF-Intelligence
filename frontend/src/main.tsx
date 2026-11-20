import { lazy, StrictMode, Suspense, useCallback, useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/inter';
import { DataError } from './data';
import { belongsTo, domainName, publicationId, sourceLabel } from './domain';
import type { Domain, Filters } from './domain';
import { activeDomain, getFilters, Link, parseRoute, resultUrl, sectionUrl, useLocation } from './navigation';
import type { Section } from './navigation';
import { provider } from './provider';
import { InfoList, ResultDetail, ResultEvidence, ResultsPage } from './results';
import { ChecksPage, DocumentsPage, ExportPage } from './secondary-pages';
import { Feedback, Icon, LoadBoundary, PageHeading, Shell, useData } from './ui';
import './styles.css';

const PdfViewer = lazy(() => import('./pdf-viewer'));

function ResultRoute({ domain, id, source, filters }: { domain: Domain; id: string; source: boolean; filters: Filters }) {
  const load = useCallback((signal: AbortSignal) => provider.result(domain, id, signal), [domain, id]);
  const data = useData(load);
  return <LoadBoundary {...data}>{(result) => {
    if (!source) return <ResultDetail result={result} filters={filters} />;
    const label = `${domainName[domain]} / ${publicationId(result.document_id)}.pdf`;
    return <>
      <PageHeading title={label} subtitle={`Источник записи · ${sourceLabel(result.source)}`} />
      <div className="action-line"><Link className="button" href={resultUrl(domain, id, filters)}><Icon name="back" size={16} />Назад к записи</Link></div>
      <div className="split detail-layout">
        <Suspense fallback={<Feedback kind="loading" title="Загрузка PDF…" />}>
          <PdfViewer key={`${domain}:${id}`} domain={domain} documentId={result.document_id} initialPage={result.source.page} label={label} />
        </Suspense>
        <ResultEvidence result={result} />
      </div>
    </>;
  }}</LoadBoundary>;
}

function DocumentSource({ section, domain, documentId, page, selected }: { section: Section; domain: Domain; documentId: string; page: number; selected: string | null }) {
  const load = useCallback(async (signal: AbortSignal) => {
    if (!belongsTo(documentId, domain)) throw new DataError('Документ относится к другому треку.');
    const docs = await provider.documents(domain, signal);
    const doc = docs.find((entry) => entry.document_id === documentId);
    if (!doc) throw new DataError('В выбранном треке нет такого документа.');
    return doc;
  }, [domain, documentId]);
  const data = useData(load);
  return <LoadBoundary {...data}>{(doc) => {
    const label = `${domainName[domain]} / ${doc.filename ?? `${publicationId(doc.document_id)}.pdf`}`;
    return <>
      <PageHeading title={label} subtitle={doc.title ?? 'Просмотр источника'} />
      <div className="action-line"><Link className="button" href={sectionUrl(section, domain, undefined, selected)}><Icon name="back" size={16} />{section === 'checks' ? 'К проверкам' : 'К документам'}</Link></div>
      <div className="split detail-layout">
        <Suspense fallback={<Feedback kind="loading" title="Загрузка PDF…" />}>
          <PdfViewer key={`${documentId}:${page}`} domain={domain} documentId={documentId} initialPage={page} label={label} />
        </Suspense>
        <section className="panel inspector"><h2>Документ</h2><InfoList items={[
          ['Трек', domainName[domain]], ['Публикация', publicationId(doc.document_id)], ['Страниц', doc.page_count ?? '—'], ['DOI', doc.doi ?? doc.DOI ?? '—'],
        ]} /></section>
      </div>
    </>;
  }}</LoadBoundary>;
}

function App() {
  const url = useLocation();
  const previousPath = useRef(url.pathname);
  useEffect(() => {
    if (previousPath.current === url.pathname) return;
    previousPath.current = url.pathname;
    document.getElementById('main-content')?.focus({ preventScroll: true });
  }, [url.pathname]);
  const route = parseRoute(url);
  const domain = activeDomain(url.searchParams);
  const filters = getFilters(url.searchParams);
  const selected = url.searchParams.get('selected');
  let content;
  if (route.view === 'not-found') {
    content = <><PageHeading title="Страница не найдена" subtitle="Выберите раздел в меню или вернитесь к результатам." /><Link className="button primary" href={sectionUrl('results', domain)}>К результатам</Link></>;
  } else if (route.view === 'detail' || route.view === 'pdf') {
    content = <ResultRoute domain={domain} id={route.id} source={route.view === 'pdf'} filters={filters} />;
  } else if (route.view === 'document-pdf') {
    content = <DocumentSource section={route.section} domain={domain} documentId={route.documentId} page={route.page} selected={selected} />;
  } else if (route.section === 'documents') {
    content = <DocumentsPage domain={domain} query={filters.query} />;
  } else if (route.section === 'checks') {
    content = <ChecksPage domain={domain} selected={selected} />;
  } else if (route.section === 'export') {
    content = <ExportPage domain={domain} />;
  } else {
    content = <ResultsPage domain={domain} filters={filters} selectedId={selected} />;
  }
  return <Shell section={route.section} domain={domain} mock={provider.mode === 'mock'}>{content}</Shell>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
