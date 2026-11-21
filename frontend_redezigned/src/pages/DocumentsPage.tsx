import { useCallback } from 'react';
import { Badge } from '../components/Badge';
import { Feedback, LoadBoundary } from '../components/Feedback';
import { InfoList } from '../components/InfoList';
import { Note } from '../components/Note';
import { PdfPane } from '../components/PdfPane';
import { ScientificReview } from '../components/ReviewSection';
import { SearchInput } from '../components/SearchInput';
import { PageHeading, WorkspacePage } from '../components/Shell';
import { DataError } from '../data';
import { emptyFilters, publicationId } from '../domain';
import { useData } from '../hooks/useData';
import { useReviewIssues } from '../hooks/useReviewIssues';
import { documentSourceUrl, Link, navigate, sectionUrl } from '../navigation';
import type { Section } from '../navigation';
import { provider } from '../provider';
import { publicationDecision, publicationScope } from '../review';

export function DocumentsPage({ query }: { query: string }) {
  const load = useCallback((signal: AbortSignal) => provider.documents(undefined, signal), []);
  const data = useData(load);
  const { state: issueState } = useReviewIssues();
  const search = query.trim().toLocaleLowerCase('ru');

  return (
    <WorkspacePage
      title="Документы"
      subtitle="Реестр PDF, готовность страниц и очередь распознавания."
    >
      <div className="toolbar short">
        <SearchInput
          value={query}
          update={(value) => {
            navigate(
              sectionUrl('documents', { ...emptyFilters, query: value }),
              { replace: true },
            );
          }}
        />
      </div>
      <LoadBoundary {...data}>
        {(documents) => {
          const filtered = documents.filter((doc) =>
            [doc.document_id, doc.filename, doc.title, doc.doi, doc.DOI]
              .join(' ')
              .toLocaleLowerCase('ru')
              .includes(search),
          );
          if (!filtered.length) {
            return (
              <Feedback
                kind="empty"
                title={search ? 'Документы не найдены' : 'В реестре пока нет документов'}
              />
            );
          }
          const ocrCount = filtered.reduce((total, doc) => total + doc.pages_needing_ocr.length, 0);
          return (
            <div className="split">
              <div className="stack">
                <section className="surface" aria-labelledby="registry-heading">
                  <header className="section-bar">
                    <h2 id="registry-heading">Реестр PDF</h2>
                    <span className="count-label num">
                      Документов: <strong>{filtered.length}</strong>
                    </span>
                  </header>
                  <table className="data-table documents-table">
                    <thead>
                      <tr>
                        <th scope="col">Документ</th>
                        <th scope="col">Публикация</th>
                        <th scope="col" className="num">Страниц</th>
                        <th scope="col">Состояние</th>
                        <th scope="col">Источник</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((doc) => {
                        const scope =
                          issueState.kind === 'ready'
                            ? publicationScope(issueState.data, doc.document_id)
                            : null;
                        const decision =
                          issueState.kind === 'ready'
                            ? publicationDecision(issueState.data, doc.document_id)
                            : undefined;
                        const label =
                          doc.user_upload && doc.filename
                            ? doc.filename
                            : publicationId(doc.document_id);
                        const track = doc.domain === 'solar' ? 'Solar' : 'RAG';
                        return (
                          <tr key={doc.document_id}>
                            <td data-label="Документ">
                              <strong>{doc.user_upload ? 'Загруженный PDF' : `${track} · ${label}`}</strong>
                            </td>
                            <td data-label="Публикация">{doc.title ?? doc.filename ?? '—'}</td>
                            <td data-label="Страниц" className="num">
                              {doc.page_count ?? '—'}
                            </td>
                            <td data-label="Состояние">
                              <div className="status-cell">
                                {doc.pages_needing_ocr.length ? (
                                  <Badge tone="warn">OCR: {doc.pages_needing_ocr.length} стр.</Badge>
                                ) : doc.processing_status === 'parsed_text' ? (
                                  <Badge tone="info">Текст прочитан</Badge>
                                ) : (
                                  <Badge>{doc.processing_status ?? 'Не указано'}</Badge>
                                )}
                                {scope && decision && (
                                  <Link
                                    className={`issue-tag ${scope.tone}`}
                                    href={sectionUrl('checks', undefined, decision.issue_id)}
                                  >
                                    {scope.label}
                                  </Link>
                                )}
                              </div>
                              {scope && decision && (
                                <p className="cell-note muted">{decision.reason}</p>
                              )}
                            </td>
                            <td data-label="Источник">
                              <Link
                                href={documentSourceUrl('documents', doc.document_id, 1)}
                                aria-label={`Открыть PDF ${doc.user_upload ? label : `${track} ${label}`}`}
                              >
                                Открыть PDF
                              </Link>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </section>
                <Note title="Проверка источников.">
                  Состояние обработки страницы и статус научной записи хранятся отдельно.
                </Note>
              </div>
              <section className="surface inspector inspector-sticky">
                <h2>Состояние страниц</h2>
                <InfoList
                  items={[
                    ['Документов в очереди OCR', filtered.filter((doc) => doc.pages_needing_ocr.length > 0).length],
                    ['Документы', filtered.length],
                    ['Страниц в очереди OCR', ocrCount],
                  ]}
                />
              </section>
            </div>
          );
        }}
      </LoadBoundary>
    </WorkspacePage>
  );
}

export function DocumentSource({
  section,
  documentId,
  page,
  selected,
}: {
  section: Section;
  documentId: string;
  page: number;
  selected: string | null;
}) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const docs = await provider.documents(undefined, signal);
      const doc = docs.find((entry) => entry.document_id === documentId);
      if (!doc) throw new DataError('Документ отсутствует в реестре.');
      return doc;
    },
    [documentId],
  );
  const data = useData(load);

  return (
    <LoadBoundary {...data}>
      {(doc) => {
        const label = doc.filename ?? `${publicationId(doc.document_id)}.pdf`;
        return (
          <>
            <PageHeading
              title={label}
              subtitle={doc.title ?? 'Просмотр источника'}
              back={{
                href: sectionUrl(section, undefined, selected),
                label: section === 'checks' ? 'К проверкам' : 'К документам',
              }}
            />
            <div className="split detail-layout">
              <PdfPane
                key={`${documentId}:${page}`}
                domain={doc.domain}
                documentId={documentId}
                initialPage={page}
                label={label}
                highlightsResult={false}
              />
              <div className="stack">
                <section className="surface inspector">
                  <h2>Документ</h2>
                  <InfoList
                    items={[
                      ['Состояние', doc.pages_needing_ocr.length ? 'В очереди OCR'
                        : doc.processing_status === 'parsed_text' ? 'Текст прочитан' : doc.processing_status ?? 'Не указано'],
                      ['Публикация', publicationId(doc.document_id)],
                      ['Страниц', doc.page_count ?? '—'],
                      ['DOI', doc.doi ?? doc.DOI ?? '—'],
                    ]}
                  />
                </section>
                {section === 'documents' && (
                  <ScientificReview documentId={doc.document_id} />
                )}
              </div>
            </div>
          </>
        );
      }}
    </LoadBoundary>
  );
}
