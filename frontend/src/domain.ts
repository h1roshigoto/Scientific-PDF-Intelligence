import { z } from 'zod';

export const domainSchema = z.enum(['solar', 'rag']);
export type Domain = z.infer<typeof domainSchema>;
export const domainName: Record<Domain, string> = { solar: 'Solar', rag: 'RAG' };

const nonempty = z.string().min(1);
const optionalText = z.string().nullable().default(null);
const pageNumber = z.number().int().positive();
const value = z.object({
  value: z.union([z.string(), z.number()]).nullable().default(null),
  unit: optionalText,
});

export const evidenceSchema = z.object({
  page: pageNumber,
  fragment: optionalText,
  section: optionalText,
  table: optionalText,
  figure: optionalText,
  row: optionalText,
  column: optionalText,
  origin: z.enum(['primary', 'cited']).nullable().default(null),
  cited_publication_id: optionalText,
}).refine((source) => source.fragment || source.table || source.figure || source.section);

export const resultSchema = z.object({
  result_id: nonempty,
  document_id: nonempty,
  domain: domainSchema,
  metric: z.object({ name: nonempty, k: z.number().int().nonnegative().nullable().default(null), variant: optionalText }),
  raw: value.refine((raw) => raw.value !== null),
  normalized: value,
  source: evidenceSchema,
  context: z.record(z.string(), z.unknown()).default({}),
  extraction_status: z.enum(['extracted', 'needs_review', 'verified']).default('needs_review'),
  note: optionalText,
}).refine((row) => belongsTo(row.document_id, row.domain));

export const documentSchema = z.object({
  document_id: nonempty,
  domain: domainSchema,
  publication_id: optionalText,
  filename: optionalText,
  title: optionalText,
  doi: optionalText,
  DOI: optionalText,
  page_count: z.number().int().nonnegative().nullable().default(null),
  pages_needing_ocr: z.array(pageNumber).default([]),
  processing_status: optionalText,
}).refine((row) => belongsTo(row.document_id, row.domain));

export const issueSchema = z.object({
  issue_id: nonempty,
  domain: domainSchema,
  rule_id: nonempty,
  severity: z.enum(['info', 'warning', 'error']).default('warning'),
  result_ids: z.array(z.string()).default([]),
  evidence_refs: z.array(z.record(z.string(), z.unknown())).default([]),
  reason: nonempty,
  observed: optionalText,
  expected: optionalText,
});

export type Result = z.infer<typeof resultSchema>;
export type ScientificDocument = z.infer<typeof documentSchema>;
export type Issue = z.infer<typeof issueSchema>;
export type Evidence = z.infer<typeof evidenceSchema>;
export type Status = Result['extraction_status'];
export const statusLabel: Record<Status, string> = {
  extracted: 'Извлечено', needs_review: 'Нужна проверка', verified: 'Сверено',
};

export function belongsTo(documentId: string, domain: Domain) {
  return documentId.startsWith(`${domain}:`) && documentId.length > domain.length + 1;
}

export function publicationId(documentId: string) {
  return documentId.slice(documentId.indexOf(':') + 1);
}

export function pdfPageUrl(url: string, page: number) {
  return `${url.split('#', 1)[0]}#page=${pageNumber.parse(page)}`;
}

const decimal = new Intl.NumberFormat('ru-RU', { maximumSignificantDigits: 21 });

export function formatValue(entry: z.infer<typeof value>, preserveRaw = false) {
  if (entry.value === null || entry.value === '') return '—';
  const display = typeof entry.value === 'number' && !preserveRaw ? decimal.format(entry.value) : String(entry.value);
  return entry.unit === null || entry.unit === '' ? display : `${display} ${entry.unit}`;
}

