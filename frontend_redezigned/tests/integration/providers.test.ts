import { readFile } from 'node:fs/promises';
import { describe, expect, it, vi } from 'vitest';
import { createApiProvider } from '../../src/data';
import { createMockProvider } from '../../src/mock';
import fixture from '../../fixtures/demo.json';

const signal = () => new AbortController().signal;
const response = (value: unknown) => Response.json(value);

describe('fixture provider', () => {
  it('implements the same read interface with separate D001 source files', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const bytes = await readFile(new URL(`../../public${String(input)}`, import.meta.url));
      return new Response(bytes);
    });
    const mock = createMockProvider({ fetcher });
    for (const [track, id, page, expectedName] of [['solar', 'demo-solar-d001-voc', 5, 'Voc'], ['rag', 'demo-rag-d001-em', 6, 'EM']] as const) {
      const result = await mock.result(track, id, signal());
      expect(result.metric.name).toBe(expectedName);
      expect(result.source.page).toBe(page);
      expect(await mock.pdf(track, result.document_id, signal())).toHaveProperty('type', 'application/pdf');
      const csv = await mock.csv(track, 'results.csv', signal());
      expect(await csv.text()).toContain(id);
    }
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/demo/solar-D001.pdf', '/demo/csv/solar/results.csv', '/demo/rag-D001.pdf', '/demo/csv/rag/results.csv']);
    await expect(mock.pdf('rag', 'solar:D001', signal())).rejects.toThrow('PDF недоступен');
  });

  it('supports an empty dataset and aborting a load', async () => {
    const empty = createMockProvider({ fixture: { documents: [], results: [], issues: [] } });
    await expect(empty.results('rag', signal())).resolves.toEqual([]);
    const controller = new AbortController();
    controller.abort();
    await expect(empty.documents('solar', controller.signal)).rejects.toHaveProperty('name', 'AbortError');
  });
});

describe('HTTP adapter', () => {
  it('calls the documented routes and parses API records, metadata and issues', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(async () => response([fixture.results[1]]))
      .mockImplementationOnce(async () => response(fixture.results[1]))
      .mockImplementationOnce(async () => response([{ ...fixture.documents[0], file_path: 'private' }]))
      .mockImplementationOnce(async () => response([fixture.issues[1]]))
      .mockImplementationOnce(async () => response(fixture.results));
    const api = createApiProvider({ fetcher });
    const rows = await api.results('solar', signal());
    expect(rows[0].normalized).toEqual({ value: 1.2, unit: 'В' });
    expect(await api.result('solar', rows[0].result_id, signal())).toEqual(rows[0]);
    expect((await api.documents('solar', signal()))[0]).not.toHaveProperty('file_path');
    expect((await api.issues('rag', signal()))[0].issue_id).toBe('demo-rag-ocr');
    expect(await (await api.exportJson('results.json', signal())).text()).toContain('demo-rag-d001-em');
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual([
      '/api/results?domain=solar', '/api/results/demo-solar-d001-voc', '/api/documents?domain=solar', '/api/issues?domain=rag', '/api/export/results.json',
    ]);
  });

  it.each([400, 422, 503])('surfaces HTTP %s for retry', async (status) => {
    const api = createApiProvider({ fetcher: async () => new Response('', { status }) });
    await expect(api.results('solar', signal())).rejects.toThrow(`HTTP ${status}`);
  });

  it('rejects malformed JSON and invalid or cross-track records', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(async () => new Response('{broken'))
      .mockImplementationOnce(async () => response([fixture.results[4]]))
      .mockImplementationOnce(async () => response([{ ...fixture.results[1], source: { page: 0 } }]));
    const api = createApiProvider({ fetcher });
    await expect(api.results('solar', signal())).rejects.toThrow('некорректный JSON');
    await expect(api.results('solar', signal())).rejects.toThrow('не соответствует контракту');
    await expect(api.results('solar', signal())).rejects.toThrow('не соответствует контракту');
  });

  it('handles missing PDFs, rejects an HTML response and checks track identity', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(async () => new Response('', { status: 404 }))
      .mockImplementationOnce(async () => new Response('<!doctype html>'));
    const api = createApiProvider({ fetcher });
    await expect(api.pdf('solar', 'solar:D001', signal())).rejects.toThrow('недоступны');
    expect(fetcher.mock.calls[0][0]).toBe('/api/documents/solar%3AD001/file');
    await expect(api.pdf('rag', 'rag:D001', signal())).rejects.toThrow('распознать как PDF');
    await expect(api.pdf('rag', 'solar:D001', signal())).rejects.toThrow('не соответствует записи');
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('times out a pending request', async () => {
    const api = createApiProvider({ timeoutMs: 10, fetcher: async (_path, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    }) });
    await expect(api.results('solar', signal())).rejects.toThrow('Истекло время ожидания');
  });

  it('loads the complete catalog through the documented unscoped routes', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(fixture.documents))
      .mockResolvedValueOnce(response(fixture.results))
      .mockResolvedValueOnce(response(fixture.issues));
    const api = createApiProvider({ fetcher });
    expect(await api.documents(undefined, signal())).toHaveLength(fixture.documents.length);
    expect(await api.results(undefined, signal())).toHaveLength(fixture.results.length);
    expect(await api.issues(undefined, signal())).toHaveLength(fixture.issues.length);
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/documents', '/api/results', '/api/issues']);
  });

  it('preserves stored JSON bytes, including server-side archival fields', async () => {
    const archived = '[{"result_id":"previous-run","extraction_metadata":{"schema_version":"archived"}}]\n';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(archived))
      .mockResolvedValueOnce(new Response('<!doctype html>'));
    const api = createApiProvider({ fetcher });
    expect(await (await api.exportJson('results.json', signal())).text()).toBe(archived);
    await expect(api.exportJson('results.json', signal())).rejects.toThrow('некорректный JSON');
  });

  it('validates CSV bytes and leaves HTTP export routes scoped to internal JSON', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(async () => new Response('<!doctype html>'))
      .mockImplementationOnce(async () => new Response('document,value\r\nD001,0.25\r\n'));
    const api = createApiProvider({ csvBase: '/artifacts', fetcher });
    await expect(api.csv('solar', 'results.csv', signal())).rejects.toThrow('UTF-8');
    expect(await (await api.csv('rag', 'results.csv', signal())).text()).toBe('document,value\r\nD001,0.25\r\n');
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/artifacts/solar/results.csv', '/artifacts/rag/results.csv']);
    await expect(api.csv('rag', '../results.csv', signal())).rejects.toThrow('Неизвестный файл');
    await expect(createApiProvider({ fetcher }).csv('rag', 'results.csv', signal())).rejects.toThrow('пока не подключена');
  });
});
