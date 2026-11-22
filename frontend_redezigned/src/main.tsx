import { StrictMode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/golos-text';
import '@fontsource/pt-serif/400.css';
import '@fontsource/pt-serif/700.css';
import { Feedback, LoadBoundary } from './components/Feedback';
import { Shell, WorkspacePage } from './components/Shell';
import { UploadPanel } from './components/UploadPanel';
import { createAccountClient, createUploadClient, DataError, errorMessage } from './data';
import type { AccountSession } from './data';
import { useData } from './hooks/useData';
import { getFilters, Link, navigate, parseRoute, useLocation } from './navigation';
import { AccountPage, SignIn } from './pages/AccountPage';
import { ChecksPage } from './pages/ChecksPage';
import { DocumentsPage, DocumentSource } from './pages/DocumentsPage';
import { ExportPage } from './pages/ExportPage';
import { HomePage } from './pages/HomePage';
import { NotFoundPage, ResultRoute } from './pages/ResultDetailPage';
import { ResultsPage } from './pages/ResultsPage';
import { UploadAnalysisPage } from './pages/UploadAnalysisPage';
import { provider, UploadClientContext } from './provider';
import './styles/index.css';

function PrivateApp() {
  const url = useLocation();
  const [access, setAccess] = useState({ token: '', expired: false });
  const setToken = useCallback((token: string) => setAccess({ token, expired: false }), []);
  const uploads = useMemo(() => provider.mode === 'api' ? createUploadClient(fetch, {
    mode: 'private', onUnauthorized: (token) => {
      setAccess((current) => current.token === token ? { token: '', expired: true } : current);
    },
  }) : undefined, []);
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
  const credentials = { ...access, setToken };

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
  } else if (route.view === 'upload-analysis') {
    content = <UploadAnalysisPage key={route.uploadId} uploadId={route.uploadId} resultId={route.resultId} {...credentials} />;
  } else if (route.section === 'documents') {
    content = <DocumentsPage query={filters.query} {...credentials} />;
  } else if (route.section === 'checks') {
    content = <ChecksPage selected={selected} />;
  } else if (route.section === 'export') {
    content = <ExportPage />;
  } else {
    content = <ResultsPage filters={filters} selectedId={selected} />;
  }

  return (
    <UploadClientContext.Provider value={uploads}>
      <Shell section={route.section} mock={provider.mode === 'mock'}>{content}</Shell>
    </UploadClientContext.Provider>
  );
}

const accountApi = createAccountClient();
const ignore = () => {};

async function restoreSession(signal: AbortSignal) {
  try {
    return await accountApi.me(signal);
  } catch (error) {
    if (error instanceof DataError && error.status === 401) return null;
    throw error;
  }
}

function AccountWorkspace({ session, onExpired, onLogout }: {
  session: AccountSession; onExpired: () => void; onLogout: () => void;
}) {
  const url = useLocation();
  const route = parseRoute(url);
  const account = url.pathname === '/account';
  const auth = useMemo(() => createAccountClient(session.session_id, onExpired), [session.session_id, onExpired]);
  const uploads = useMemo(() => createUploadClient(fetch, {
    mode: 'multiuser', sessionId: session.session_id, onUnauthorized: onExpired,
  }), [session.session_id, onExpired]);

  useEffect(() => {
    const controller = new AbortController();
    const verify = () => { if (document.visibilityState === 'visible') void auth.me(controller.signal).catch(ignore); };
    document.addEventListener('visibilitychange', verify);
    window.addEventListener('focus', verify);
    return () => {
      controller.abort();
      document.removeEventListener('visibilitychange', verify);
      window.removeEventListener('focus', verify);
    };
  }, [auth]);

  let content;
  if (account) {
    content = <AccountPage session={session} api={auth} onLogout={onLogout} />;
  } else if (route.view === 'upload-analysis') {
    content = <UploadAnalysisPage key={route.uploadId} uploadId={route.uploadId} resultId={route.resultId} token="" />;
  } else if (route.view === 'home') {
    content = <WorkspacePage title="Scientific PDF Intelligence"
      subtitle="Загрузите научный PDF, запустите анализ и сверьте результаты с оригиналом.">
      <Link className="button primary" href="/documents">Загрузить PDF</Link>
    </WorkspacePage>;
  } else if (route.view === 'list' && route.section === 'documents') {
    content = <WorkspacePage title="Мои документы"><UploadPanel token="" /></WorkspacePage>;
  } else {
    content = <NotFoundPage />;
  }

  return (
    <UploadClientContext.Provider value={uploads}>
      <Shell section="documents" mock={false} account={{ selected: account, onLogout }}>{content}</Shell>
    </UploadClientContext.Provider>
  );
}

function MultiuserApp() {
  const restored = useData(restoreSession);
  const [override, setOverride] = useState<AccountSession | null>();
  const [closing, setClosing] = useState(false);
  const [closeError, setCloseError] = useState('');
  const channel = useRef<BroadcastChannel>(null);
  const logout = useRef<AbortController>(null);
  const expire = useCallback(() => setOverride(null), []);
  const current = override !== undefined ? override : restored.state.kind === 'ready' ? restored.state.data : null;

  useEffect(() => {
    channel.current = new BroadcastChannel('spi:account');
    channel.current.onmessage = () => window.location.reload();
    const restore = (event: PageTransitionEvent) => { if (event.persisted) window.location.reload(); };
    window.addEventListener('pageshow', restore);
    return () => {
      logout.current?.abort();
      channel.current?.close();
      channel.current = null;
      window.removeEventListener('pageshow', restore);
    };
  }, []);

  async function signOut() {
    if (!current || logout.current) return;
    const controller = new AbortController();
    logout.current = controller;
    setClosing(true);
    setCloseError('');
    try {
      await createAccountClient(current.session_id).logout(controller.signal);
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (!(cause instanceof DataError) || cause.status !== 401) {
        setCloseError(errorMessage(cause));
        return;
      }
    } finally {
      logout.current = null;
    }
    expire();
    setClosing(false);
    navigate('/documents', { replace: true });
    channel.current?.postMessage('changed');
  }

  if (closing) return <main className="main account-entry">
    {closeError ? <Feedback kind="error" title="Не удалось завершить сессию" retry={() => void signOut()}>{closeError}</Feedback>
      : <Feedback kind="loading" title="Завершаем сессию…" />}
  </main>;
  if (current) return <AccountWorkspace key={current.session_id} session={current} onExpired={expire} onLogout={() => void signOut()} />;
  const form = <SignIn onLogin={(session) => {
    setOverride(session);
    channel.current?.postMessage('changed');
  }} />;
  return override === null ? form : <LoadBoundary {...restored}>{() => form}</LoadBoundary>;
}

function ConfiguredApp() {
  const config = useData(accountApi.config);
  return <LoadBoundary {...config}>{({ mode }) => mode === 'multiuser' ? <MultiuserApp /> : <PrivateApp />}</LoadBoundary>;
}

function App() {
  return provider.mode === 'mock' || (import.meta.env.DEV && import.meta.env.VITE_SPI_AUTH_MODE === 'private')
    ? <PrivateApp /> : <ConfiguredApp />;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
