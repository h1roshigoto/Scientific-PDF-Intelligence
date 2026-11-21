import { z } from 'zod';
import { belongsTo, csvFiles, documentSchema, domainSchema, issueSchema, resultSchema } from './domain';
import type { Domain, Issue, Result, ScientificDocument } from './domain';
import { passportFilename, passportQuery } from './project-passport';
import type { PassportExport } from './project-passport';
import { pdfStatus, reviewEventSchema, reviewStateSchema } from './upload-review';
import type { ReviewRequest } from './upload-review';

export type InternalArtifact = 'results.json' | 'issues.json';
export interface Provider {
  readonly mode: 'mock' | 'api';
  readonly csvAvailable: boolean;
  readonly projectPassport?: PassportExport;
  documents(domain: Domain | undefined, signal: AbortSignal): Promise<ScientificDocument[]>;
  results(domain: Domain | undefined, signal: AbortSignal): Promise<Result[]>;
  result(domain: Domain | undefined, id: string, signal: AbortSignal): Promise<Result>;
  issues(domain: Domain | undefined, signal: AbortSignal): Promise<Issue[]>;
  pdf(domain: Domain, documentId: string, signal: AbortSignal): Promise<Blob>;
  csv(domain: Domain, filename: string, signal: AbortSignal): Promise<Blob>;
  exportJson(artifact: InternalArtifact, signal: AbortSignal): Promise<Blob>;
}

export class DataError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = 'DataError';
  }
}

export const errorMessage = (error: unknown) => error instanceof DataError ? error.message : 'Не удалось выполнить запрос. Повторите попытку.';

