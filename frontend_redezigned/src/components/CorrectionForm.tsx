import { useState } from 'react';
import type { FormEvent } from 'react';
import { contextLabel, contextText } from '../domain';
import type { Evidence, Result } from '../domain';
import { evidenceLabels } from '../upload-review';
import type { Correction } from '../upload-review';

const evidenceFields = ['fragment', 'table', 'figure', 'section', 'row', 'column', 'cited_publication_id'] as const;
const contextFields = {
  solar: ['device_label', 'device_id', 'measurement_id', 'measurement_type', 'measurement_instrument',
    'scan_direction', 'spectrum', 'light_intensity', 'intensity_unit', 'temperature', 'stress_conditions'],
  rag: ['dataset_name', 'rag_variant', 'generator_model', 'task_type', 'top_k', 'evaluation_protocol', 'metric_definition'],
};
const same = (first: unknown, second: unknown) => JSON.stringify(first) === JSON.stringify(second);
const display = (value: unknown) => value === undefined || value === null ? '—' : contextText(value);

function scientificFields(result: Result): Record<string, { label: string; value: unknown }> {
  return {
    metric: { label: 'Метрика', value: result.metric.name },
    metric_k: { label: 'metric_k', value: result.metric.k },
    raw: { label: 'Исходное значение', value: result.raw.value },
    raw_unit: { label: 'Исходная единица', value: result.raw.unit },
    normalized: { label: 'Нормализованное значение', value: result.normalized.value },
    normalized_unit: { label: 'Нормализованная единица', value: result.normalized.unit },
    ...Object.fromEntries(Object.entries(result.source).map(([key, value]) =>
      [`source.${key}`, { label: evidenceLabels[key as keyof Evidence], value }])),
    ...Object.fromEntries(Object.entries(result.context).map(([key, value]) =>
      [`context.${key}`, { label: `Контекст · ${contextLabel(key)}`, value }])),
  };
}

export function ReviewChanges({ original, changed }: { original: Result; changed: Result }) {
  const before = scientificFields(original);
  const after = scientificFields(changed);
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .filter((key) => !same(before[key]?.value, after[key]?.value));
  if (!fields.length) return null;
  return (
    <div className="review-comparison">
      <h4>Было → Стало</h4>
      <dl>{fields.map((key) => <div key={key}>
        <dt>{(after[key] ?? before[key]).label}</dt>
        <dd><del>{display(before[key]?.value)}</del> → <ins>{display(after[key]?.value)}</ins></dd>
      </div>)}</dl>
    </div>
  );
}

function readCorrection(form: HTMLFormElement, original: Result, initial: Result, contextInputs: string[]) {
  const fields = new FormData(form);
  const read = (name: string) => String(fields.get(name) ?? '');
  const text = (name: string) => read(name).trim() || null;
  const value = (name: string, previous: Result['raw']['value']) =>
    read(name) === String(previous ?? '') ? previous : text(name);
  const context: unknown = JSON.parse(read('context'));
  if (!context || typeof context !== 'object' || Array.isArray(context)) throw new Error('context');
  const draftContext = { ...context } as Record<string, unknown>;
  for (const key of contextInputs) {
    const previous = initial.context[key];
    const field = `context.${key}`;
    const input = text(field);
    delete draftContext[key];
    if (read(field) === String(previous ?? '')) {
      if (Object.hasOwn(initial.context, key)) draftContext[key] = previous;
    } else {
      draftContext[key] = key === 'top_k' && input !== null ? Number(input) : input;
    }
  }
  const contextChanges = Object.fromEntries(Object.entries(draftContext)
    .filter(([key, value]) => !same(original.context[key], value)));
  const source = {
    page: Number(read('page')), origin: text('origin'),
    ...Object.fromEntries(evidenceFields.map((field) => [field, text(field)])),
  } as Evidence;
  const metric = { name: read('metric').trim(), k: text('metric_k') === null ? null : Number(read('metric_k')) };
  const raw = { value: value('raw', initial.raw.value), unit: text('raw_unit') };
  const normalized = { value: value('normalized', initial.normalized.value), unit: text('normalized_unit') };
  const changed = { ...original, raw, normalized, metric: { ...original.metric, ...metric }, source,
    context: { ...original.context, ...contextChanges } };
  const correction: Correction = {};
  if (!same(original.raw, raw)) correction.raw = raw;
  if (!same(original.normalized, normalized)) correction.normalized = normalized;
  if (!same(original.metric, changed.metric)) correction.metric = metric;
  if (Object.keys(contextChanges).length) correction.context = contextChanges;
  if (Object.keys(source).some((key) => source[key as keyof Evidence] !== original.source[key as keyof Evidence])) {
    correction.source = source;
  }
  return { correction, changed, normalization: text('normalization_evidence'), reason: read('reason').trim() };
}

