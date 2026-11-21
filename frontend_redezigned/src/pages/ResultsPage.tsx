import { useCallback } from 'react';
import { StatusBadge } from '../components/Badge';
import { Feedback, LoadBoundary } from '../components/Feedback';
import { Note } from '../components/Note';
import { FilterSelect, SearchInput } from '../components/SearchInput';
import { WorkspacePage } from '../components/Shell';
import {
  emptyFilters,
  filterResults,
  formatValue,
  publicationId,
  resultConditions,
  selectResult,
  sourceLabel,
  statusLabel,
  subject,
} from '../domain';
import type { Filters, Issue, Result } from '../domain';
import { useData } from '../hooks/useData';
import { useReviewIssues } from '../hooks/useReviewIssues';
import { Link, navigate, resultUrl, sectionUrl } from '../navigation';
import { provider } from '../provider';
import { checkLabel, indexResultIssues } from '../review';
import { metricTitle, QuotedFragment } from './ResultDetailPage';

function uniqueOptions(values: string[]) {
  return [...new Set(values.filter((entry) => entry !== '—'))]
    .sort()
    .map((value) => ({ value, label: value }));
}

function ResultsTable({
  rows,
  selected,
  issues,
  onSelect,
}: {
  rows: Result[];
  selected: Result | null;
  issues: Map<string, Issue[]>;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="surface result-list" aria-labelledby="results-list-title">
      <header className="section-bar">
        <h2 id="results-list-title">Найденные результаты</h2>
        <span className="count-label num" aria-live="polite">
          Записей:{' '}
          <strong>{rows.length}</strong>
        </span>
      </header>
      <table className="data-table results-table">
        <thead>
          <tr>
            <th scope="col">Документ</th>
            <th scope="col">Метрика</th>
            <th scope="col" className="num">Исходное</th>
            <th scope="col" className="num">Нормализация</th>
            <th scope="col">Контекст</th>
            <th scope="col">Источник</th>
            <th scope="col">Статус и проверки</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const rowIssues = issues.get(row.result_id) ?? [];
            const isSelected = row.result_id === selected?.result_id;
            return (
              <tr
                key={row.result_id}
                className={isSelected ? 'selected' : undefined}
                onClick={() => onSelect(row.result_id)}
              >
                <td data-label="Документ">
                  <button
                    className="row-trigger"
                    type="button"
                    aria-label={`${publicationId(row.document_id)} · ${row.metric.name} · ${subject(row)} · ${sourceLabel(row.source)}`}
                    aria-pressed={isSelected}
                    aria-controls="result-inspector"
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect(row.result_id);
                    }}
                  >
                    {publicationId(row.document_id)}
                  </button>
                </td>
                <td data-label="Метрика">
                  <strong className="metric-code">
                    {row.metric.name}
                    {row.metric.k === null ? '' : `@${row.metric.k}`}
                  </strong>
                </td>
                <td data-label="Исходное" className="num">
                  {formatValue(row.raw, true)}
                </td>
                <td data-label="Нормализация" className="num strong" data-testid="normalized-cell">
                  {formatValue(row.normalized)}
                </td>
                <td data-label="Контекст">{subject(row)}</td>
                <td data-label="Источник" className="muted">
                  {sourceLabel(row.source)}
                </td>
                <td data-label="Статус">
                  <div className="status-cell">
                    <StatusBadge status={row.extraction_status} />
                    {rowIssues.length > 0 && (
                      <ul className="issue-tags" aria-label="Замечания">
                        {rowIssues.map((issue) => (
                          <li key={issue.issue_id}>
                            <Link
                              className={`issue-tag ${issue.severity === 'warning' ? 'warn' : issue.severity}`}
                              title={issue.reason}
                              href={sectionUrl('checks', undefined, issue.issue_id)}
                              onClick={(event) => event.stopPropagation()}
                            >
                              {checkLabel(issue)}
                            </Link>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function QuickInspector({ result, filters }: { result: Result; filters: Filters }) {
  return (
    <section
      className="surface inspector inspector-sticky"
      id="result-inspector"
      tabIndex={-1}
      aria-labelledby="inspector-title"
    >
      <div className="inspector-header">
        <div className="inspector-title-row">
          <h2 id="inspector-title">Первоисточник</h2>
          <StatusBadge status={result.extraction_status} />
        </div>
        <p className="inspector-meta">
          {publicationId(result.document_id)} · {sourceLabel(result.source)}
        </p>
      </div>
      <div className="metric-readout">
        <div className="metric-display num" data-testid="summary-value">
          {formatValue(result.normalized)}
        </div>
        <p className="muted">{metricTitle(result)}</p>
      </div>
      <dl className="inspector-facts">
        <div>
          <dt>Контекст</dt>
          <dd>
            {subject(result)}
            {result.source.table ? ` · ${result.source.table}` : ''}
          </dd>
        </div>
        <div>
          <dt>Исходное значение</dt>
          <dd className="num" data-testid="summary-raw">
            {formatValue(result.raw, true)}
          </dd>
        </div>
      </dl>
      {result.source.fragment && (
        <blockquote key={result.result_id}>
          <QuotedFragment text={result.source.fragment} found={String(result.raw.value ?? '')} />
        </blockquote>
      )}
      <div className="button-row">
        <Link className="button primary" href={resultUrl(result.result_id, filters)}>
          Открыть детали
        </Link>
        <Link className="button" href={resultUrl(result.result_id, filters, true)}>
          Страница PDF
        </Link>
      </div>
    </section>
  );
}

export function ResultsPage({
  filters,
  selectedId,
}: {
  filters: Filters;
  selectedId: string | null;
}) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const [rows, documents] = await Promise.all([
        provider.results(undefined, signal),
        provider.documents(undefined, signal),
      ]);
      return { rows, documents };
    },
    [],
  );
  const data = useData(load);
  const reviews = useReviewIssues();
  const filtering = Object.values(filters).some(Boolean);
  const change = (key: keyof Filters, value: string) =>
    navigate(sectionUrl('results', { ...filters, [key]: value }), { replace: true });
  const reset = () => navigate(sectionUrl('results', emptyFilters), { replace: true });

  return (
    <WorkspacePage
      title="Результаты"
      subtitle="Научные показатели с контекстом измерения и привязкой к страницам PDF."
    >
      <Link className="button upload-panel" href={sectionUrl('documents')}>Загрузить PDF</Link>
      <LoadBoundary {...data}>
        {({ rows, documents }) => {
          const issues = reviews.state.kind === 'ready' ? reviews.state.data : [];
          const byResult = indexResultIssues(rows, issues);
          const warningOptions = new Map(
            rows.flatMap((row) =>
              (byResult.get(row.result_id) ?? []).map(
                (issue) => [issue.rule_id, checkLabel(issue)] as const,
              ),
            ),
          );
          const visible = filterResults(rows, documents, filters, byResult);
          const selected = selectResult(visible, selectedId);

          function select(id: string) {
            navigate(sectionUrl('results', filters, id), { replace: true });
            if (window.matchMedia('(max-width: 850px)').matches) {
              requestAnimationFrame(() => document.getElementById('result-inspector')?.focus());
            }
          }

          return (
            <>
              <div className="toolbar" aria-label="Фильтры результатов">
                <SearchInput value={filters.query} update={(value) => change('query', value)} />
                <FilterSelect
                  label="Метрика"
                  value={filters.metric}
                  choices={uniqueOptions(rows.map((row) => row.metric.name))}
                  onChange={(value) => change('metric', value)}
                />
                <FilterSelect
                  label="Контекст"
                  value={filters.subject}
                  choices={uniqueOptions(rows.map(subject))}
                  onChange={(value) => change('subject', value)}
                />
                <FilterSelect
                  label="Статус"
                  value={filters.status}
                  choices={Object.entries(statusLabel).map(([value, label]) => ({ value, label }))}
                  onChange={(value) => change('status', value)}
                />
                <FilterSelect
                  label="Источник"
                  value={filters.source}
                  choices={uniqueOptions(rows.map((row) => sourceLabel(row.source)))}
                  onChange={(value) => change('source', value)}
                />
                {filtering ? (
                  <button type="button" className="button quiet toolbar-action" onClick={reset}>
                    Сбросить
                  </button>
                ) : (
                  <Link className="button quiet toolbar-action" href={sectionUrl('export')}>
                    Экспорт
                  </Link>
                )}
              </div>
              <details
                className="advanced-filters"
                open={filters.condition || filters.warning ? true : undefined}
              >
                <summary>Условия и замечания</summary>
                <div className="toolbar">
                  <FilterSelect
                    label="Условия"
                    value={filters.condition ?? ''}
                    choices={uniqueOptions(rows.flatMap(resultConditions))}
                    onChange={(value) => change('condition', value)}
                  />
                  <FilterSelect
                    label="Замечание"
                    value={filters.warning ?? ''}
                    choices={[...warningOptions].map(([value, label]) => ({ value, label }))}
                    onChange={(value) => change('warning', value)}
                  />
                  {reviews.state.kind === 'error' && (
                    <button className="button quiet" type="button" onClick={reviews.retry}>
                      Обновить замечания
                    </button>
                  )}
                </div>
              </details>
              {visible.length ? (
                <div className="split">
                  <div className="stack">
                    <ResultsTable rows={visible} selected={selected} issues={byResult} onSelect={select} />
                    <Note title="Сначала сравните контекст.">
                      Сопоставимость зависит от условий эксперимента, определения показателя и протокола оценки.
                    </Note>
                  </div>
                  {selected && <QuickInspector result={selected} filters={filters} />}
                </div>
              ) : (
                <Feedback
                  kind="empty"
                  title={
                    filtering ? 'По фильтрам ничего не найдено' : 'Пока нет результатов'
                  }
                >
                  {filtering
                    ? 'Измените параметры поиска или сбросьте фильтры.'
                    : 'Результаты появятся после обработки документов.'}
                </Feedback>
              )}
            </>
          );
        }}
      </LoadBoundary>
    </WorkspacePage>
  );
}
