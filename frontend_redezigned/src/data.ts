import { z } from 'zod';
import { belongsTo, csvFiles, documentSchema, issueSchema, resultSchema } from './domain';
import type { Domain, Issue, Result, ScientificDocument } from './domain';
import { passportFilename, passportQuery } from './project-passport';
import type { PassportExport } from './project-passport';

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
  constructor(message: string) {
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

function client(fetcher: typeof fetch, timeoutMs: number) {
  async function request<T>(url: string, signal: AbortSignal, read: (response: Response) => Promise<T>, init?: RequestInit) {
    const timeout = AbortSignal.timeout(timeoutMs);
    try {
      const response = await fetcher(url, { ...init, signal: AbortSignal.any([signal, timeout]) });
      if (response.status === 404) throw new DataError('Запрошенный файл или запись недоступны.');
      if (!response.ok) throw new DataError(`Сервер вернул ошибку HTTP ${response.status}.`);
      return await read(response);
    } catch (error) {
      if (signal.aborted) throw signal.reason;
      if (timeout.aborted) throw new DataError('Истекло время ожидания ответа сервера.');
      if (error instanceof DataError) throw error;
      throw new DataError('Не удалось связаться с сервером. Проверьте подключение.');
    }
  }

  function json<T>(url: string, schema: z.ZodType<T>, signal: AbortSignal, init?: RequestInit) {
    return request(url, signal, async (response) => {
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
    }, init);
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
      return client(fetcher, 120_000).request(url, signal, async (response) => {
        if (response.status === 202) throw new DataError('Сервер ещё не вернул готовый паспорт. Повторите попытку позже.');
        const blob = await readPdf(response);
        const trailer = await blob.slice(Math.max(0, blob.size - 1024)).text();
        if (!trailer.trimEnd().endsWith('%%EOF')) throw new DataError('PDF-паспорт загружен не полностью.');
        const timestamp = passport.generatedAtHeader ? response.headers.get(passport.generatedAtHeader) : null;
        return {
          blob,
          filename: passportFilename(passport.runId, scope),
          generatedAt: timestamp && Number.isFinite(Date.parse(timestamp)) ? timestamp : null,
        };
      });
    },
  };
  return {
    mode: 'api',
    csvAvailable: Boolean(csvBase),
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
      if (!csvBase) throw new DataError('Папка с CSV-выгрузкой пока не подключена.');
      return http.csv(joinUrl(csvBase, `${domain}/${requireCsvName(domain, filename)}`), signal);
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
