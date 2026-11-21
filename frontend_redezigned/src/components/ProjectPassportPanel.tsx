import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../data';
import { publicationId } from '../domain';
import { saveFile } from '../download';
import { useData } from '../hooks/useData';
import type { PassportExport, PassportScope } from '../project-passport';
import { provider } from '../provider';
import { LoadBoundary } from './Feedback';
import { Icon } from './Icon';

const publicationScopes = [
  ['all', 'Весь проект'],
  ['publication', 'Одна публикация'],
] as const;

type Choice = (typeof publicationScopes)[number][0];

const generatedTime = new Intl.DateTimeFormat('ru-RU', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  timeZoneName: 'short',
  timeZone: 'Europe/Moscow',
});

function PassportForm({ exporter }: { exporter: PassportExport }) {
  const [choice, selectChoice] = useState<Choice>('all');
  const [documentId, selectDocument] = useState('');
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<
    { kind: 'ready'; generatedAt: string | null } | { kind: 'error'; message: string }
  >();
  const activeRequest = useRef<AbortController | undefined>(undefined);

  useEffect(() => () => activeRequest.current?.abort(), []);

  const load = useCallback(async (signal: AbortSignal) => {
    const [docs, rows] = await Promise.all([
      provider.documents(undefined, signal),
      provider.results(undefined, signal),
    ]);
    const documents = docs.filter((doc) => !doc.user_upload);
    const ids = new Set(documents.map((doc) => doc.document_id));
    return {
      documents,
      results: rows.filter((row) => ids.has(row.document_id)),
    };
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

  return (
    <LoadBoundary {...data}>
      {({ documents, results }) => {
        const selected = documents.find((doc) => doc.document_id === documentId);
        const scope: PassportScope | undefined =
          choice === 'publication'
            ? selected && { domain: selected.domain, publicationId: publicationId(selected.document_id) }
            : { domain: choice };
        const selectedDocuments = documents.filter((doc) =>
          choice === 'publication'
            ? doc.document_id === selected?.document_id
            : true,
        );
        const ocrPages = selectedDocuments.reduce(
          (count, doc) => count + doc.pages_needing_ocr.length,
          0,
        );
        const resultCount = results.filter((row) =>
          choice === 'publication'
            ? row.document_id === selected?.document_id
            : true,
        ).length;

        return (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (scope) void download(scope);
            }}
          >
            <fieldset className="passport-filters" disabled={pending}>
              <legend className="sr-only">Параметры экспорта PDF</legend>
              <label>
                Область экспорта
                <select
                  value={choice}
                  onChange={(event) => {
                    selectChoice(event.target.value as Choice);
                    setFeedback(undefined);
                  }}
                >
                  {publicationScopes.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              {choice === 'publication' && (
                <label>
                  Публикация
                  <select
                    value={selected?.document_id ?? ''}
                    onChange={(event) => {
                      selectDocument(event.target.value);
                      setFeedback(undefined);
                    }}
                  >
                    <option value="">
                      {documents.length ? 'Выберите публикацию' : 'Нет публикаций'}
                    </option>
                    {documents.map((doc) => (
                      <option key={doc.document_id} value={doc.document_id}>
                        {publicationId(doc.document_id)}
                        {doc.title || doc.filename ? ` · ${doc.title ?? doc.filename}` : ''}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </fieldset>
            <div className="passport-coverage muted" aria-live="polite">
              <p className="num">
                Публикаций: {selectedDocuments.length} · Результатов: {resultCount} · Запуск{' '}
                {exporter.runId}
              </p>
              {ocrPages > 0 && (
                <p className="ocr-caution">
                  Страниц в очереди OCR: {ocrPages}. Результаты по этим страницам могут быть неполными.
                </p>
              )}
            </div>
            <button
              className="button primary"
              type="submit"
              disabled={!scope || pending}
              aria-busy={pending}
            >
              <Icon name="down" size={14} />
              Скачать паспорт проекта (PDF)
            </button>
            {pending && (
              <p className="passport-feedback" role="status">
                Формируется PDF…
              </p>
            )}
            {feedback?.kind === 'error' && (
              <p className="passport-feedback download-status error" role="alert">
                {feedback.message}
              </p>
            )}
            {feedback?.kind === 'ready' && (
              <p className="passport-feedback download-status good" role="status">
                PDF готов. Скачивание началось.
                {feedback.generatedAt && (
                  <>
                    {' '}
                    Сформирован{' '}
                    <time dateTime={feedback.generatedAt}>
                      {generatedTime.format(new Date(feedback.generatedAt))}
                    </time>
                    .
                  </>
                )}
              </p>
            )}
          </form>
        );
      }}
    </LoadBoundary>
  );
}

export function ProjectPassportPanel() {
  return (
    <section className="surface export-panel" aria-labelledby="project-passport-heading">
      <div className="panel-intro">
        <h2 id="project-passport-heading">Паспорт проекта (PDF)</h2>
        <p className="muted">
          Полный отчёт по выбранной области с результатами, источниками и замечаниями.
        </p>
      </div>
      {provider.projectPassport ? (
        <PassportForm exporter={provider.projectPassport} />
      ) : (
        <p className="muted" role="status">
          Серверный экспорт PDF пока недоступен.
        </p>
      )}
    </section>
  );
}
