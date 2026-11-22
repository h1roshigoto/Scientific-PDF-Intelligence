import { useCallback, useEffect, useRef, useState } from 'react';
import { DataError, errorMessage } from '../data';
import type { ReviewDetail, ReviewEvent, StoredUpload, UploadClient } from '../data';
import { contextLabel, contextText, formatValue, sourceLabel } from '../domain';
import type { Issue } from '../domain';
import { useData } from '../hooks/useData';
import { Link, uploadAnalysisUrl } from '../navigation';
import { metricTitle, ResultEvidence } from '../pages/ResultDetailPage';
import { checkValues } from '../review';
import { isApproved, reviewLabels } from '../upload-review';
import type { Correction, EvidenceMode, ReviewAction, ReviewRequest, ReviewState } from '../upload-review';
import { Badge } from './Badge';
import { CorrectionForm, ReviewChanges } from './CorrectionForm';
import { LoadBoundary } from './Feedback';
import { InfoList } from './InfoList';
import { PdfPane } from './PdfPane';

const date = new Intl.DateTimeFormat('ru', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' });

export function ReviewBadge({ status }: { status: ReviewState['status'] }) {
  const tone = isApproved(status) ? 'good' : status === 'rejected' ? 'error' : status === 'pending' ? 'neutral' : 'warn';
  return <Badge tone={tone}>{reviewLabels[status]}</Badge>;
}

export function ReviewWarnings({ issues }: { issues: Issue[] }) {
  if (!issues.length) return null;
  const groups = new Map<string, Issue[]>();
  for (const issue of issues) {
    const reference = issue.evidence_refs[0];
    const label = [
      typeof reference?.page === 'number' ? 'Стр. ' + reference.page : 'Документ',
      typeof reference?.table === 'string' ? reference.table : '',
    ].filter(Boolean).join(' · ');
    groups.set(label, [...(groups.get(label) ?? []), issue]);
  }
  const severity = { info: 'Сведения', warning: 'Требует научной проверки', error: 'Ошибка анализа' };
  return <div className="review-warnings">
    <h3>Научные замечания</h3>
    {[...groups].map(([label, group]) => <details key={label}>
      <summary>{label} · {group.length}</summary>
      <ul className="upload-issues">{group.map((issue) => <li key={issue.issue_id}>
        <p><strong>{severity[issue.severity]}</strong> · {issue.reason}</p>
        {checkValues(issue).map(([field, value]) => <p className="muted" key={field}>{field} · {value}</p>)}
        {issue.evidence_refs.map((reference, index) => {
          const sources: unknown[] = Array.isArray(reference.sources) ? reference.sources : [reference];
          const details: unknown[] = Array.isArray(reference.details) ? reference.details : [];
          return <div key={index}>
            {sources.map((source, i) => {
              const entry = record(source);
              return typeof entry?.fragment === 'string' && <p className="muted" key={i}>{entry.fragment}</p>;
            })}
            {details.length > 0 && <details><summary>Исходные ячейки · {details.length}</summary>
              <ul>{details.map((item, i) => {
                const detail = record(item);
                const headings = Array.isArray(detail?.headers)
                  ? detail.headers.map((header: unknown) => record(header)?.text).filter(Boolean).join(' / ') : '';
                const raw = record(detail?.value);
                const coordinates = Array.isArray(raw?.bbox) ? raw.bbox.map((n: number) => n.toFixed(1)).join(', ') : '';
                return <li key={i}>{[detail?.code, headings, raw?.text, coordinates, detail?.source_block_id]
                  .filter((value): value is string => typeof value === 'string' && value.length > 0).join(' · ')}</li>;
              })}</ul>
            </details>}
          </div>;
        })}
      </li>)}</ul>
    </details>)}
  </div>;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' ? value as Record<string, unknown> : undefined;
}

function History({ events }: { events: ReviewEvent[] }) {
  if (!events.length) return <p className="muted">Решений пока нет.</p>;
  return <ol className="review-history">{events.map(({ decision, previous_decision: previous, original_result: original, issues, ocr_pages: ocr }) => (
    <li key={decision.review_id}>
      <details>
        <summary>
          <strong>{reviewLabels[decision.status]}</strong> · ревизия {decision.revision} ·{' '}
          <time dateTime={decision.reviewed_at}>{date.format(new Date(decision.reviewed_at))} UTC</time>
        </summary>
        <InfoList items={[
          ['Предыдущее решение', previous ? `${reviewLabels[previous.status]} · ревизия ${previous.revision}` : 'Ожидает проверки'],
          ['Метка оператора', decision.reviewer_label ?? '—'], ['Основание', decision.reason ?? '—'],
          ['Задание анализа', decision.source_job_id], ['SHA-256 PDF', decision.pdf_sha256],
          ['SHA-256 исходной записи', decision.source_result_hash],
          ['Исходное значение', `${metricTitle(original)} · ${formatValue(original.raw, true)}`],
          ['Нормализованное значение', formatValue(original.normalized)],
          ['Источник', sourceLabel(original.source)],
          ['Сверка', decision.evidence_mode === 'visual' ? 'Визуальная' : decision.evidence_mode === 'text' ? 'По тексту PDF' : '—'],
          ...Object.entries(original.context).map(([key, value]): [string, string] => [contextLabel(key), contextText(value)]),
        ]} />
        {original.source.fragment && <p className="muted">{original.source.fragment}</p>}
        {decision.reviewed_result && <ReviewChanges original={original} changed={decision.reviewed_result} />}
        {decision.normalization_evidence && <p>Основание нормализации · {decision.normalization_evidence}</p>}
        {ocr.length > 0 && <p className="ocr-caution">Требуется OCR · {ocr.join(', ')}</p>}
        {decision.warnings_acknowledged && <p>Оператор учёл научные замечания.</p>}
        <ReviewWarnings issues={issues} />
      </details>
    </li>
  ))}</ol>;
}

export function UploadReview({ api, upload, token, initial, onRefresh }: {
  api: UploadClient; upload: StoredUpload; token: string; initial: ReviewDetail; onRefresh: () => void;
}) {
  const id = upload.upload_id;
  const resultId = initial.result_id;
  const [detail, setDetail] = useState(initial);
  const [requestedPage, setRequestedPage] = useState<number>();
  const [renderedPage, setRenderedPage] = useState<number>();
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [mode, setMode] = useState<EvidenceMode>('text');
  const [checked, setChecked] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<DataError>();
  const controller = useRef<AbortController>(null);
  useEffect(() => () => controller.current?.abort(), []);

  const loadHistory = useCallback((signal: AbortSignal) => api.reviewHistory(id, resultId, token, signal), [api, id, resultId, token]);
  const history = useData(loadHistory);
  const loadPdf = useCallback(async (signal: AbortSignal) => {
    const blob = await api.pdf(id, token, signal);
    if (!globalThis.crypto?.subtle) return blob;
    const hash = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
    const digest = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('');
    if (digest !== upload.sha256) throw new DataError('SHA-256 открытого PDF не совпадает с исходным.');
    return blob;
  }, [api, id, token, upload.sha256]);

  const original = detail.original_result;
  const correction = detail.decision?.reviewed_result;
  const proposal = detail.status === 'correction_proposed' ? correction : null;
  const currentPage = requestedPage ?? original?.source.page;
  const active = detail.processing_status === 'queued' || detail.processing_status === 'processing';
  const blocked = active || detail.pdf_status !== 'valid' || error?.status === 409;
  const viewed = (page: number) => renderedPage === page && (!detail.ocr_pages.includes(page) || mode === 'visual');
  const approvalReady = checked && (!detail.issues.length || acknowledged) && (mode !== 'visual' || Boolean(reason.trim()));

  function openPage(page: number) {
    if (page !== currentPage) setRenderedPage(undefined);
    setRequestedPage(page);
  }

  async function save(action: ReviewAction, changes?: Correction, explanation = reason) {
    if (!detail.basis || blocked || controller.current) return;
    const request = new AbortController();
    controller.current = request;
    setSaving(true);
    setError(undefined);
    const body: ReviewRequest = {
      basis: detail.basis, expected_revision: detail.revision, action,
      reviewer_label: reviewer.trim() || null, reason: explanation.trim() || null,
    };
    if (changes) body.correction = changes;
    if (action === 'approve' || action === 'approve_correction') {
      body.evidence_mode = mode;
      body.warnings_acknowledged = acknowledged;
    }
    try {
      const saved = await api.saveReview(id, resultId, token, body, request.signal);
      if (request.signal.aborted) return;
      setDetail(saved);
      setEditing(false);
      setChecked(false);
      setAcknowledged(false);
      setReason('');
      history.retry();
    } catch (cause) {
      if (!request.signal.aborted) setError(cause instanceof DataError ? cause : new DataError(errorMessage(cause)));
    } finally {
      controller.current = null;
      if (!request.signal.aborted) setSaving(false);
    }
  }

  return (
    <div className="split detail-layout">
      {original && currentPage && detail.pdf_status === 'valid' ? (
        <PdfPane key={currentPage} domain={upload.domain} documentId={original.document_id} initialPage={currentPage}
          label={upload.original_filename} loadPdf={loadPdf} onPageRendered={setRenderedPage}
          highlightsResult={currentPage === original.source.page} />
      ) : <section className="surface inspector">
        <h2>Оригинал PDF</h2>
        <p>{detail.pdf_status !== 'valid' ? 'Оригинал отсутствует или его SHA-256 изменился.' : 'В текущем анализе запись недоступна.'}</p>
        <Link href={uploadAnalysisUrl(id)}>К результатам загрузки</Link>
      </section>}
      <div className="stack">
        {original && <>
          <ResultEvidence result={original}>
            {currentPage !== original.source.page && <button className="button" type="button"
              onClick={() => openPage(original.source.page)}>К исходной странице</button>}
            <Link href={uploadAnalysisUrl(id)}>К результатам загрузки</Link>
          </ResultEvidence>
          <section className="surface">
            <header className="section-bar"><h2>Контекст записи</h2></header>
            <InfoList items={[
              ['Происхождение', original.source.origin === 'primary' ? 'Первичный результат'
                : original.source.origin === 'cited' ? ['Цитируемый результат', original.source.cited_publication_id].filter(Boolean).join(' · ')
                  : 'Требует уточнения'],
              ...Object.entries(original.context).map(([key, value]): [string, string] => [contextLabel(key), contextText(value)]),
            ]} />
          </section>
        </>}
        <section className="surface" aria-label="Ручная проверка">
          <header className="section-bar"><h2>Ручная проверка</h2><ReviewBadge status={detail.status} /></header>
          <div className="upload-content review-content">
            <p className="muted" aria-live="polite">Ревизия {detail.revision}</p>
            {detail.status === 'stale' && <p>Основание предыдущей проверки изменилось. Решение доступно в истории.</p>}
            {active && <p>Идёт анализ. Обновите данные после завершения.</p>}
            {detail.applicable && detail.decision?.reason && <p>{detail.decision.reason}</p>}
            {original && correction && detail.applicable && <>
              <ReviewChanges original={original} changed={correction} />
              {detail.decision?.normalization_evidence && <p>Основание нормализации · {detail.decision.normalization_evidence}</p>}
              {correction.source.page !== original.source.page && <button className="button" type="button"
                onClick={() => openPage(correction.source.page)}>Открыть страницу исправления · {correction.source.page}</button>}
            </>}
            {detail.ocr_pages.length > 0 && <p className="ocr-caution">Страницы, требующие OCR · {detail.ocr_pages.join(', ')}</p>}
            <ReviewWarnings issues={detail.issues} />
            {error && <p role="alert">{error.message}</p>}
            {blocked && <button className="button" type="button" onClick={onRefresh}>Обновить данные</button>}
            {detail.basis && original && !blocked && <div className="review-form">
              <label>Метка оператора (необязательно)<input value={reviewer} maxLength={160} disabled={saving}
                onChange={(event) => setReviewer(event.target.value)} /></label>
              <p className="muted">Приватная проверка. Метку вводит оператор. Общий токен даёт доступ к хранилищу.</p>
              {editing ? <CorrectionForm original={original} initial={(detail.applicable ? correction : null) ?? original}
                normalizationEvidence={detail.applicable ? detail.decision?.normalization_evidence : null}
                pageCount={upload.page_count} saving={saving} onCancel={() => { setEditing(false); setError(undefined); }}
                onPropose={(changes, note) => void save('propose_correction', changes, note)} /> : <>
                <fieldset disabled={saving}>
                  <legend>Сверка с оригиналом</legend>
                  <label>Способ сверки<select value={mode} onChange={(event) => setMode(event.target.value as EvidenceMode)}>
                    <option value="text">По тексту PDF</option><option value="visual">Визуально по PDF</option>
                  </select></label>
                  <label className="review-check"><input type="checkbox" checked={checked}
                    onChange={(event) => setChecked(event.target.checked)} />Я сверил значение, единицу и контекст на странице PDF</label>
                  {detail.issues.length > 0 && <label className="review-check"><input type="checkbox" checked={acknowledged}
                    onChange={(event) => setAcknowledged(event.target.checked)} />Я учёл научные замечания</label>}
                  <label>Примечание или причина<textarea value={reason} rows={3} maxLength={4000}
                    onChange={(event) => setReason(event.target.value)} /></label>
                  <p className="muted">Для отклонения и визуальной сверки укажите причину.</p>
                  <div className="upload-actions">
                    <button className="button primary" type="button" disabled={!approvalReady || !viewed(original.source.page)}
                      onClick={() => void save('approve')}>Подтвердить исходное</button>
                    {proposal && <button className="button primary" type="button" disabled={!approvalReady || !viewed(proposal.source.page)}
                      onClick={() => void save('approve_correction')}>Подтвердить исправление</button>}
                    <button className="button" type="button" onClick={() => { setEditing(true); setError(undefined); }}>Предложить исправление</button>
                    <button className="button quiet" type="button" disabled={!reason.trim()} onClick={() => void save('reject')}>Отклонить</button>
                  </div>
                </fieldset>
              </>}
            </div>}
          </div>
        </section>
        <section className="surface" aria-label="История решений">
          <header className="section-bar"><h2>История решений</h2></header>
          <div className="upload-content"><LoadBoundary {...history}>{(events) => <History events={events} />}</LoadBoundary></div>
        </section>
      </div>
    </div>
  );
}
