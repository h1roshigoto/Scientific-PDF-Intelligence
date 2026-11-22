import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { errorMessage } from '../data';
import type { StoredUpload, UploadClient } from '../data';
import type { Domain } from '../domain';
import { useData } from '../hooks/useData';
import { useUploadClient } from '../provider';
import { Badge } from './Badge';
import { Feedback, LoadBoundary } from './Feedback';
import { PdfPane } from './PdfPane';
import { UploadProcessingControls } from './UploadProcessing';

export type UploadCredentials = { token: string; setToken?: (token: string) => void };
const date = new Intl.DateTimeFormat('ru', { dateStyle: 'medium', timeStyle: 'medium' });
const sizeNumber = new Intl.NumberFormat('ru', { maximumFractionDigits: 2 });

function fileSize(bytes: number) {
  return bytes >= 1024 * 1024
    ? `${sizeNumber.format(bytes / (1024 * 1024))} МиБ`
    : `${sizeNumber.format(bytes)} Б`;
}

function AuthorizedUploads({ api, token, initial, maxBytes, lock }: {
  api: UploadClient; token: string; initial: StoredUpload[]; maxBytes: number; lock?: () => void;
}) {
  const [records, setRecords] = useState(initial);
  const [listRevision, setListRevision] = useState(0);
  const [file, setFile] = useState<File>();
  const [domain, setDomain] = useState<Domain | ''>('');
  const [busy, setBusy] = useState<'upload' | 'refresh'>();
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [view, setView] = useState<StoredUpload>();
  const fileInput = useRef<HTMLInputElement>(null);
  const operation = useRef<AbortController>(null);
  const invalidFile = file && (file.size > maxBytes || file.size === 0);
  const viewId = view?.upload_id;
  const loadPdf = useCallback((signal: AbortSignal) => api.pdf(viewId ?? '', token, signal), [api, token, viewId]);

  useEffect(() => () => operation.current?.abort(), []);

  async function run(kind: NonNullable<typeof busy>, work: (signal: AbortSignal) => Promise<void>) {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(kind);
    setError('');
    try {
      await work(controller.signal);
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setBusy(undefined);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!file || !domain || invalidFile || operation.current) return;
    setSaved(false);
    void run('upload', async (signal) => {
      const record = await api.create(file, domain, token, signal);
      signal.throwIfAborted();
      setRecords((previous) => [record, ...previous.filter((item) => item.upload_id !== record.upload_id)]);
      setSaved(true);
      setFile(undefined);
      if (fileInput.current) fileInput.current.value = '';
    });
  }

  function refresh() {
    void run('refresh', async (signal) => {
      const access = await api.list(token, signal);
      signal.throwIfAborted();
      if (access.kind === 'authorized') {
        setRecords(access.records);
        setListRevision((value) => value + 1);
      } else lock?.();
    });
  }

  return (
    <>
      <form className="upload-form" onSubmit={submit} aria-busy={busy === 'upload'}>
        <label>
          Файл PDF
          <input ref={fileInput} type="file" accept=".pdf,application/pdf" disabled={Boolean(busy)}
            onChange={(event) => {
              setFile(event.currentTarget.files?.[0]);
              setSaved(false);
              setError('');
            }} />
        </label>
        <label>
          Направление
          <select value={domain} disabled={Boolean(busy)}
            onChange={(event) => setDomain(event.currentTarget.value as Domain | '')}>
            <option value="">Выберите направление</option>
            <option value="solar">Solar</option>
            <option value="rag">RAG</option>
          </select>
        </label>
        <button className="button" disabled={!file || !domain || Boolean(busy) || invalidFile}>
          {busy === 'upload' ? 'Отправляем PDF…' : 'Загрузить PDF'}
        </button>
      </form>
      <p className="muted">Максимальный размер файла — {fileSize(maxBytes)}.</p>
      {file && <p className="file-name">{file.name} · {fileSize(file.size)}</p>}
      {invalidFile && <p role="alert">{file.size === 0 ? 'Выбранный PDF пуст.' : 'Файл превышает допустимый размер.'}</p>}
      {error && <p role="alert">{error}</p>}
      {saved && <p role="status">Файл сохранён.</p>}
      <div className="upload-actions">
        <button className="button" type="button" onClick={refresh} disabled={Boolean(busy)}>
          {busy === 'refresh' ? 'Обновляем список…' : 'Обновить список'}
        </button>
        {lock && <button className="button quiet" type="button" onClick={lock} disabled={Boolean(busy)}>Выйти</button>}
      </div>
      {records.length === 0 ? <p className="muted">Сохранённых PDF пока нет.</p> : (
        <ul className="upload-list" aria-label="Сохранённые PDF">
          {records.map((record) => (
            <li key={record.upload_id}>
              <div className="upload-record">
                <strong>{record.original_filename}</strong>
                <p className="muted">
                  {record.domain === 'solar' ? 'Solar' : 'RAG'} · {record.page_count} стр. · {fileSize(record.size_bytes)}
                  {' · '}<time dateTime={record.created_at}>{date.format(new Date(record.created_at))}</time>
                </p>
              </div>
              <Badge>{record.status}</Badge>
              <button className="button" type="button" aria-label={`Открыть оригинал ${record.original_filename}`}
                onClick={() => setView(record)}>Открыть оригинал</button>
              <UploadProcessingControls api={api} id={record.upload_id} token={token} listRevision={listRevision} />
            </li>
          ))}
        </ul>
      )}
      {view && (
        <div className="upload-preview">
          <button className="button quiet" type="button" onClick={() => setView(undefined)}>Закрыть PDF</button>
          <PdfPane key={view.upload_id} domain={view.domain} documentId={view.upload_id}
            label={view.original_filename} initialPage={1} highlightsResult={false} loadPdf={loadPdf} />
        </div>
      )}
    </>
  );
}

