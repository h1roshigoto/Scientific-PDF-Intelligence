import { useCallback, useState } from 'react';
import { StatusBadge } from '../components/Badge';
import { Feedback, LoadBoundary } from '../components/Feedback';
import { PageHeading } from '../components/Shell';
import { UploadExport } from '../components/UploadExport';
import { PrivateUploadAccess } from '../components/UploadPanel';
import type { UploadCredentials } from '../components/UploadPanel';
import { UploadStatus } from '../components/UploadProcessing';
import { ReviewBadge, ReviewWarnings, UploadReview } from '../components/UploadReview';
import type { StoredUpload, UploadClient } from '../data';
import { formatValue, sourceLabel } from '../domain';
import { useData } from '../hooks/useData';
import { Link, uploadAnalysisUrl } from '../navigation';
import { useUploadClient } from '../provider';
import { isApproved } from '../upload-review';
import { metricTitle } from './ResultDetailPage';

type UploadViewProps = { api: UploadClient; upload: StoredUpload; token: string };

function UploadResultReview({ api, upload, token, resultId }: UploadViewProps & { resultId: string }) {
  const load = useCallback((signal: AbortSignal) => api.review(upload.upload_id, resultId, token, signal),
    [api, upload.upload_id, resultId, token]);
  const data = useData(load);
  return <LoadBoundary {...data}>{(detail) => (
    <UploadReview key={detail.result_id} api={api} upload={upload} token={token} initial={detail} onRefresh={data.retry} />
  )}</LoadBoundary>;
}

function UploadAnalysis({ api, upload, token }: UploadViewProps) {
  const load = useCallback((signal: AbortSignal) => api.reviews(upload.upload_id, token, signal), [api, upload.upload_id, token]);
  const data = useData(load);
  const [filter, setFilter] = useState('all');
  return (
    <LoadBoundary {...data}>
      {({ processing: state, reviews, issues, pdf_status: pdf }) => {
        const approved = reviews.filter((review) => isApproved(review.status)).length;
        const rejected = reviews.filter((review) => review.status === 'rejected').length;
        const visible = reviews.filter((review) => {
          if (filter === 'rejected') return review.status === 'rejected';
          if (filter === 'approved') return isApproved(review.status);
          if (filter === 'pending') return !isApproved(review.status) && review.status !== 'rejected';
          return true;
        });
        return (
          <section className="surface" aria-label="Результаты анализа">
            <header className="section-bar"><h2>{upload.original_filename}</h2></header>
            <div className="upload-content">
              <UploadStatus state={state} />
              {state.mode === 'local' && <p className="muted">Локальный анализ извлекает значения из доступных таблиц.</p>}
              {state.human_review_required && <p>Научные записи и охват публикации требуют проверки человеком.</p>}
              {!state.counts && <button className="button" type="button" onClick={data.retry}>Обновить состояние</button>}
              {pdf !== 'valid' && <p role="alert">Оригинал PDF отсутствует или его SHA-256 изменился.</p>}
              {state.status === 'completed' && state.counts?.results === 0 && <p>Научные записи не извлечены.</p>}
              {reviews.length > 0 && <div className="upload-form review-filters">
                <label>Статус проверки<select value={filter} onChange={(event) => setFilter(event.target.value)}>
                  <option value="all">Все</option><option value="pending">Ожидают проверки</option>
                  <option value="approved">Подтверждены</option><option value="rejected">Отклонены</option>
                </select></label>
                <p>Проверено — {approved + rejected} · Ожидают проверки — {reviews.length - approved - rejected}
                  {' · '}Подтверждено — {approved} · Отклонено — {rejected}</p>
              </div>}
              {reviews.length > 0 && !visible.length && <p>Записей с этим статусом нет.</p>}
              {visible.length > 0 && <ul className="upload-list" aria-label="Научные записи">
                {visible.map(({ result_id: id, original_result: result, status, decision }) => <li key={id}>
                  <div className="upload-record">
                    <strong>{result ? metricTitle(result) : 'Запись из предыдущего анализа'}</strong>
                    {result && <p>{formatValue(result.raw, true)} · {sourceLabel(result.source)}</p>}
                    {status === 'approved_corrected' && decision?.reviewed_result && <p>
                      Подтверждено · {formatValue(decision.reviewed_result.raw, true)}
                    </p>}
                  </div>
                  {result && <StatusBadge status={result.extraction_status} />}
                  <ReviewBadge status={status} />
                  <Link className="button" href={uploadAnalysisUrl(upload.upload_id, id)}>{result ? 'Открыть источник' : 'Открыть историю'}</Link>
                </li>)}
              </ul>}
              <ReviewWarnings issues={issues} />
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
  const api = useUploadClient();
  return (
    <>
      <PageHeading title={resultId ? 'Источник научной записи' : 'Результаты анализа PDF'}
        back={resultId ? { href: uploadAnalysisUrl(uploadId), label: 'К результатам загрузки' }
          : { href: '/documents', label: 'К моим загрузкам' }} />
      {api ? (
        <PrivateUploadAccess api={api} {...credentials}>
          {(records) => {
            const upload = records.find((row) => row.upload_id === uploadId);
            if (!upload) return <Feedback kind="empty" title="Загрузка не найдена" />;
            const props = { api, upload, token: credentials.token };
            return resultId ? <UploadResultReview {...props} resultId={resultId} /> : <>
              <UploadAnalysis {...props} />
              <UploadExport key={upload.upload_id} {...props} />
            </>;
          }}
        </PrivateUploadAccess>
      ) : <Feedback kind="empty" title="Приватные загрузки недоступны" />}
    </>
  );
}
