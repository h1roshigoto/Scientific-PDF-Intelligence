import { readFile } from 'node:fs/promises';
import { describe, expect, it, vi } from 'vitest';
import { createApiProvider } from '../../src/data';
import { createMockProvider } from '../../src/mock';

const signal = () => new AbortController().signal;
const pdf = '%PDF-1.4\n%%EOF\n';

describe('project passport provider', () => {
  it('leaves the capability unavailable until it has an endpoint and run', () => {
    expect(createApiProvider().projectPassport).toBeUndefined();
  });

  it('requests the selected track and publication for the configured run', async () => {
    const fetcher = vi.fn<typeof fetch>(async () => new Response(pdf, {
      headers: { 'X-Report-Generated-At': '2026-11-20T17:40:00Z' },
    }));
    const { projectPassport } = createApiProvider({ base: '/gateway', fetcher, passport: {
      path: 'reports/project.pdf', runId: 'run 7&2026', generatedAtHeader: 'X-Report-Generated-At',
    } });
    const report = await projectPassport!.download({ domain: 'rag', publicationId: 'D001' }, signal());
    expect(fetcher.mock.calls[0][0]).toBe('/gateway/reports/project.pdf?run_id=run+7%262026&domain=rag&publication_id=D001');
    expect(report.filename).toBe('scientific-pdf-intelligence_passport_run_7_2026_rag_D001.pdf');
    expect(report.generatedAt).toBe('2026-11-20T17:40:00Z');
    expect(report.blob.type).toBe('application/pdf');
    expect(await report.blob.text()).toBe(pdf);
    await projectPassport!.download({ domain: 'all' }, signal());
    expect(fetcher.mock.calls[1][0]).toBe('/gateway/reports/project.pdf?run_id=run+7%262026&domain=all');
  });

  it('rejects deferred, truncated and non-PDF responses instead of saving them', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('{"state":"pending"}', { status: 202 }))
      .mockResolvedValueOnce(new Response('%PDF-1.4\n'))
      .mockResolvedValueOnce(new Response('<!doctype html>'));
    const { projectPassport } = createApiProvider({ fetcher, passport: { path: 'export/project-passport.pdf', runId: 'r1' } });
    await expect(projectPassport!.download({ domain: 'solar' }, signal())).rejects.toThrow('ещё не вернул');
    await expect(projectPassport!.download({ domain: 'solar' }, signal())).rejects.toThrow('не полностью');
    await expect(projectPassport!.download({ domain: 'solar' }, signal())).rejects.toThrow('распознать как PDF');
  });

  it('keeps the two D001 files distinct in mock mode and rejects an unknown publication', async () => {
    const fetcher = vi.fn<typeof fetch>(async (url) => {
      const bytes = await readFile(new URL(`../../public${String(url)}`, import.meta.url));
      return new Response(bytes);
    });
    const { projectPassport } = createMockProvider({ fetcher });
    const solar = await projectPassport!.download({ domain: 'solar', publicationId: 'D001' }, signal());
    const rag = await projectPassport!.download({ domain: 'rag', publicationId: 'D001' }, signal());
    expect(rag.filename).not.toBe(solar.filename);
    expect(solar.generatedAt).toBe('2026-11-20T17:40:00Z');
    await expect(projectPassport!.download({ domain: 'rag', publicationId: 'D006' }, signal())).rejects.toThrow('отсутствует');
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