const contextLabels: Record<string, string> = {
  device_label: 'Прибор', material_name: 'Материал', scan_direction: 'Направление развёртки',
  aggregation: 'Агрегация', dataset_name: 'Набор данных', generator_model: 'Генератор',
  method: 'Метод', top_k: 'top_k конфигурации', evaluation_protocol: 'Протокол оценки',
  measurement_type: 'Измерение', experiment_id: 'Эксперимент', task_type: 'Задача', device_id: 'ID прибора',
  measurement_id: 'ID измерения', config_id: 'ID конфигурации', dataset_id: 'ID набора',
  material_name_raw: 'Материал', formula_normalized: 'Формула', material_class: 'Класс материала', bandgap_value: 'Ширина запрещённой зоны',
  bandgap_unit: 'Единица Eg', device_role: 'Роль устройства', device_type: 'Тип устройства', architecture: 'Архитектура', substrate_type: 'Подложка',
  active_area: 'Активная площадь', area_definition: 'Определение площади', spectrum: 'Спектр', light_intensity: 'Интенсивность света',
  intensity_unit: 'Единица интенсивности', scan_rate: 'Скорость развёртки', mask_used: 'Маска', temperature: 'Температура',
  measurement_instrument: 'Оборудование', stability_protocol: 'Протокол стабильности', stress_conditions: 'Условия испытания',
  layer_order: 'Порядок', layer_function: 'Функция', layer_material: 'Материал слоя', layer_thickness: 'Толщина', thickness_unit: 'Единица толщины',
  deposition_method: 'Способ нанесения', annealing_temperature: 'Температура отжига', annealing_time: 'Время отжига', atmosphere: 'Атмосфера',
  notes: 'Примечания', note: 'Примечание',
};

export function contextLabel(key: string) {
  return Object.hasOwn(contextLabels, key) ? contextLabels[key] : key;
}

export function contextText(value: unknown): string {
  if (value == null || value === '') return '—';
  if (Array.isArray(value)) return value.map(contextText).join(', ') || '—';
  if (typeof value === 'object') {
    return Object.entries(value).map(([key, entry]) => `${contextLabel(key)} — ${contextText(entry)}`).join(' · ') || '—';
  }
  if (typeof value === 'boolean') return value ? 'Да' : 'Нет';
  return String(value);
}

export function subject(result: Result) {
  return contextText(result.domain === 'solar'
    ? result.context.device_label ?? result.context.device_id ?? result.source.row
    : result.context.dataset_name ?? result.context.dataset_id);
}

export function sourceLabel(source: Evidence) {
  return [`стр. ${source.page}`, source.table, source.figure, source.section].filter(Boolean).join(' · ');
}

export type Filters = { query: string; metric: string; subject: string; status: string; source: string };
export const emptyFilters: Filters = { query: '', metric: '', subject: '', status: '', source: '' };

export function filterResults(rows: Result[], documents: ScientificDocument[], filters: Filters) {
  const query = filters.query.trim().toLocaleLowerCase('ru');
  const documentsById = new Map(documents.map((doc) => [doc.document_id, doc]));
  return rows.filter((row) => {
    const doc = documentsById.get(row.document_id);
    const search = [row.document_id, doc?.title, doc?.doi, doc?.DOI, doc?.filename, row.metric.name, contextText(row.context)].join(' ').toLocaleLowerCase('ru');
    return search.includes(query) && (!filters.metric || row.metric.name === filters.metric)
      && (!filters.subject || subject(row) === filters.subject)
      && (!filters.status || row.extraction_status === filters.status)
      && (!filters.source || sourceLabel(row.source) === filters.source);
  });
}

export function selectResult(rows: Result[], selectedId: string | null) {
  return rows.find((row) => row.result_id === selectedId) ?? rows[0] ?? null;
}

export const csvFiles = {
  solar: ['publications', 'materials', 'devices', 'layers', 'measurements', 'results', 'issues'],
  rag: ['publications', 'configurations', 'datasets', 'tasks', 'experiments', 'results', 'issues'],
} as const;
