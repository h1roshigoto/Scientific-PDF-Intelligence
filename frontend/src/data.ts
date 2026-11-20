import { z } from 'zod';
import { belongsTo, csvFiles, documentSchema, issueSchema, resultSchema } from './domain';
import type { Domain, Issue, Result, ScientificDocument } from './domain';

export type InternalArtifact = 'results.json' | 'issues.json';

export interface Provider {
  readonly mode: 'mock' | 'api';
  readonly csvAvailable: boolean;
  documents(domain: Domain, signal: AbortSignal): Promise<ScientificDocument[]>;
  results(domain: Domain, signal: AbortSignal): Promise<Result[]>;
  result(domain: Domain, id: string, signal: AbortSignal): Promise<Result>;
  issues(domain: Domain, signal: AbortSignal): Promise<Issue[]>;
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

function client(fetcher: typeof fetch, timeoutMs: number) {
  async function request<T>(url: string, signal: AbortSignal, read: (response: Response) => Promise<T>) {
    const timeout = AbortSignal.timeout(timeoutMs);
    try {
      const response = await fetcher(url, { signal: AbortSignal.any([signal, timeout]) });
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

  function json<T>(url: string, schema: z.ZodType<T>, signal: AbortSignal) {
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
    });
  }

  function pdf(url: string, signal: AbortSignal) {
    return request(url, signal, async (response) => {
      const bytes = await response.arrayBuffer();
      const prefix = new TextDecoder().decode(bytes.slice(0, 1024));
      if (!prefix.includes('%PDF-')) throw new DataError('Источник вернул файл, который не удалось распознать как PDF.');
      return new Blob([bytes], { type: 'application/pdf' });
    });
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
  return { json, pdf, csv };
}

function scoped<T extends { domain: Domain }>(schema: z.ZodType<T>, domain: Domain) {
  return z.array(schema.refine((row) => row.domain === domain));
}

export function requireCsvName(domain: Domain, filename: string) {
  if (!csvFiles[domain].some((name) => `${name}.csv` === filename)) throw new DataError('Неизвестный файл экспорта.');
  return filename;
}

export function jsonBlob(value: unknown) {
  return new Blob([`${JSON.stringify(value, null, 2)}\n`], { type: 'application/json' });
}

export function createApiProvider({
  base = '/api', csvBase, fetcher = fetch, timeoutMs = 20_000,
}: { base?: string; csvBase?: string; fetcher?: typeof fetch; timeoutMs?: number } = {}): Provider {
  const http = client(fetcher, timeoutMs);
  const endpoint = (path: string) => joinUrl(base, path);
  return {
    mode: 'api',
    csvAvailable: Boolean(csvBase),
    documents: (domain, signal) => http.json(endpoint(`documents?domain=${domain}`), scoped(documentSchema, domain), signal),
    results: (domain, signal) => http.json(endpoint(`results?domain=${domain}`), scoped(resultSchema, domain), signal),
    issues: (domain, signal) => http.json(endpoint(`issues?domain=${domain}`), scoped(issueSchema, domain), signal),
    result: (domain, id, signal) => http.json(endpoint(`results/${encodeURIComponent(id)}`),
      resultSchema.refine((row) => row.domain === domain && row.result_id === id), signal),
    async pdf(domain, documentId, signal) {
      if (!belongsTo(documentId, domain)) throw new DataError('Документ относится к другому треку.');
      return http.pdf(endpoint(`documents/${encodeURIComponent(documentId)}/file`), signal);
    },
    async csv(domain, filename, signal) {
      if (!csvBase) throw new DataError('Папка с CSV-выгрузкой пока не подключена.');
      return http.csv(joinUrl(csvBase, `${domain}/${requireCsvName(domain, filename)}`), signal);
    },
    async exportJson(artifact, signal) {
      const url = endpoint(`export/${artifact}`);
      const value = artifact === 'results.json' ? await http.json(url, z.array(resultSchema), signal)
        : await http.json(url, z.array(issueSchema), signal);
      return jsonBlob(value);
    },
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
