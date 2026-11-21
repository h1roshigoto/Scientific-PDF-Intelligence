import { useCallback } from 'react';
import type { ReactNode } from 'react';
import { Feedback, LoadBoundary } from '../components/Feedback';
import { InfoList } from '../components/InfoList';
import { PdfPane } from '../components/PdfPane';
import { ScientificReview, SolarStructure, solarStructureField } from '../components/ReviewSection';
import { PageHeading } from '../components/Shell';
import { StatusBadge } from '../components/Badge';
import { contextLabel, contextText, formatValue, publicationId, sourceLabel } from '../domain';
import type { Filters, Result } from '../domain';
import { useData } from '../hooks/useData';
import { Link, resultUrl, sectionUrl } from '../navigation';
import { provider } from '../provider';

const metricDescriptions: Record<string, string> = {
  PCE: 'эффективность преобразования',
  Voc: 'напряжение холостого хода',
  EM: 'точное совпадение ответа',
  F1: 'мера F1',
};

export function metricTitle(result: Result) {
  const { name, k, variant } = result.metric;
  return [name + (k === null ? '' : `@${k}`), variant, metricDescriptions[name]]
    .filter(Boolean)
    .join(' · ');
}

export function QuotedFragment({ text, found }: { text: string; found: string }) {
  const at = found ? text.indexOf(found) : -1;
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark>{found}</mark>
      {text.slice(at + found.length)}
    </>
  );
}

export function ResultEvidence({ result, children }: { result: Result; children?: ReactNode }) {
  const { source } = result;
  return (
    <section className="surface inspector">
      <div className="inspector-title-row">
        <h2>Первоисточник</h2>
        <StatusBadge status={result.extraction_status} />
      </div>
      <dl className="result-values">
        <div>
          <dt>Исходное значение</dt>
          <dd className="num" data-testid="raw-value">
            {formatValue(result.raw, true)}
          </dd>
        </div>
        <div>
          <dt>Нормализованное</dt>
          <dd className="num" data-testid="normalized-value">
            {formatValue(result.normalized)}
          </dd>
        </div>
      </dl>
      <p className="evidence-location">
        <strong>
          {publicationId(result.document_id)} · {sourceLabel(source)}
        </strong>
      </p>
      {(source.row || source.column) && (
        <p className="muted evidence-position">
          {[source.row && `Строка ${source.row}`, source.column && `Столбец ${source.column}`]
            .filter(Boolean)
            .join(' · ')}
        </p>
      )}
      {source.fragment && (
        <blockquote key={result.result_id}>
          <QuotedFragment text={source.fragment} found={String(result.raw.value ?? '')} />
        </blockquote>
      )}
      {result.note && <p className="muted evidence-note">{result.note}</p>}
      {children && <div className="inspector-actions">{children}</div>}
    </section>
  );
}

export function ResultDetail({ result, filters }: { result: Result; filters: Filters }) {
  const { source } = result;
  const info: [string, ReactNode][] = [
    ['Публикация', publicationId(result.document_id)],
    ...Object.entries(result.context)
      .filter(([key, value]) => result.domain !== 'solar' || !solarStructureField(key, value))
      .map(([key, value]): [string, ReactNode] => [contextLabel(key), contextText(value)]),
  ];
  if (result.metric.k !== null) info.push(['metric_k показателя', result.metric.k]);
  if (source.origin) {
    info.push([
      'Происхождение',
      source.origin === 'primary'
        ? 'Первичный результат'
        : `Цитируемый результат${source.cited_publication_id ? ` · ${source.cited_publication_id}` : ''}`,
    ]);
  }
  info.push(['Источник', [sourceLabel(source), source.row, source.column].filter(Boolean).join(' · ')]);

  return (
    <>
      <PageHeading
        title={publicationId(result.document_id)}
        subtitle={metricTitle(result)}
        back={{ href: sectionUrl('results', filters, result.result_id), label: 'К результатам' }}
      />
      <div className="split detail-layout result-detail-layout">
        <div className="stack">
          <ResultEvidence result={result}>
            <Link className="button primary" href={resultUrl(result.result_id, filters, true)}>
              Открыть страницу PDF
            </Link>
          </ResultEvidence>
          <section className="surface">
            <header className="section-bar">
              <h2>Связанные данные</h2>
              <span className="count-label">Контекст записи</span>
            </header>
            <InfoList items={info} />
          </section>
          <SolarStructure result={result} />
        </div>
        <div className="stack">
          <PdfPane
            key={result.result_id}
            domain={result.domain}
            documentId={result.document_id}
            initialPage={source.page}
            label={`${publicationId(result.document_id)}.pdf`}
          />
          <ScientificReview
            documentId={result.document_id}
            recordId={result.result_id}
          />
        </div>
      </div>
    </>
  );
}

export function ResultRoute({
  id,
  source,
  filters,
}: {
  id: string;
  source: boolean;
  filters: Filters;
}) {
  const load = useCallback((signal: AbortSignal) => provider.result(undefined, id, signal), [id]);
  const data = useData(load);
  return (
    <LoadBoundary {...data}>
      {(result) => {
        if (!source) return <ResultDetail result={result} filters={filters} />;
        const label = `${publicationId(result.document_id)}.pdf`;
        return (
          <>
            <PageHeading
              title={label}
              subtitle={`Источник записи · ${sourceLabel(result.source)}`}
              back={{ href: resultUrl(id, filters), label: 'Назад к записи' }}
            />
            <div className="split detail-layout">
              <PdfPane
                key={id}
                domain={result.domain}
                documentId={result.document_id}
                initialPage={result.source.page}
                label={label}
              />
              <ResultEvidence result={result} />
            </div>
          </>
        );
      }}
    </LoadBoundary>
  );
}

export function NotFoundPage() {
  return (
    <div className="not-found">
      <PageHeading
        title="Страница не найдена"
        subtitle="Выберите раздел в меню или вернитесь к результатам."
      />
      <Feedback kind="empty" title="Маршрут не существует">
        Проверьте адрес страницы или перейдите к таблице результатов.
      </Feedback>
      <div className="not-found-actions">
        <Link className="button primary" href={sectionUrl('results')}>
          К результатам
        </Link>
      </div>
    </div>
  );
}
