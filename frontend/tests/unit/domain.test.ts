import { describe, expect, it } from 'vitest';
import { documentSchema, emptyFilters, filterResults, formatValue, pdfPageUrl, resultSchema, selectResult } from '../../src/domain';
import { activeDomain, getFilters, parseRoute, resultUrl } from '../../src/navigation';
import fixture from '../../fixtures/demo.json';

const rows = fixture.results.map((row) => resultSchema.parse(row));
const docs = fixture.documents.map((row) => documentSchema.parse(row));
const solar = rows.filter((row) => row.domain === 'solar');
const url = (href: string) => new URL(href, 'http://localhost');

describe('result display and selection', () => {
  it('preserves source precision, formats normalization separately and retains zero', () => {
    expect(formatValue({ value: '1.200', unit: 'V' }, true)).toBe('1.200 V');
    expect(formatValue({ value: 1.2, unit: 'В' })).toBe('1,2 В');
    expect(formatValue({ value: 0, unit: null })).toBe('0');
    expect(formatValue({ value: null, unit: 'V' })).toBe('—');
    expect(formatValue({ value: '', unit: null })).toBe('—');
  });

  it('validates a required raw value, 1-based evidence and document scope', () => {
    const original = fixture.results[1];
    expect(resultSchema.parse(original).normalized.value).toBe(1.2);
    expect(resultSchema.safeParse({ ...original, raw: {} }).success).toBe(false);
    expect(resultSchema.safeParse({ ...original, source: { page: 0, table: 'Table 1' } }).success).toBe(false);
    expect(resultSchema.safeParse({ ...original, source: { page: 5 } }).success).toBe(false);
    expect(resultSchema.safeParse({ ...original, document_id: 'rag:D001' }).success).toBe(false);
    expect(resultSchema.parse(fixture.results[5]).normalized).toEqual({ value: null, unit: null });
  });

  it('filters by title, identifiers, DOI, subject, source, status and metric', () => {
    expect(filterResults(solar, docs, { ...emptyFilters, query: 'тонкоплёночный', metric: 'Voc' })).toEqual([solar[1]]);
    const withDoi = docs.map((entry) => entry.document_id === 'solar:D001' ? { ...entry, DOI: '10.0000/example' } : entry);
    expect(filterResults(solar, withDoi, { ...emptyFilters, query: '10.0000/EXAMPLE' })).toEqual(solar.slice(0, 2));
    expect(filterResults(solar, docs, { query: 'D021', subject: 'B4', status: 'needs_review', metric: 'PCE', source: 'стр. 13 · Fig. 10' })).toEqual([solar[3]]);
    expect(filterResults(solar, docs, { ...emptyFilters, subject: 'HotpotQA' })).toEqual([]);
  });

  it('uses only the current list when selecting a record', () => {
    expect(selectResult(solar, solar[1].result_id)).toEqual(solar[1]);
    expect(selectResult(solar, 'demo-rag-d001-em')).toEqual(solar[0]);
    expect(selectResult([], 'demo-rag-d001-em')).toBeNull();
  });

  it('preserves 1-based PDF pages and replaces existing hash fragments', () => {
    expect(pdfPageUrl('/d001.pdf#zoom=100', 5)).toBe('/d001.pdf#page=5');
    expect(pdfPageUrl('blob:local-id', 6)).toBe('blob:local-id#page=6');
    expect(() => pdfPageUrl('/d001.pdf', 0)).toThrow();
  });
});

describe('routing', () => {
  it('loads the requested category and defaults when query is invalid', () => {
    expect(activeDomain(url('/results?domain=rag').searchParams)).toBe('rag');
    expect(activeDomain(url('/results?domain=other').searchParams)).toBe('solar');
  });

  it('round trips a deep link and its filters with URL encoding', () => {
    const filters = { ...emptyFilters, query: 'D001 & 1', metric: 'EM' };
    const href = url(resultUrl('rag', 'RAG:D 001', filters, true));
    expect(parseRoute(href)).toEqual({ section: 'results', view: 'pdf', id: 'RAG:D 001' });
    expect(getFilters(href.searchParams)).toEqual(filters);
    expect(parseRoute(url('/invalid')).view).toBe('not-found');
    expect(parseRoute(url('/documents/rag:D001/source?page=0')).view).toBe('not-found');
  });
});
