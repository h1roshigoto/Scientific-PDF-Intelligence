import { z } from 'zod';
import { documentSchema, issueSchema, publicationId, resultSchema } from './domain';
import { DataError, jsonBlob, localFiles } from './data';
import type { Domain } from './domain';
import type { Provider } from './data';
import seed from '../fixtures/demo.json';

const demoSchema = z.object({
  documents: z.array(documentSchema), results: z.array(resultSchema), issues: z.array(issueSchema),
});

export function createMockProvider({ fixture = seed, base = '/demo', fetcher = fetch }: {
  fixture?: unknown; base?: string; fetcher?: typeof fetch;
} = {}): Provider {
  const demo = demoSchema.parse(fixture);
  const files = localFiles(base, fetcher);
  const byDomain = <T extends { domain: Domain }>(items: T[], domain: Domain, signal: AbortSignal) => {
    signal.throwIfAborted();
    return items.filter((item) => item.domain === domain);
  };
  return {
    mode: 'mock',
    csvAvailable: true,
    async results(domain, signal) { return byDomain(demo.results, domain, signal); },
    async documents(domain, signal) { return byDomain(demo.documents, domain, signal); },
    async issues(domain, signal) { return byDomain(demo.issues, domain, signal); },
    async result(domain, id, signal) {
      const row = byDomain(demo.results, domain, signal).find((entry) => entry.result_id === id);
      if (!row) throw new DataError('Результат недоступен.');
      return row;
    },
    async pdf(domain, documentId, signal) {
      const doc = byDomain(demo.documents, domain, signal).find((entry) => entry.document_id === documentId);
      if (!doc) throw new DataError('Демонстрационный PDF недоступен.');
      return files.pdf(`${domain}-${encodeURIComponent(publicationId(documentId))}.pdf`, signal);
    },
    csv: files.csv,
    async exportJson(artifact, signal) {
      signal.throwIfAborted();
      return jsonBlob(artifact === 'results.json' ? demo.results : demo.issues);
    },
  };
}
