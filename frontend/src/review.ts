import { belongsTo } from './domain';
import type { Issue } from './domain';

const checkLabels: Record<string, string> = {
  publication_scope: 'Допуск публикации',
  confirmed_duplicate: 'Подтверждённый повтор',
  suspected_duplicate: 'Возможный повтор',
  pce_check_pass: 'Формула PCE сошлась',
  pce_mismatch: 'Расхождение PCE',
  pce_not_evaluable: 'PCE нельзя проверить',
  field_not_reported: 'Не указано',
  field_extraction_failed: 'Указано, но не извлечено',
  field_undetermined: 'Наличие не установлено',
  supplement_unavailable: 'Приложение не предоставлено',
};

export function checkLabel(issue: Issue) {
  return Object.hasOwn(checkLabels, issue.rule_id) ? checkLabels[issue.rule_id] : issue.rule_id;
}

export function checkValues(issue: Issue): [string, string][] {
  const isPceComparison = issue.rule_id === 'pce_mismatch' || issue.rule_id === 'pce_check_pass';
  const values: [string, string | null][] = [
    [isPceComparison ? 'Заявленный PCE' : 'Наблюдение', issue.observed],
    [isPceComparison ? 'Расчётный PCE' : 'Ожидаемое', issue.expected],
  ];
  return values.flatMap(([label, value]) => value ? [[label, value]] : []);
}

export function evidenceLinks(issue: Issue) {
  return issue.evidence_refs.flatMap((ref) => {
    const { document_id: documentId, page, label } = ref;
    if (typeof documentId !== 'string' || !belongsTo(documentId, issue.domain)) return [];
    if (typeof page !== 'number' || !Number.isSafeInteger(page) || page < 1) return [];
    return [{ documentId, page, label: typeof label === 'string' ? label.trim() : '' }];
  });
}

export function publicationScope(issues: Issue[], documentId: string) {
  const decision = issues.find((issue) => issue.rule_id === 'publication_scope'
    && evidenceLinks(issue).some((source) => source.documentId === documentId));
  const state = decision?.observed;
  if (state === 'in_scope') return { label: 'В области кейса', tone: 'good' } as const;
  if (state === 'out_of_scope') return { label: 'Вне области кейса', tone: 'warn' } as const;
  if (state === 'needs_review') return { label: 'Допуск нужно проверить', tone: 'warn' } as const;
  return null;
}

export function orderedLayers(value: unknown) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((layer: unknown) => {
    if (typeof layer !== 'object' || layer === null || Array.isArray(layer)) return [];
    return [layer as Record<string, unknown>];
  }).sort((a, b) => layerOrder(a.layer_order) - layerOrder(b.layer_order));
}

function layerOrder(value: unknown) {
  if (typeof value === 'string' && !value.trim()) return Number.POSITIVE_INFINITY;
  if (typeof value !== 'number' && typeof value !== 'string') return Number.POSITIVE_INFINITY;
  const order = Number(value);
  return Number.isSafeInteger(order) && order > 0 ? order : Number.POSITIVE_INFINITY;
}
