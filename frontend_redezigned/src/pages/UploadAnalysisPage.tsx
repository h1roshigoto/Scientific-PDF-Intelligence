import { useCallback } from 'react';
import { StatusBadge } from '../components/Badge';
import { Feedback, LoadBoundary } from '../components/Feedback';
import { InfoList } from '../components/InfoList';
import { PdfPane } from '../components/PdfPane';
import { PageHeading } from '../components/Shell';
import { PrivateUploadAccess } from '../components/UploadPanel';
import type { UploadCredentials } from '../components/UploadPanel';
import { UploadStatus } from '../components/UploadProcessing';
import type { StoredUpload, UploadClient } from '../data';
import { contextLabel, contextText, formatValue, sourceLabel } from '../domain';
import { useData } from '../hooks/useData';
import { Link, uploadAnalysisUrl } from '../navigation';
import { uploadClient } from '../provider';
import { metricTitle, ResultEvidence } from './ResultDetailPage';

function UploadAnalysis({ api, upload, token, resultId }: {
  api: UploadClient; upload: StoredUpload; token: string; resultId: string | null;
}) {
  const load = useCallback(async (signal: AbortSignal) => {
    const state = await api.processing(upload.upload_id, token, signal);
    if (!state.counts) return { state, results: [], issues: [] };
    const [results, issues] = await Promise.all([api.results(upload, token, signal), api.issues(upload, token, signal)]);
    return { state, results, issues };
  }, [api, upload, token]);
  const pdf = useCallback((signal: AbortSignal) => api.pdf(upload.upload_id, token, signal), [api, upload.upload_id, token]);
  const data = useData(load);
  const href = uploadAnalysisUrl(upload.upload_id);
  return (
    <LoadBoundary {...data}>
      {({ state, results, issues }) => {
        if (resultId) {
          const result = results.find((row) => row.result_id === resultId);
          if (!result) return <Feedback kind="empty" title="Научная запись недоступна">
            <Link href={href}>К результатам загрузки</Link>
          </Feedback>;
          return (
            <div className="split detail-layout">
              <PdfPane key={result.result_id} domain={upload.domain} documentId={result.document_id}
                initialPage={result.source.page} label={upload.original_filename} loadPdf={pdf} />
              <div className="stack">
                <ResultEvidence result={result}><Link href={href}>К результатам загрузки</Link></ResultEvidence>
                <section className="surface">
                  <header className="section-bar"><h2>Контекст записи</h2></header>
                  <InfoList items={[
                    ['Происхождение', result.source.origin === 'primary' ? 'Первичный результат'
                      : result.source.origin === 'cited'
                        ? ['Цитируемый результат', result.source.cited_publication_id].filter(Boolean).join(' · ')
                        : 'Требует уточнения'],
                    ...Object.entries(result.context).map(([key, value]): [string, string] => [contextLabel(key), contextText(value)]),
                  ]} />
                </section>
              </div>
            </div>
          );
        }
        return (
          <section className="surface" aria-label="Результаты анализа">
            <header className="section-bar"><h2>{upload.original_filename}</h2></header>
            <div className="upload-content">
              <UploadStatus state={state} />
              {state.mode === 'local' && <p className="muted">Локальный анализ извлекает значения из доступных таблиц.</p>}
              {state.human_review_required && <p>Научные записи и охват публикации требуют проверки человеком.</p>}
              {!state.counts && <button className="button" type="button" onClick={data.retry}>Обновить состояние</button>}
              {state.status === 'completed' && state.counts?.results === 0 && (
                <p>Анализ завершён, но подтверждённых научных результатов не обнаружено</p>
              )}
              {results.length > 0 && <ul className="upload-list" aria-label="Научные записи">
                {results.map((result) => <li key={result.result_id}>
                  <div className="upload-record">
                    <strong>{metricTitle(result)}</strong>
                    <p>{formatValue(result.raw, true)} · {sourceLabel(result.source)}</p>
                  </div>
                  <StatusBadge status={result.extraction_status} />
                  <Link className="button" href={uploadAnalysisUrl(upload.upload_id, result.result_id)}>Открыть источник</Link>
                </li>)}
              </ul>}
              {issues.length > 0 && <>
                <h3>Замечания</h3>
                <ul className="upload-issues">
                  {issues.map((issue) => <li key={issue.issue_id}>
                    <p>{issue.reason}</p>
                    {issue.evidence_refs.map((reference, index) => (
                      <p className="muted" key={index}>
                        {typeof reference.page === 'number' && `Стр. ${reference.page}`}
                        {typeof reference.fragment === 'string' && ` · ${reference.fragment}`}
                      </p>
                    ))}
                  </li>)}
                </ul>
              </>}
            </div>
          </section>
        );
      }}
    </LoadBoundary>
  );
}

export function UploadAnalysisPage({ uploadId, resultId, ...credentials }: UploadCredentials & {
  uploadId: string; resultId: string | null;
}) {
  const api = uploadClient;
  return (
    <>
      <PageHeading title={resultId ? 'Источник научной записи' : 'Результаты анализа PDF'}
        back={resultId ? { href: uploadAnalysisUrl(uploadId), label: 'К результатам загрузки' }
          : { href: '/documents', label: 'К моим загрузкам' }} />
      {api ? (
        <PrivateUploadAccess api={api} {...credentials}>
          {(records) => {
            const upload = records.find((row) => row.upload_id === uploadId);
            return upload ? <UploadAnalysis api={api} upload={upload} token={credentials.token} resultId={resultId} />
              : <Feedback kind="empty" title="Загрузка не найдена" />;
          }}
        </PrivateUploadAccess>
      ) : <Feedback kind="empty" title="Приватные загрузки недоступны" />}
    </>
  );
}