export function CorrectionForm({ original, initial, normalizationEvidence, pageCount, saving, onCancel, onPropose }: {
  original: Result; initial: Result; normalizationEvidence?: string | null; pageCount: number; saving: boolean;
  onCancel: () => void; onPropose: (correction: Correction, reason: string) => void;
}) {
  const [preview, setPreview] = useState<Result>();
  const [error, setError] = useState('');
  const contextInputs = contextFields[original.domain].filter((key) => initial.context[key] === null
    || typeof initial.context[key] !== 'object');
  const extraContext = Object.fromEntries(Object.entries(initial.context).filter(([key]) => !contextInputs.includes(key)));

  function previewFields(form: HTMLFormElement) {
    try {
      const data = readCorrection(form, original, initial, contextInputs);
      setPreview(data.changed);
      setError('');
      return data;
    } catch {
      setPreview(undefined);
      setError('Контекст должен быть объектом JSON. Проверьте формат и повторите попытку.');
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = previewFields(event.currentTarget);
    if (!data) return;
    if (!Object.keys(data.correction).length) return setError('Измените хотя бы одно научное поле.');
    if (!data.reason) return setError('Обоснуйте исправление по PDF.');
    if (data.normalization) data.correction.normalization_evidence = data.normalization;
    onPropose(data.correction, data.reason);
  }

  return (
    <form className="review-form" onSubmit={submit} onChange={(event) => previewFields(event.currentTarget)}>
      <fieldset disabled={saving}>
        <legend>Предложение исправления</legend>
        <div className="review-fields">
          <label>Метрика<input name="metric" defaultValue={initial.metric.name} maxLength={4000} required /></label>
          <label>metric_k<input name="metric_k" type="number" min="0" step="1" defaultValue={initial.metric.k ?? ''} /></label>
          <label>Исходное значение<input name="raw" defaultValue={initial.raw.value ?? ''} maxLength={4000} required /></label>
          <label>Исходная единица<input name="raw_unit" defaultValue={initial.raw.unit ?? ''} maxLength={4000} /></label>
          <label>Нормализованное значение<input name="normalized" inputMode="decimal" defaultValue={initial.normalized.value ?? ''} maxLength={4000} /></label>
          <label>Нормализованная единица<input name="normalized_unit" defaultValue={initial.normalized.unit ?? ''} maxLength={4000} /></label>
        </div>
        <p className="muted">Оставьте неизвестные единицы пустыми. Если нормализацию нельзя обосновать по PDF, оставьте её поля пустыми.</p>
        <label>Основание нормализации<textarea name="normalization_evidence" rows={2} maxLength={4000}
          defaultValue={normalizationEvidence ?? ''} placeholder="Единица, протокол или определение метрики и место в PDF" /></label>
        <details open>
          <summary>Источник и evidence</summary>
          <div className="review-fields">
            <label>Страница PDF<input name="page" type="number" min="1" max={pageCount} step="1" defaultValue={initial.source.page} required /></label>
            <label>Происхождение<select name="origin" defaultValue={initial.source.origin ?? ''}>
              <option value="">Неизвестно</option><option value="primary">Первичный результат</option><option value="cited">Цитируемый результат</option>
            </select></label>
            {evidenceFields.map((field) => <label key={field}>{evidenceLabels[field]}
              <input name={field} defaultValue={initial.source[field] ?? ''} maxLength={4000} />
            </label>)}
          </div>
        </details>
        <details>
          <summary>Научный контекст</summary>
          <div className="review-fields">{contextInputs.map((key) => <label key={key}>{contextLabel(key)}
            <input name={`context.${key}`} type={key === 'top_k' ? 'number' : 'text'} min={key === 'top_k' ? 0 : undefined}
              step={key === 'top_k' ? 1 : undefined} defaultValue={String(initial.context[key] ?? '')} />
          </label>)}</div>
          <details>
            <summary>Дополнительные поля</summary>
            <label>Дополнительный контекст (JSON)<textarea name="context" rows={7} spellCheck={false}
              defaultValue={JSON.stringify(extraContext, null, 2)} required /></label>
            <p className="muted">Для неизвестного значения укажите null.</p>
          </details>
        </details>
        <label>Обоснование исправления<textarea name="reason" rows={3} maxLength={4000} required
          placeholder="Укажите, что подтверждает изменение и где это видно на указанной странице" /></label>
        {preview && <ReviewChanges original={original} changed={preview} />}
        {error && <p role="alert">{error}</p>}
        <div className="upload-actions">
          <button className="button primary" type="submit">Сохранить предложение</button>
          <button className="button quiet" type="button" onClick={onCancel}>Отмена</button>
        </div>
      </fieldset>
    </form>
  );
}
