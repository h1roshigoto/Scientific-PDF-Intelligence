import { useEffect, useRef, useState } from 'react';
import { DataError, errorMessage } from '../data';
import type { UploadClient, UploadProcessing } from '../data';
import { Link, uploadAnalysisUrl } from '../navigation';

const date = new Intl.DateTimeFormat('ru', { dateStyle: 'medium', timeStyle: 'medium' });
const labels: Record<UploadProcessing['status'], string> = {
  not_started: 'Анализ не запущен',
  queued: 'В очереди',
  processing: 'В обработке',
  needs_review: 'Нужна проверка',
  completed: 'Анализ завершён',
  failed: 'Ошибка анализа',
};
const steps: Record<Exclude<UploadProcessing['step'], null | 'finished'>, string> = {
  queued: 'Ожидание worker',
  validating_input: 'Проверка оригинала и SHA-256',
  extracting: 'Чтение PDF и извлечение данных',
  checking_results: 'Проверка результатов',
};
const diagnostics: Record<string, string> = {
  original_unavailable: 'Оригинал PDF недоступен.',
  original_sha256_mismatch: 'Контрольная сумма оригинала PDF не совпадает.',
  upload_identity_mismatch: 'Данные задания не совпадают с каталогом загрузок.',
  model_credentials_missing_local: 'GigaChat не настроен. Использован локальный анализ.',
  model_unavailable_for_recovery: 'GigaChat недоступен для продолжения прерванного анализа.',
  request_budget_exhausted: 'Исчерпан лимит запросов к GigaChat. Повтор продолжит анализ с сохранённого этапа.',
  'completion:model_request_quota_full': 'Исчерпан суточный лимит запросов к модели. Лимит обновится в полночь UTC.',
  page_analysis_failed: 'Некоторые страницы не удалось обработать.',
  worker_recovered: 'Worker продолжил прерванную обработку.',
  processing_error: 'Worker не смог обработать PDF. Повторите анализ.',
  processing_timeout: 'Истекло время обработки PDF. Можно повторить анализ.',
  pdf_parser_unavailable: 'Служба чтения PDF недоступна. Повторите анализ после её запуска.',
  storage_full: 'Не хватает места для сохранения анализа.',
  batch_failed: 'Не удалось сохранить результаты анализа PDF.',
  upload_page_count_mismatch: 'Число прочитанных страниц не совпало с данными загрузки.',
};
export const processingActive = (status?: UploadProcessing['status']) => status === 'queued' || status === 'processing';
const pollInterval = 1500;

export function UploadStatus({ state }: { state: UploadProcessing }) {
  const { status, step, updated_at: updatedAt, diagnostic, counts, pages_needing_ocr: ocr } = state;
  return (
    <div aria-live="polite" className="upload-status">
      <p><strong>{labels[status]}</strong>{step && step !== 'finished' && ` · ${steps[step]}`}</p>
      {updatedAt && <p className="muted">Обновлено <time dateTime={updatedAt}>{date.format(new Date(updatedAt))}</time></p>}
      {diagnostic && <p>{Object.hasOwn(diagnostics, diagnostic) ? diagnostics[diagnostic] : `Диагностика — ${diagnostic}`}</p>}
      {counts && <p className="muted">Обработано страниц — {counts.pages_analyzed} из {counts.pages}
        {' · '}Результатов — {counts.results}{' · '}Замечаний — {counts.issues}</p>}
      {counts && counts.unresolved > 0 && <p>Нерешённых фрагментов или страниц — {counts.unresolved}</p>}
      {counts && counts.pending_fragments > 0 && <p>Ожидают обработки фрагменты — {counts.pending_fragments}</p>}
      {ocr.length > 0 && <p>Ожидают OCR страницы — {ocr.join(', ')}</p>}
    </div>
  );
}

