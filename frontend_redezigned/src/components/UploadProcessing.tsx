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
  page_analysis_failed: 'Некоторые страницы не удалось обработать.',
  worker_recovered: 'Worker продолжил прерванную обработку.',
};
const active = (status?: UploadProcessing['status']) => status === 'queued' || status === 'processing';

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

export function UploadProcessingControls({ api, id, token, listRevision }: {
  api: UploadClient; id: string; token: string; listRevision: number;
}) {
  const [state, setState] = useState<UploadProcessing>();
  const [error, setError] = useState('');
  const [request, setRequest] = useState(0);
  const [accepted, setAccepted] = useState<UploadProcessing['status']>();
  const [pending, setPending] = useState(false);
  const submit = useRef<AbortController>(null);

  useEffect(() => () => submit.current?.abort(), []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let waiting = active(accepted);

    async function poll() {
      try {
        const next = await api.processing(id, token, controller.signal);
        if (controller.signal.aborted) return;
        setState(next);
        setError('');
        waiting = active(next.status);
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(errorMessage(cause));
        if (cause instanceof DataError && (cause.status === 401 || cause.status === 503)) waiting = false;
      }
      if (waiting) timer = window.setTimeout(() => void poll(), 1500);
    }

    // Leave time for an accepted queue status to render before the next read.
    timer = window.setTimeout(() => void poll(), accepted ? 600 : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [api, id, token, request, accepted, listRevision]);

  async function enqueue() {
    if (submit.current) return;
    const controller = new AbortController();
    submit.current = controller;
    setPending(true);
    setError('');
    try {
      const job = await api.process(id, token, controller.signal);
      if (controller.signal.aborted) return;
      setAccepted(job.status);
      setState(undefined);
      setRequest((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      submit.current = null;
      if (!controller.signal.aborted) setPending(false);
    }
  }

  const start = state?.status === 'not_started';
  const retry = state?.status === 'failed' && state.retry_allowed;
  return (
    <div className="upload-analysis-controls">
      {state ? <UploadStatus state={state} /> : <p className="muted" aria-live="polite">
        {accepted ? labels[accepted] : 'Проверяем состояние анализа…'}
      </p>}
      {error && <p role="alert">{error}</p>}
      <div className="upload-actions">
        {(start || retry) && <button className="button" type="button" disabled={pending} onClick={() => void enqueue()}>
          {retry ? 'Повторить' : 'Начать анализ'}
        </button>}
        {state?.counts && <Link className="button" href={uploadAnalysisUrl(id)}>Посмотреть результаты</Link>}
        {!state && error && <button className="button quiet" type="button" onClick={() => setRequest((value) => value + 1)}>
          Обновить состояние
        </button>}
      </div>
    </div>
  );
}
