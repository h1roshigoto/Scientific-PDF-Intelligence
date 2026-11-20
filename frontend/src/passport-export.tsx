import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from './data';
import { domainName, publicationId } from './domain';
import { saveFile } from './download';
import type { PassportExport, PassportScope } from './project-passport';
import { provider } from './provider';
import { Icon, LoadBoundary, useData } from './ui';

const publicationScopes = [
  ['all', 'Весь проект (Solar + RAG)'],
  ['solar', 'Solar'],
  ['rag', 'RAG'],
  ['publication', 'Одна публикация'],
] as const;
type Choice = typeof publicationScopes[number][0];

const generatedTime = new Intl.DateTimeFormat('ru-RU', {
  year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  timeZone: 'Europe/Moscow',
});

function PassportForm({ exporter }: { exporter: PassportExport }) {
  const [choice, selectChoice] = useState<Choice>('all');
  const [documentId, selectDocument] = useState('');
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: 'ready'; generatedAt: string | null } | { kind: 'error'; message: string }>();
  const activeRequest = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => activeRequest.current?.abort(), []);

  const load = useCallback(async (signal: AbortSignal) => {
    const [solarDocs, ragDocs, solarResults, ragResults] = await Promise.all([
      provider.documents('solar', signal), provider.documents('rag', signal),
      provider.results('solar', signal), provider.results('rag', signal),
    ]);
    return { documents: [...solarDocs, ...ragDocs], results: [...solarResults, ...ragResults] };
  }, []);
  const data = useData(load);

  async function download(scope: PassportScope) {
    if (activeRequest.current) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    setPending(true);
    setFeedback(undefined);
    try {
      const file = await exporter.download(scope, controller.signal);
      if (controller.signal.aborted) return;
      saveFile(file.blob, file.filename);
      setFeedback({ kind: 'ready', generatedAt: file.generatedAt });
    } catch (error) {
      if (!controller.signal.aborted) setFeedback({ kind: 'error', message: errorMessage(error) });
    } finally {
      activeRequest.current = undefined;
      if (!controller.signal.aborted) setPending(false);
    }
  }

  return <LoadBoundary {...data}>{({ documents, results }) => {
    const selected = documents.find((doc) => doc.document_id === documentId);
    const scope: PassportScope | undefined = choice === 'publication'
      ? selected && { domain: selected.domain, publicationId: publicationId(selected.document_id) }
      : { domain: choice };
    const documentCount = choice === 'publication' ? Number(Boolean(selected))
      : documents.filter((doc) => choice === 'all' || doc.domain === choice).length;
    const resultCount = results.filter((row) => choice === 'publication' ? row.document_id === selected?.document_id
      : choice === 'all' || row.domain === choice).length;

    return <form onSubmit={(event) => { event.preventDefault(); if (scope) void download(scope); }}>
      <fieldset className="passport-filters" disabled={pending}>
        <legend className="sr-only">Параметры экспорта PDF</legend>
        <label>Область экспорта
          <select value={choice} onChange={(event) => { selectChoice(event.target.value as Choice); setFeedback(undefined); }}>
            {publicationScopes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        {choice === 'publication' && <label>Публикация
          <select value={selected?.document_id ?? ''} onChange={(event) => { selectDocument(event.target.value); setFeedback(undefined); }}>
            <option value="">{documents.length ? 'Выберите публикацию' : 'Нет публикаций'}</option>
            {(['solar', 'rag'] as const).map((domain) => {
              const matches = documents.filter((doc) => doc.domain === domain);
              return matches.length > 0 && <optgroup key={domain} label={domainName[domain]}>{matches.map((doc) =>
                <option key={doc.document_id} value={doc.document_id}>
                  {domainName[domain]} / {publicationId(doc.document_id)}{doc.title ? ` · ${doc.title}` : ''}
                </option>)}</optgroup>;
            })}
          </select>
        </label>}
      </fieldset>
      <p className="passport-coverage muted" aria-live="polite">Публикаций: {documentCount} · Результатов: {resultCount} · Запуск {exporter.runId}</p>
      <button className="button primary" type="submit" disabled={!scope || pending} aria-busy={pending}>
        <Icon name="down" size={16} />Скачать паспорт проекта (PDF)
      </button>
      {pending && <p className="passport-feedback" role="status">Формируется PDF…</p>}
      {feedback?.kind === 'error' && <p className="passport-feedback download-status error" role="alert">{feedback.message}</p>}
      {feedback?.kind === 'ready' && <p className="passport-feedback download-status good" role="status">
        PDF готов. Скачивание началось.{feedback.generatedAt && <> Сформирован <time dateTime={feedback.generatedAt}>{generatedTime.format(new Date(feedback.generatedAt))}</time>.</>}
      </p>}
    </form>;
  }}</LoadBoundary>;
}

export function ProjectPassportPanel() {
  return <section className="panel export-panel" aria-labelledby="project-passport-heading">
    <h2 id="project-passport-heading">Паспорт проекта (PDF)</h2>
    <p className="muted">Полный отчёт по выбранной области с результатами, источниками и замечаниями.</p>
    {provider.projectPassport ? <PassportForm exporter={provider.projectPassport} />
      : <p className="muted" role="status">Серверный экспорт PDF пока недоступен.</p>}
  </section>;
}
