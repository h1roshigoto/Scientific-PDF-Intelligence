import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../data';
import type { StoredUpload, UploadClient, UploadExportFormat, UploadExportPreview } from '../data';
import { saveFile } from '../download';
import { useData } from '../hooks/useData';
import { LoadBoundary } from './Feedback';

const formats: UploadExportFormat[] = ['CSV', 'JSON', 'PDF', 'ZIP'];
const warnings: Record<UploadExportPreview['validation']['warnings'][number]['code'], string> = {
  scientific_completeness_unverified: 'Научная полнота не подтверждена. Ручная проверка относится к отдельным записям.',
  private_not_official_submission: 'Приватный пакет для работы с загруженным PDF.',
  zero_approved_results: 'Пакет доступен для аудита. Подтверждённых записей пока нет.',
  incomplete_ocr: 'Часть страниц ещё требует распознавания текста.',
  local_only: 'Выполнен локальный анализ без извлечения с помощью языковой модели.',
  partial_analysis: 'Охват анализа неполон. Сведения о страницах и замечания включены в архив.',
  unresolved_issues: 'Научные замечания остаются открытыми после подтверждения записи.',
};

export function UploadExport({ api, upload, token }: { api: UploadClient; upload: StoredUpload; token: string }) {
  const load = useCallback((signal: AbortSignal) => api.exportPreview(upload, token, signal), [api, upload, token]);
  const preview = useData(load);
  const active = useRef<AbortController | undefined>(undefined);
  const [pending, setPending] = useState<UploadExportFormat>();
  const [error, setError] = useState<string>();

  useEffect(() => () => active.current?.abort(), []);

  async function download(data: UploadExportPreview, format: UploadExportFormat) {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setPending(format);
    setError(undefined);
    try {
      const { blob, filename } = await api.downloadExport(data, format, token, controller.signal);
      if (!controller.signal.aborted) saveFile(blob, filename);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally {
      active.current = undefined;
      if (!controller.signal.aborted) setPending(undefined);
    }
  }

  return (
    <section className="surface" aria-label="Экспорт научного проекта">
      <header className="section-bar"><h2>Экспорт научного проекта</h2></header>
      <div className="upload-content">
        <LoadBoundary {...preview}>
          {(data) => {
            const { review_counts: counts, issue_counts: issues, ocr_pending_pages: pages } = data.validation;
            return <>
              <p className="num">Автоматически извлечено — {counts.automatic}{' · '}Подтверждено исходных — {counts.approved} · Подтверждено исправленных — {counts.approved_corrected}
                {' · '}Ожидают решения — {counts.pending} · Предложено исправлений — {counts.correction_proposed}
                {' · '}Отклонено — {counts.rejected} · Устарело — {counts.stale} · Замечаний — {issues.total}</p>
              <p>Страниц в ожидании OCR — {pages.length}</p>
              {data.metadata.processing_status === 'failed' && <p>Анализ завершился с ошибкой. Доступен сохранённый частичный снимок.</p>}
              {counts.approved + counts.approved_corrected === 0 && <p>{counts.automatic > 0
                ? 'CSV и JSON заполняются после подтверждения кандидатов. Исходные кандидаты есть в журнале проверок внутри ZIP.'
                : 'В CSV и JSON пока нет научных записей. В ZIP доступны сведения об обработанных страницах и замечаниях.'}</p>}
              <ul className="muted">
                {data.validation.warnings.map(({ code }) => <li key={code}>{warnings[code]}</li>)}
              </ul>
              <div className="upload-actions">
                {formats.map((format) => <button key={format} type="button" disabled={Boolean(pending)}
                  className={format === 'ZIP' ? 'button primary' : 'button'} onClick={() => void download(data, format)}>
                  Скачать {format}
                </button>)}
              </div>
              {pending && <p role="status">Подготавливается {pending}…</p>}
              {error && <p role="alert">{error}</p>}
              <div className="upload-actions">
                <button className="button" type="button" disabled={Boolean(pending)} onClick={() => {
                  setError(undefined);
                  preview.retry();
                }}>Обновить данные экспорта</button>
              </div>
            </>;
          }}
        </LoadBoundary>
      </div>
    </section>
  );
}
