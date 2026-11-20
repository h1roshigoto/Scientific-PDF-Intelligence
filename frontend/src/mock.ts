import { z } from 'zod';
import { documentSchema, issueSchema, publicationId, resultSchema } from './domain';
import { DataError, jsonBlob, localFiles } from './data';
import type { Domain } from './domain';
import type { Provider } from './data';
import { passportFilename } from './project-passport';
import seed from '../fixtures/demo.json';

const demoSchema = z.object({
  documents: z.array(documentSchema), results: z.array(resultSchema), issues: z.array(issueSchema),
  passport_snapshot: z.object({ run_id: z.string().min(1), generated_at: z.iso.datetime() }).optional(),
});

export function createMockProvider({ fixture = seed, base = '/demo', fetcher = fetch }: {
  fixture?: unknown; base?: string; fetcher?: typeof fetch;
} = {}): Provider {
  const demo = demoSchema.parse(fixture);
  const snapshot = demo.passport_snapshot;
  const files = localFiles(base, fetcher);
  const byDomain = <T extends { domain: Domain }>(items: T[], domain: Domain, signal: AbortSignal) => {
    signal.throwIfAborted();
    return items.filter((item) => item.domain === domain);
  };
  return {
    mode: 'mock',
    csvAvailable: true,
    projectPassport: snapshot && {
      runId: snapshot.run_id,
      async download(scope, signal) {
        if (scope.domain !== 'all' && scope.publicationId && !demo.documents.some((doc) =>
          doc.domain === scope.domain && publicationId(doc.document_id) === scope.publicationId)) {
          throw new DataError('Публикация отсутствует в демонстрационном запуске.');
        }
        const filename = passportFilename(snapshot.run_id, scope);
        const blob = await files.pdf(`passports/${filename}`, signal);
        return { blob, filename, generatedAt: snapshot.generated_at };
      },
    },
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
