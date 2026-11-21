import { StrictMode, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/golos-text';
import '@fontsource/pt-serif/400.css';
import '@fontsource/pt-serif/700.css';
import { Shell } from './components/Shell';
import { getFilters, parseRoute, useLocation } from './navigation';
import { ChecksPage } from './pages/ChecksPage';
import { DocumentsPage, DocumentSource } from './pages/DocumentsPage';
import { ExportPage } from './pages/ExportPage';
import { HomePage } from './pages/HomePage';
import { NotFoundPage, ResultRoute } from './pages/ResultDetailPage';
import { ResultsPage } from './pages/ResultsPage';
import { provider } from './provider';
import './styles/index.css';

function App() {
  const url = useLocation();
  const [uploadToken, setUploadToken] = useState('');
  const previousPath = useRef(url.pathname);

  useEffect(() => {
    if (previousPath.current === url.pathname) return;
    previousPath.current = url.pathname;
    document.getElementById('main-content')?.focus({ preventScroll: true });
  }, [url.pathname]);

  const route = parseRoute(url);
  if (route.view === 'home') return <HomePage />;

  const filters = getFilters(url.searchParams);
  const selected = url.searchParams.get('selected');

  let content;
  if (route.view === 'not-found') {
    content = <NotFoundPage />;
  } else if (route.view === 'detail' || route.view === 'pdf') {
    content = (
      <ResultRoute id={route.id} source={route.view === 'pdf'} filters={filters} />
    );
  } else if (route.view === 'document-pdf') {
    content = (
      <DocumentSource
        section={route.section}
        documentId={route.documentId}
        page={route.page}
        selected={selected}
      />
    );
  } else if (route.section === 'documents') {
    content = <DocumentsPage query={filters.query} uploadToken={uploadToken} setUploadToken={setUploadToken} />;
  } else if (route.section === 'checks') {
    content = <ChecksPage selected={selected} />;
  } else if (route.section === 'export') {
    content = <ExportPage />;
  } else {
    content = <ResultsPage filters={filters} selectedId={selected} />;
  }

  return (
    <Shell section={route.section} mock={provider.mode === 'mock'}>
      {content}
    </Shell>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