export function PrivateUploadAccess({ api, token, setToken, children }: UploadCredentials & {
  api: UploadClient; children: (records: StoredUpload[], maxBytes: number) => ReactNode;
}) {
  const load = useCallback((signal: AbortSignal) => api.list(token, signal), [api, token]);
  const access = useData(load);
  const tokenInput = useRef<HTMLInputElement>(null);

  return (
    <LoadBoundary {...access}>
      {(result) => {
        if (result.kind === 'unavailable') return <p className="muted" role="status">{result.message}</p>;
        if (result.kind === 'authorized') {
          return children(result.records, result.maxBytes);
        }
        if (!setToken) return <Feedback kind="error" title="Войдите, чтобы продолжить" />;
        return (
          <>
            {result.rejected && <p role="alert">Неверный токен доступа.</p>}
            <form className="upload-form" autoComplete="off" onSubmit={(event) => {
              event.preventDefault();
              const value = tokenInput.current?.value.trim();
              if (!value) return;
              if (value === token) access.retry();
              else setToken(value);
            }}>
              <label>
                Токен доступа
                <input ref={tokenInput} type="password" autoComplete="off" required />
              </label>
              <button className="button">Войти</button>
            </form>
          </>
        );
      }}
    </LoadBoundary>
  );
}

export function UploadPanel({ token, setToken }: UploadCredentials) {
  const api = useUploadClient();
  return (
    <section className="surface upload-panel" aria-labelledby="uploads-heading">
      <header className="section-bar"><h2 id="uploads-heading">Мои загрузки</h2></header>
      <div className="upload-content">
        {api ? <PrivateUploadAccess api={api} token={token} setToken={setToken}>
          {(records, maxBytes) => <AuthorizedUploads api={api} token={token}
            initial={records} maxBytes={maxBytes} lock={setToken ? () => setToken('') : undefined} />}
        </PrivateUploadAccess> : (
          <Feedback kind="empty" title="Загрузка PDF отключена в демонстрационном режиме" />
        )}
      </div>
    </section>
  );
}