export function useUploadProcessing(api: UploadClient, id: string, token: string, listRevision = 0) {
  const [state, setState] = useState<UploadProcessing>();
  const [pollError, setPollError] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [request, setRequest] = useState(0);
  const [accepted, setAccepted] = useState<UploadProcessing['status']>();
  const [pending, setPending] = useState(false);
  const watching = useRef(false);
  const pollRequest = useRef<AbortController>(null);
  const submit = useRef<AbortController>(null);

  useEffect(() => () => submit.current?.abort(), []);

  useEffect(() => {
    if (pending) return;
    const controller = new AbortController();
    pollRequest.current = controller;
    let timer: number | undefined;
    let failures = 0;

    async function poll() {
      if (controller.signal.aborted) return;
      let delay = pollInterval;
      try {
        const next = await api.processing(id, token, controller.signal);
        if (controller.signal.aborted) return;
        setState(next);
        setPollError('');
        failures = 0;
        watching.current = processingActive(next.status);
      } catch (cause) {
        if (controller.signal.aborted) return;
        setPollError(errorMessage(cause));
        if (cause instanceof DataError && (cause.status === 401 || cause.status === 404)) return;
        failures += 1;
        const backoff = pollInterval * 2 ** Math.min(failures, 4);
        delay = Math.min(60_000, Math.max(backoff, (cause instanceof DataError ? cause.retryAfter ?? 0 : 0) * 1000));
      }
      if (watching.current) timer = window.setTimeout(() => void poll(), delay);
    }

    // Leave time for an accepted queue status to render before the next read.
    timer = window.setTimeout(() => void poll(), accepted ? 600 : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [api, id, token, request, accepted, listRevision, pending]);

  function refresh() {
    pollRequest.current?.abort();
    setPollError('');
    setRequest((value) => value + 1);
  }

  async function enqueue() {
    if (submit.current) return;
    pollRequest.current?.abort();
    const controller = new AbortController();
    submit.current = controller;
    setPending(true);
    setSubmitError('');
    setPollError('');
    try {
      const job = await api.process(id, token, controller.signal);
      if (controller.signal.aborted) return;
      watching.current = true;
      setAccepted(job.status);
      setState(undefined);
    } catch (cause) {
      // A lost response can follow a successful enqueue. Check the queue before enabling Start again.
      if (!controller.signal.aborted) {
        setSubmitError(errorMessage(cause));
        setState(undefined);
        setAccepted(undefined);
        watching.current = true;
      }
    } finally {
      submit.current = null;
      if (!controller.signal.aborted) {
        setPending(false);
        setRequest((value) => value + 1);
      }
    }
  }

  return { state, accepted, pending, pollError, submitError, refresh, enqueue };
}

export function UploadProcessingControls({ api, id, token, listRevision }: {
  api: UploadClient; id: string; token: string; listRevision: number;
}) {
  const { state, accepted, pending, pollError, submitError, refresh, enqueue } = useUploadProcessing(api, id, token, listRevision);
  const start = state?.status === 'not_started';
  const retry = state?.status === 'failed' && state.retry_allowed;
  const canOpen = state?.counts && !processingActive(state.status);
  return (
    <div className="upload-analysis-controls">
      {state ? <UploadStatus state={state} /> : (!pollError || accepted) && <p className="muted" aria-live="polite">
        {accepted ? labels[accepted] : 'Проверяем состояние анализа…'}
      </p>}
      {pollError && <p role="alert">Не удалось обновить состояние. {pollError}</p>}
      {(start || retry || !state) && submitError && <p role="alert">Не удалось подтвердить запуск. {submitError}</p>}
      <div className="upload-actions">
        {(pending || start || retry) && <button className="button primary" type="button" disabled={pending || Boolean(pollError)}
          onClick={() => void enqueue()}>{pending ? 'Запускаем анализ…' : retry ? 'Повторить' : 'Начать анализ'}</button>}
        {canOpen && <Link className="button primary" href={uploadAnalysisUrl(id)}>Посмотреть результаты</Link>}
        {pollError && <button className="button quiet" type="button" onClick={refresh}>Обновить состояние</button>}
      </div>
    </div>
  );
}
