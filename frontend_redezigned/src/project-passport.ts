import type { Domain } from './domain';

export type PassportScope = { domain: 'all' } | { domain: Domain; publicationId?: string };

export interface PassportFile {
  blob: Blob;
  filename: string;
  generatedAt: string | null;
}

export interface PassportExport {
  runId: string;
  download(scope: PassportScope, signal: AbortSignal): Promise<PassportFile>;
}

const filenamePart = (value: string) => value.replace(/[^\p{L}\p{N}._-]+/gu, '_').slice(0, 80) || 'project';

export function passportFilename(runId: string, scope: PassportScope) {
  const publication = scope.domain !== 'all' && scope.publicationId ? `_${filenamePart(scope.publicationId)}` : '';
  return `scientific-pdf-intelligence_passport_${filenamePart(runId)}_${scope.domain}${publication}.pdf`;
}

export function passportQuery(runId: string, scope: PassportScope) {
  const params = new URLSearchParams({ run_id: runId, domain: scope.domain });
  if (scope.domain !== 'all' && scope.publicationId) params.set('publication_id', scope.publicationId);
  return params.toString();
}
