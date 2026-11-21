import { expect, it } from 'vitest';
import { checkLabel, checkValues, evidenceLinks, orderedLayers, publicationScope } from '../../src/review';
import { documentSchema, emptyFilters, filterResults, issueSchema, resultSchema } from '../../src/domain';
import seed from '../../fixtures/demo.json';

const issues = seed.issues.map((issue) => issueSchema.parse(issue));

it('does not infer publication eligibility from successful PCE arithmetic', () => {
  const formula = issues.find((issue) => issue.issue_id === 'demo-solar-d006-formula')!;
  expect(checkValues(formula)).toEqual([['Заявленный PCE', '20,00 %'], ['Расчётный PCE', '20,00 %']]);
  expect(publicationScope([formula], 'solar:D006')).toBeNull();
  expect(publicationScope(issues, 'solar:D006')?.label).toBe('Вне области кейса');
  expect(publicationScope(issues, 'rag:D006')).toBeNull();
});

it('preserves two pieces of evidence for the same result and rejects invalid source links', () => {
  const issue = issues.find((row) => row.rule_id === 'confirmed_duplicate')!;
  expect(issue.result_ids).toEqual(['demo-solar-d001-pce']);
  expect(evidenceLinks({ ...issue, evidence_refs: [...issue.evidence_refs, { document_id: 'solar:D001', page: 0 }, { document_id: 'rag:D001', page: 5 }] })
    .map(({ page }) => page)).toEqual([4, 5]);
});

it('keeps equal values from different devices and measurements as separate records', () => {
  const original = resultSchema.parse(seed.results[0]);
  const other = { ...original, result_id: 'other', context: { device_label: 'Device B', scan_direction: 'forward' } };
  const documents = seed.documents.map((doc) => documentSchema.parse(doc));
  expect(filterResults([original, other], documents, { ...emptyFilters, metric: 'PCE' }).map((row) => row.result_id))
    .toEqual([original.result_id, 'other']);
});

it('preserves distinct availability labels and displays unrecognised rule IDs', () => {
  const names = ['field_not_reported', 'field_extraction_failed', 'supplement_unavailable', 'field_undetermined'];
  const labels = issues.filter((issue) => names.includes(issue.rule_id)).map(checkLabel);
  expect(new Set(labels).size).toBe(names.length);
  expect(checkLabel({ ...issues[0], rule_id: 'constructor' })).toBe('constructor');
});

it('orders layer rows by an explicit position and leaves unknown order at the end', () => {
  const sample = [{ layer_order: 2 }, { layer_material: 'Unknown order' }, { layer_order: '1' }, null];
  expect(orderedLayers(sample)).toEqual([sample[2], sample[0], sample[1]]);
});