function joinUrl(base: string, path: string) {
  return `${base.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
}

async function readPdf(response: Response) {
  const blob = await response.blob();
  const prefix = await blob.slice(0, 1024).text();
  if (!prefix.includes('%PDF-')) throw new DataError('Источник вернул файл, который не удалось распознать как PDF.');
  return blob.slice(0, blob.size, 'application/pdf');
}

function client(fetcher: typeof fetch, timeoutMs: number, statusMessages: Readonly<Record<number, string>> = {},
  detailMessages?: Readonly<Record<string, string>>) {
  async function request<T>(url: string, signal: AbortSignal, read: (response: Response) => Promise<T>, init?: RequestInit) {
    const timeout = AbortSignal.timeout(timeoutMs);
    try {
      const response = await fetcher(url, { ...init, signal: AbortSignal.any([signal, timeout]) });
      if (!response.ok) {
        const body: unknown = detailMessages && (await response.json().catch(() => null));
        const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null;
        const message = (typeof detail === 'string' && detailMessages && Object.hasOwn(detailMessages, detail)
          ? detailMessages[detail] : undefined) ?? statusMessages[response.status] ?? (response.status === 404
          ? 'Запрошенный файл или запись недоступны.'
          : `Сервер вернул ошибку HTTP ${response.status}.`);
        throw new DataError(message, response.status);
      }
      return await read(response);
    } catch (error) {
      if (signal.aborted) throw signal.reason;
      if (timeout.aborted) throw new DataError('Истекло время ожидания ответа сервера.');
      if (error instanceof DataError) throw error;
      throw new DataError('Не удалось связаться с сервером. Проверьте подключение.');
    }
  }

  function json<T>(url: string, schema: z.ZodType<T>, signal: AbortSignal, init?: RequestInit) {
    return request(url, signal, (response) => readJson(response, schema), init);
  }

  function pdf(url: string, signal: AbortSignal) {
    return request(url, signal, readPdf);
  }

  function csv(url: string, signal: AbortSignal) {
    return request(url, signal, async (response) => {
      const bytes = await response.arrayBuffer();
      try {
        const decoded = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
        if (!decoded.trim() || decoded.trimStart().startsWith('<') || !decoded.split('\n', 1)[0].includes(',')) throw new Error();
      } catch {
        throw new DataError('Выгрузка не содержит корректный CSV в UTF-8.');
      }
      return new Blob([bytes], { type: 'text/csv;charset=utf-8' });
    });
  }
  return { request, json, pdf, csv };
}

async function readJson<T>(response: Response, schema: z.ZodType<T>) {
  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new DataError('Ответ сервера содержит некорректный JSON.');
  }
  const data = schema.safeParse(body);
  if (!data.success) throw new DataError('Ответ сервера не соответствует контракту данных.');
  return data.data;
}

const uploadId = z.string().regex(/^[a-f0-9]{32}$/);
const uploadSchema = z.object({
  upload_id: uploadId,
  domain: domainSchema,
  original_filename: z.string().min(1).max(255),
  size_bytes: z.number().int().positive(),
  sha256: z.string().regex(/^[a-f0-9]{64}$/),
  page_count: z.number().int().positive(),
  status: z.literal('stored'),
  created_at: z.iso.datetime({ offset: true }),
});
export type StoredUpload = z.infer<typeof uploadSchema>;

const processingStatus = z.enum(['not_started', 'queued', 'processing', 'needs_review', 'completed', 'failed']);
const processingStep = z.enum(['queued', 'validating_input', 'extracting', 'checking_results', 'finished']);
const count = z.number().int().nonnegative();
const processingSchema = z.object({
  upload_id: uploadId,
  job_id: uploadId.nullable(),
  status: processingStatus,
  step: processingStep.nullable(),
  updated_at: z.iso.datetime({ offset: true }).nullable(),
  diagnostic: z.string().nullable(),
  retry_allowed: z.boolean(),
  mode: z.enum(['local', 'gigachat']).nullable(),
  coverage: z.string().nullable(),
  counts: z.object({
    pages: count, pages_analyzed: count, results: count, needs_review: count,
    issues: count, unresolved: count, pending_fragments: count,
  }).nullable(),
  pages_needing_ocr: z.array(z.number().int().positive()),
  human_review_required: z.boolean(),
});
export type UploadProcessing = z.infer<typeof processingSchema>;

const reviewDetailSchema = reviewStateSchema.extend({
  processing_status: processingStatus, pdf_status: pdfStatus, issues: z.array(issueSchema),
  ocr_pages: z.array(z.number().int().positive()),
});
const reviewOverviewSchema = z.object({
  processing: processingSchema, pdf_status: pdfStatus, issues: z.array(issueSchema), reviews: z.array(reviewStateSchema),
});
export type ReviewDetail = z.infer<typeof reviewDetailSchema>;
export type ReviewEvent = z.infer<typeof reviewEventSchema>;

const sha256 = z.string().regex(/^[a-f0-9]{64}$/);
const exportWarning = z.enum([
  'zero_approved_results', 'scientific_completeness_unverified', 'incomplete_ocr',
  'local_only', 'partial_analysis', 'unresolved_issues', 'private_not_official_submission',
]);
const exportPreviewSchema = z.object({
  metadata: z.object({
    format: z.literal('spi-private-project-v1'), upload_id: uploadId, domain: domainSchema,
    pdf_sha256: sha256, processing_job_id: uploadId,
    processing_status: z.enum(['needs_review', 'completed', 'failed']),
    snapshot_sha256: sha256, official_submission: z.literal(false),
  }),
  validation: z.object({
    review_counts: z.object({
      automatic: count, approved: count, approved_corrected: count, pending: count, correction_proposed: count,
      rejected: count, stale: count, unconfirmed: count,
    }),
    issue_counts: z.object({ total: count }),
    ocr_pending_pages: z.array(z.number().int().positive()),
    scientific_completeness: z.literal('unverified'),
    warnings: z.array(z.object({ code: exportWarning })),
  }),
});
export type UploadExportPreview = z.infer<typeof exportPreviewSchema>;

const exportFiles = {
  CSV: { route: 'reviewed-results.csv', filename: 'reviewed_results.csv', mime: 'text/csv' },
  JSON: { route: 'reviewed-results.json', filename: 'reviewed_results.json', mime: 'application/json' },
  PDF: { route: 'scientific-passport.pdf', filename: 'scientific-passport.pdf', mime: 'application/pdf' },
  ZIP: { route: 'project.zip', filename: 'project.zip', mime: 'application/zip' },
};
export type UploadExportFormat = keyof typeof exportFiles;

const uploadDetails = {
  uploads_disabled: 'Загрузка PDF выключена на сервере.',
  uploads_misconfigured: 'Приватная загрузка недоступна. Проверьте настройки сервера.',
  upload_too_large: 'Размер PDF превышает установленный на сервере предел.',
  upload_quota_full: 'Хранилище заполнено. Достигнут предел количества загрузок.',
  empty_pdf: 'Выбранный PDF пуст.',
  missing_pdf_signature: 'Файл не содержит сигнатуру PDF.',
  encrypted_pdf: 'Зашифрованные PDF не поддерживаются.',
  invalid_pdf: 'PDF повреждён или не содержит страниц.',
  upload_storage_error: 'Сервер не смог выполнить операцию с хранилищем. Повторите попытку.',
  upload_processing_unconfigured: 'Обработка PDF ещё не настроена на сервере.',
  upload_processing_storage_error: 'Сервер не смог прочитать или сохранить состояние анализа.',
  upload_review_storage_error: 'Сервер не смог прочитать или сохранить рецензию. Повторите попытку.',
  review_revision_conflict: 'Рецензию уже изменили. Обновите данные и повторите решение.',
  review_source_changed: 'Версия научной записи изменилась. Обновите данные и сверьте источник заново.',
  review_pdf_changed: 'Оригинал PDF отсутствует или его SHA-256 изменился.',
  review_processing_active: 'Идёт повторный анализ. После его завершения обновите данные и сверьте источник заново.',
  review_proposal_required: 'Текущее предложение недоступно. Обновите данные и проверьте историю.',
  review_warnings_unacknowledged: 'Подтвердите, что учли научные замечания.',
  review_visual_evidence_required: 'Для этой страницы нужна визуальная сверка с обоснованием.',
  review_page_outside_pdf: 'Укажите существующую страницу PDF.',
  review_origin_required: 'Для RAG укажите, первичный это результат или цитируемый.',
  invalid_review_origin: 'Укажите ID публикации для цитируемого результата. Для первичного результата оставьте это поле пустым.',
  normalization_evidence_required: 'Укажите единицы или протокол и обоснуйте нормализацию по PDF. Если их нельзя установить, оставьте нормализованное значение пустым.',
  inconsistent_normalization: 'Нормализованное значение не соответствует исходному значению и указанной единице.',
  invalid_normalized_value: 'Укажите числовое нормализованное значение. При неизвестном значении оставьте оба поля пустыми.',
  invalid_review_context: 'Проверьте научный контекст. Укажите top_k как целое неотрицательное число, а metric_k в поле метрики.',
  empty_review_correction: 'Измените хотя бы одно научное поле.',
  invalid_review_correction: 'Проверьте исходное значение, единицы и метрику.',
  invalid_review_request: 'Проверьте значения полей, обоснование и указатель на источник.',
  upload_export_processing_active: 'Идёт анализ PDF. Экспорт будет доступен после его завершения.',
  upload_export_analysis_unavailable: 'Для экспорта нужен завершённый запуск с сохранёнными результатами анализа.',
  upload_export_pdf_changed: 'Экспорт недоступен. Оригинал PDF повреждён, отсутствует или его SHA-256 изменился.',
  upload_export_source_changed: 'Версия анализа или рецензии изменилась. Обновите данные экспорта.',
  upload_export_invalid_review: 'Экспорт остановлен. Подтверждённая запись не прошла проверку источника.',
  upload_export_too_large: 'Размер проекта превышает установленный на сервере предел экспорта.',
  upload_export_storage_error: 'Сервер не смог подготовить экспорт. Повторите попытку.',
};

export function createUploadClient(fetcher = fetch) {
  const endpoint = '/api/uploads';
  const http = client(fetcher, 120_000, {
    401: 'Неверный токен доступа.',
    503: 'Приватная загрузка сейчас недоступна.',
  }, uploadDetails);
  const privateRequest = (token: string): RequestInit => ({
    cache: 'no-store', credentials: 'omit', redirect: 'error',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const uploadEndpoint = (id: string, action: string) => `${endpoint}/${encodeURIComponent(id)}/${action}`;
  const reviewEndpoint = (id: string, result: string) => uploadEndpoint(id, `results/${encodeURIComponent(result)}/review`);

  return {
    async list(token: string, signal: AbortSignal) {
      try {
        return await http.request(endpoint, signal, async (response) => {
          const maxBytes = Number(response.headers.get('X-SPI-Upload-Max-Bytes'));
          if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) {
            throw new DataError('Сервер не сообщил допустимый размер PDF.');
          }
          const records = await readJson(response, z.array(uploadSchema));
          return { kind: 'authorized' as const, records, maxBytes };
        }, privateRequest(token));
      } catch (error) {
        if (error instanceof DataError) {
          if (error.status === 401) return { kind: 'locked' as const, rejected: Boolean(token) };
          if (error.status === 503 || error.status === 404) {
            return { kind: 'unavailable' as const, message: error.status === 404
              ? 'Сервер не поддерживает загрузку PDF.' : error.message };
          }
        }
        throw error;
      }
    },
    create(file: File, domain: Domain, token: string, signal: AbortSignal) {
      const body = new FormData();
      body.set('file', file);
      body.set('domain', domain);
      return http.request(endpoint, signal, (response) => {
        if (response.status !== 201) throw new DataError('Сервер не подтвердил сохранение PDF.');
        return readJson(response, uploadSchema);
      }, { ...privateRequest(token), method: 'POST', body });
    },
    pdf(id: string, token: string, signal: AbortSignal) {
      return http.request(uploadEndpoint(id, 'file'), signal, readPdf, privateRequest(token));
    },
    process(id: string, token: string, signal: AbortSignal) {
      const accepted = z.object({ upload_id: z.literal(id), job_id: uploadId, status: processingStatus });
      return http.request(uploadEndpoint(id, 'process'), signal, (response) => {
        if (response.status !== 202) throw new DataError('Сервер не подтвердил постановку в очередь.');
        return readJson(response, accepted);
      }, { ...privateRequest(token), method: 'POST' });
    },
    processing(id: string, token: string, signal: AbortSignal) {
      return http.json(uploadEndpoint(id, 'processing'), processingSchema.refine((state) => state.upload_id === id),
        signal, privateRequest(token));
    },
    results(upload: StoredUpload, token: string, signal: AbortSignal) {
      const schema = resultSchema.refine((row) => row.domain === upload.domain
        && row.document_id === `${upload.domain}:${upload.upload_id}`
        && row.extraction_metadata?.pdf_sha256 === upload.sha256 && row.source.page <= upload.page_count);
      return http.json(uploadEndpoint(upload.upload_id, 'results'), z.array(schema), signal, privateRequest(token));
    },
    issues(upload: StoredUpload, token: string, signal: AbortSignal) {
      return http.json(uploadEndpoint(upload.upload_id, 'issues'), scoped(issueSchema, upload.domain),
        signal, privateRequest(token));
    },
    reviews(id: string, token: string, signal: AbortSignal) {
      return http.json(uploadEndpoint(id, 'reviews'), reviewOverviewSchema.refine((row) => row.processing.upload_id === id),
        signal, privateRequest(token));
    },
    review(id: string, result: string, token: string, signal: AbortSignal) {
      return http.json(reviewEndpoint(id, result), reviewDetailSchema.refine((row) => row.result_id === result),
        signal, privateRequest(token));
    },
    saveReview(id: string, result: string, token: string, body: ReviewRequest, signal: AbortSignal) {
      const init = privateRequest(token);
      return http.json(reviewEndpoint(id, result), reviewDetailSchema.refine((row) => row.result_id === result), signal, {
        ...init, method: 'POST', headers: { ...init.headers, 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
    },
    reviewHistory(id: string, result: string, token: string, signal: AbortSignal) {
      const schema = reviewEventSchema.refine((event) => event.decision.upload_id === id && event.decision.result_id === result);
      return http.json(`${reviewEndpoint(id, result)}/history`, z.array(schema), signal, privateRequest(token));
    },
    exportPreview(upload: StoredUpload, token: string, signal: AbortSignal) {
      const schema = exportPreviewSchema.refine(({ metadata }) =>
        metadata.upload_id === upload.upload_id && metadata.domain === upload.domain && metadata.pdf_sha256 === upload.sha256);
      return http.json(uploadEndpoint(upload.upload_id, 'export/manifest'), schema, signal, privateRequest(token));
    },
    downloadExport(preview: UploadExportPreview, format: UploadExportFormat, token: string, signal: AbortSignal) {
      const { upload_id: id, domain, snapshot_sha256: snapshot } = preview.metadata;
      const file = exportFiles[format];
      const init = privateRequest(token);
      return http.request(uploadEndpoint(id, `export/${file.route}`), signal, async (response) => {
        if (response.headers.get('Content-Type')?.split(';')[0] !== file.mime
          || response.headers.get('X-SPI-Snapshot-SHA256') !== snapshot) {
          throw new DataError('Полученный файл не соответствует выбранному формату или версии проекта.');
        }
        const blob = format === 'PDF' ? await readPdf(response) : await response.blob();
        if (!blob.size || (format === 'ZIP' && await blob.slice(0, 4).text() !== 'PK\x03\x04')) {
          throw new DataError('Сервер вернул пустой или повреждённый файл.');
        }
        return { blob, filename: format === 'ZIP' ? `spi-project-${domain}-${id}.zip` : file.filename };
      }, { ...init, headers: { ...init.headers, 'X-SPI-Snapshot-SHA256': snapshot } });
    },
  };
}

export type UploadClient = ReturnType<typeof createUploadClient>;

function scoped<T extends { domain: Domain }>(schema: z.ZodType<T>, domain?: Domain) {
  return z.array(domain ? schema.refine((row) => row.domain === domain) : schema);
}

export function requireCsvName(domain: Domain, filename: string) {
  if (!csvFiles[domain].some((name) => `${name}.csv` === filename)) throw new DataError('Неизвестный файл экспорта.');
  return filename;
}

export function jsonBlob(value: unknown) {
  return new Blob([`${JSON.stringify(value, null, 2)}\n`], { type: 'application/json' });
}

type ProjectPassportConfig = { path: string; runId: string; generatedAtHeader?: string };
const passportErrors = {
  404: 'Запуск или публикация недоступны (HTTP 404). Проверьте выбранную публикацию и VITE_PROJECT_RUN_ID.',
  409: 'Идентификатор публикации неоднозначен (HTTP 409).',
  500: 'Серверу не удалось сформировать полный PDF-паспорт (HTTP 500). Повторите попытку.',
  503: 'Серверный PDF-паспорт недоступен (HTTP 503). Проверьте SPI_RUN_ID на сервере.',
};

export function createApiProvider({
  base = '/api', csvBase, fetcher = fetch, timeoutMs = 20_000, passport,
}: { base?: string; csvBase?: string; fetcher?: typeof fetch; timeoutMs?: number; passport?: ProjectPassportConfig } = {}): Provider {
  const http = client(fetcher, timeoutMs);
  const endpoint = (path: string) => joinUrl(base, path);
  const listEndpoint = (path: string, domain?: Domain) => endpoint(domain ? `${path}?domain=${domain}` : path);
  const projectPassport: PassportExport | undefined = passport && {
    runId: passport.runId,
    async download(scope, signal) {
      const url = `${endpoint(passport.path)}?${passportQuery(passport.runId, scope)}`;
      return client(fetcher, 120_000, passportErrors).request(url, signal, async (response) => {
        if (response.status === 202) throw new DataError('Сервер ещё не вернул готовый паспорт. Повторите попытку позже.');
        const blob = await readPdf(response);
        const trailer = await blob.slice(Math.max(0, blob.size - 1024)).text();
        if (!trailer.trimEnd().endsWith('%%EOF')) throw new DataError('PDF-паспорт загружен не полностью.');
        const timestamp = response.headers.get(passport.generatedAtHeader || 'X-SPI-Generated-At');
        return {
          blob,
          filename: passportFilename(passport.runId, scope),
          generatedAt: timestamp && Number.isFinite(Date.parse(timestamp)) ? timestamp : null,
        };
      });
    },
  };
  const csvRoot = csvBase?.trim();
  return {
    mode: 'api',
    csvAvailable: Boolean(csvRoot),
    projectPassport,
    documents: (domain, signal) => http.json(listEndpoint('documents', domain), scoped(documentSchema, domain), signal),
    results: (domain, signal) => http.json(listEndpoint('results', domain), scoped(resultSchema, domain), signal),
    issues: (domain, signal) => http.json(listEndpoint('issues', domain), scoped(issueSchema, domain), signal),
    result: (domain, id, signal) => http.json(endpoint(`results/${encodeURIComponent(id)}`),
      resultSchema.refine((row) => (!domain || row.domain === domain) && row.result_id === id), signal),
    async pdf(domain, documentId, signal) {
      if (!belongsTo(documentId, domain)) throw new DataError('Идентификатор документа не соответствует записи.');
      return http.pdf(endpoint(`documents/${encodeURIComponent(documentId)}/file`), signal);
    },
    async csv(domain, filename, signal) {
      if (!csvRoot) throw new DataError('Папка с CSV-выгрузкой пока не подключена.');
      return http.csv(joinUrl(csvRoot, `${domain}/${requireCsvName(domain, filename)}`), signal);
    },
    exportJson: (artifact, signal) => http.request(endpoint(`export/${artifact}`), signal, async (response) => {
      const bytes = await response.arrayBuffer();
      let content: unknown;
      try { content = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
      catch { throw new DataError('Выгрузка содержит некорректный JSON.'); }
      if (!Array.isArray(content) || content.some((item) => !item || typeof item !== 'object' || Array.isArray(item))) {
        throw new DataError('Выгрузка не соответствует формату архива.');
      }
      return new Blob([bytes], { type: 'application/json;charset=utf-8' });
    }),
  };
}

export function localFiles(base: string, fetcher = fetch) {
  const http = client(fetcher, 20_000);
  return {
    pdf: (filename: string, signal: AbortSignal) => http.pdf(joinUrl(base, filename), signal),
    csv: (domain: Domain, filename: string, signal: AbortSignal) =>
      http.csv(joinUrl(base, `csv/${domain}/${requireCsvName(domain, filename)}`), signal),
  };
}
