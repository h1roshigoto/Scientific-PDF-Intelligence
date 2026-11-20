import { useCallback } from 'react';
import type { ReactNode } from 'react';
import { contextLabels, contextText, domainName, emptyFilters, filterResults, formatValue, publicationId, selectResult, sourceLabel, statusLabel, subject } from './domain';
import type { Domain, Filters, Result } from './domain';
import { Link, navigate, resultUrl, sectionUrl } from './navigation';
import { provider } from './provider';
import { Feedback, Icon, LoadBoundary, PageHeading, Ribbon, StatusBadge, TrackTabs, useData } from './ui';

const metricDescriptions: Record<string, string> = {
  PCE: 'эффективность преобразования', Voc: 'напряжение холостого хода', EM: 'точное совпадение ответа', F1: 'мера F1',
};

export function metricTitle(result: Result) {
  const { name, k, variant } = result.metric;
  return [name + (k === null ? '' : `@${k}`), variant, metricDescriptions[name]].filter(Boolean).join(' · ');
}

export function SearchInput({ value, update }: { value: string; update: (value: string) => void }) {
  return <label className="search-field"><Icon name="search" size={17} />
    <input type="search" aria-label="Поиск по названию, DOI или ID" placeholder="Название статьи или DOI" value={value} onChange={(event) => update(event.target.value)} />
  </label>;
}

function FilterSelect({ label, value, choices, onChange }: {
  label: string; value: string; choices: { value: string; label: string }[]; onChange: (value: string) => void;
}) {
  const options = value && !choices.some((choice) => choice.value === value) ? [...choices, { value, label: value }] : choices;
  return <label className="select-field"><Icon name="filter" size={16} />
    <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">{label === 'Источник' ? label : `${label}: все`}</option>
      {options.map((entry) => <option key={entry.value} value={entry.value}>{entry.label}</option>)}
    </select>
  </label>;
}

function options(values: string[]) {
  return [...new Set(values.filter((entry) => entry !== '—'))].sort().map((value) => ({ value, label: value }));
}

function ResultsTable({ rows, selected, onSelect }: { rows: Result[]; selected: Result | null; onSelect: (id: string) => void }) {
  return <section className="panel result-list" aria-labelledby="results-list-title">
    <header className="panel-heading"><h2 id="results-list-title">Найденные результаты</h2>
      <p aria-live="polite">{provider.mode === 'mock' ? 'Записей в демонстрационной выборке' : 'Найдено записей'}: {rows.length}</p></header>
    <table className="data-table results-table">
      <thead><tr>{['Документ', 'Метрика', 'Исходное', 'Нормализация', 'Контекст', 'Источник', 'Статус'].map((label) => <th key={label} scope="col">{label}</th>)}</tr></thead>
      <tbody>{rows.map((row) => <tr key={row.result_id} className={row.result_id === selected?.result_id ? 'selected' : undefined} onClick={() => onSelect(row.result_id)}>
        <td data-label="Документ"><button className="row-trigger" type="button" aria-label={`${publicationId(row.document_id)} · ${row.metric.name} · ${subject(row)} · ${sourceLabel(row.source)}`}
          aria-pressed={row.result_id === selected?.result_id} aria-controls="result-inspector" onClick={(event) => { event.stopPropagation(); onSelect(row.result_id); }}>{publicationId(row.document_id)}</button></td>
        <td data-label="Метрика"><strong>{row.metric.name}{row.metric.k === null ? '' : `@${row.metric.k}`}</strong></td>
        <td data-label="Исходное">{formatValue(row.raw, true)}</td>
        <td data-label="Нормализация" data-testid="normalized-cell">{formatValue(row.normalized)}</td>
        <td data-label="Контекст">{subject(row)}</td>
        <td data-label="Источник">{sourceLabel(row.source)}</td>
        <td data-label="Статус"><StatusBadge status={row.extraction_status} /></td>
      </tr>)}</tbody>
    </table>
  </section>;
}

function QuickInspector({ result, filters }: { result: Result; filters: Filters }) {
  return <section className="panel inspector" id="result-inspector" tabIndex={-1} aria-labelledby="inspector-title">
    <div className="inspector-header"><h2 id="inspector-title">Первоисточник</h2>
      <p>{domainName[result.domain]} / {publicationId(result.document_id)} · {sourceLabel(result.source)}</p></div>
    <div className="metric-display" data-testid="summary-value">{formatValue(result.normalized)}</div>
    <p className="muted">{metricTitle(result)}</p>
    <StatusBadge status={result.extraction_status} />
    <p className="summary-context">{subject(result)}{result.source.table ? ` · ${result.source.table}` : ''}</p>
    <p className="summary-source" data-testid="summary-raw">Исходное значение · {formatValue(result.raw, true)}</p>
    <div className="button-row">
      <Link className="button primary" href={resultUrl(result.domain, result.result_id, filters)}>Открыть детали</Link>
      <Link className="button" href={resultUrl(result.domain, result.result_id, filters, true)}>Страница PDF</Link>
    </div>
  </section>;
}

