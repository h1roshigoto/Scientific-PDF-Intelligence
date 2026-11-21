import { z } from 'zod';
import { domainSchema, issueSchema, resultSchema } from './domain';
import type { Result } from './domain';

const decisionStatus = z.enum(['correction_proposed', 'approved', 'approved_corrected', 'rejected']);
const reviewStatus = z.enum(['pending', 'stale', ...decisionStatus.options]);
const reviewAction = z.enum(['approve', 'propose_correction', 'approve_correction', 'reject']);
const evidenceMode = z.enum(['text', 'visual']);
const revision = z.number().int().nonnegative();
const sha256 = z.string().regex(/^[a-f0-9]{64}$/);
const opaqueId = z.string().regex(/^[a-f0-9]{32}$/);
export const pdfStatus = z.enum(['valid', 'missing', 'mismatch']);

const basisSchema = z.object({
  upload_id: opaqueId, domain: domainSchema, result_id: z.string().min(1),
  source_job_id: opaqueId, pdf_sha256: sha256, source_result_hash: sha256,
});

const decisionSchema = basisSchema.extend({
  review_id: opaqueId, revision: revision.positive(), status: decisionStatus,
  reviewed_at: z.iso.datetime({ offset: true }), reviewer_label: z.string().nullable(), reason: z.string().nullable(),
  evidence_mode: evidenceMode.nullable(), warnings_acknowledged: z.boolean(),
  normalization_evidence: z.string().nullable(), reviewed_result: resultSchema.nullable(),
});

export const reviewStateSchema = z.object({
  result_id: z.string().min(1), status: reviewStatus, revision, applicable: z.boolean(),
  basis: basisSchema.nullable(), original_result: resultSchema.nullable(), decision: decisionSchema.nullable(),
});

export const reviewEventSchema = z.object({
  action: reviewAction, previous_revision: revision, revision: revision.positive(),
  original_result: resultSchema, previous_decision: decisionSchema.nullable(), decision: decisionSchema,
  issues: z.array(issueSchema), ocr_pages: z.array(z.number().int().positive()),
});

export type ReviewAction = z.infer<typeof reviewAction>;
export type ReviewState = z.infer<typeof reviewStateSchema>;
export type EvidenceMode = z.infer<typeof evidenceMode>;
export type Correction = Partial<Pick<Result, 'raw' | 'normalized' | 'context' | 'source'>> & {
  metric?: Pick<Result['metric'], 'name' | 'k'>;
  normalization_evidence?: string;
};
export type ReviewRequest = {
  basis: z.infer<typeof basisSchema>;
  expected_revision: number;
  action: ReviewAction;
  reviewer_label: string | null;
  reason: string | null;
  correction?: Correction;
  evidence_mode?: EvidenceMode;
  warnings_acknowledged?: boolean;
};

export const reviewLabels: Record<ReviewState['status'], string> = {
  pending: 'Ожидает проверки', stale: 'Требуется повторная проверка',
  correction_proposed: 'Предложено исправление', approved: 'Подтверждено',
  approved_corrected: 'Исправление подтверждено', rejected: 'Отклонено',
};

export const isApproved = (status: ReviewState['status']) => status === 'approved' || status === 'approved_corrected';

export const evidenceLabels = {
  page: 'Страница PDF', fragment: 'Фрагмент', table: 'Таблица', figure: 'Рисунок',
  section: 'Раздел', row: 'Строка', column: 'Столбец', origin: 'Происхождение',
  cited_publication_id: 'Цитируемая публикация',
};