export function ResultsPage({ domain, filters, selectedId }: { domain: Domain; filters: Filters; selectedId: string | null }) {
  const load = useCallback(async (signal: AbortSignal) => {
    const [rows, documents] = await Promise.all([provider.results(domain, signal), provider.documents(domain, signal)]);
    return { rows, documents };
  }, [domain]);
  const data = useData(load);
  const filtering = Object.values(filters).some(Boolean);
  const change = (key: keyof Filters, value: string) => navigate(sectionUrl('results', domain, { ...filters, [key]: value }), { replace: true });
  const reset = () => navigate(sectionUrl('results', domain, emptyFilters), { replace: true });

  return <>
    <PageHeading title="Результаты" subtitle="Научные показатели с контекстом измерения и привязкой к страницам PDF." artwork />
    <TrackTabs section="results" domain={domain}>
      <LoadBoundary {...data}>{({ rows, documents }) => {
        const visible = filterResults(rows, documents, filters);
        const selected = selectResult(visible, selectedId);
        function select(id: string) {
          navigate(sectionUrl('results', domain, filters, id), { replace: true });
          if (window.matchMedia('(max-width: 850px)').matches) {
            requestAnimationFrame(() => document.getElementById('result-inspector')?.focus());
          }
        }
        return <>
          <div className="toolbar" aria-label="Фильтры результатов">
            <SearchInput value={filters.query} update={(value) => change('query', value)} />
            <FilterSelect label="Метрика" value={filters.metric} choices={options(rows.map((row) => row.metric.name))} onChange={(value) => change('metric', value)} />
            <FilterSelect label={domain === 'solar' ? 'Прибор' : 'Набор'} value={filters.subject} choices={options(rows.map(subject))} onChange={(value) => change('subject', value)} />
            <FilterSelect label="Статус" value={filters.status} choices={Object.entries(statusLabel).map(([value, label]) => ({ value, label }))} onChange={(value) => change('status', value)} />
            <FilterSelect label="Источник" value={filters.source} choices={options(rows.map((row) => sourceLabel(row.source)))} onChange={(value) => change('source', value)} />
            {filtering ? <button type="button" className="button quiet toolbar-action" onClick={reset}>Сбросить</button>
              : <Link className="button quiet toolbar-action" href={sectionUrl('export', domain)}>Экспорт</Link>}
          </div>
          {visible.length ? <div className="split">
            <div className="stack">
              <ResultsTable rows={visible} selected={selected} onSelect={select} />
              <Ribbon title="Сначала сравните контекст">{domain === 'solar' ? 'Условия, направление развёртки и тип агрегации могут различаться даже в одной статье.'
                : 'Сопоставимость зависит от задачи, набора, определения метрики и протокола оценки.'}</Ribbon>
            </div>
            {selected && <QuickInspector result={selected} filters={filters} />}
          </div> : <Feedback kind="empty" title={filtering ? 'По фильтрам ничего не найдено' : 'Для этого трека пока нет результатов'}>
            {filtering ? 'Измените параметры поиска или сбросьте фильтры.' : 'Результаты появятся после обработки документов.'}</Feedback>}
        </>;
      }}</LoadBoundary>
    </TrackTabs>
  </>;
}

export function InfoList({ items }: { items: [string, ReactNode][] }) {
  return <dl className="info-list">{items.map(([label, value], index) => <div key={`${label}-${index}`}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

export function ResultEvidence({ result, children }: { result: Result; children?: ReactNode }) {
  const { source } = result;
  return <section className="panel inspector">
    <h2>Первоисточник</h2><StatusBadge status={result.extraction_status} />
    <dl className="result-values">
      <div><dt>Исходное значение</dt><dd data-testid="raw-value">{formatValue(result.raw, true)}</dd></div>
      <div><dt>Нормализованное</dt><dd data-testid="normalized-value">{formatValue(result.normalized)}</dd></div>
    </dl>
    <p><strong>{domainName[result.domain]} / {publicationId(result.document_id)} · {sourceLabel(source)}</strong></p>
    {(source.row || source.column) && <p className="muted">{[source.row && `Строка ${source.row}`, source.column && `Столбец ${source.column}`].filter(Boolean).join(' · ')}</p>}
    {source.fragment && <blockquote>{source.fragment}</blockquote>}
    {result.note && <p className="muted">{result.note}</p>}
    {children}
  </section>;
}

export function ResultDetail({ result, filters }: { result: Result; filters: Filters }) {
  const { source } = result;
  const info: [string, ReactNode][] = [
    ['Публикация', `${domainName[result.domain]} / ${publicationId(result.document_id)}`],
    ...Object.entries(result.context).map(([key, value]): [string, ReactNode] => [contextLabels[key] ?? key, contextText(value)]),
  ];
  if (result.metric.k !== null) info.push(['metric_k показателя', result.metric.k]);
  if (source.origin) info.push(['Происхождение', source.origin === 'primary' ? 'Первичный результат' : `Цитируемый результат${source.cited_publication_id ? ` · ${source.cited_publication_id}` : ''}`]);
  info.push(['Источник', [sourceLabel(source), source.row, source.column].filter(Boolean).join(' · ')]);
  return <>
    <PageHeading title={`${domainName[result.domain]} / ${publicationId(result.document_id)}`} subtitle={metricTitle(result)} />
    <div className="action-line"><Link className="button" href={sectionUrl('results', result.domain, filters, result.result_id)}><Icon name="back" size={16} />К результатам</Link></div>
    <div className="split detail-layout">
      <section className="panel"><header className="panel-heading"><h2>Связанные данные</h2><p>Идентификаторы и условия конкретной записи</p></header>
        <InfoList items={info} /></section>
      <ResultEvidence result={result}><Link className="button primary" href={resultUrl(result.domain, result.result_id, filters, true)}>Открыть страницу PDF</Link></ResultEvidence>
    </div>
  </>;
}
