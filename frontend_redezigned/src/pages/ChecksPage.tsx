import { useCallback } from 'react';
import { Badge } from '../components/Badge';
import { Feedback, LoadBoundary } from '../components/Feedback';
import { Icon } from '../components/Icon';
import { InfoList } from '../components/InfoList';
import { ReviewSources } from '../components/ReviewSection';
import { WorkspacePage } from '../components/Shell';
import { publicationId, sourceLabel } from '../domain';
import type { Issue, Result } from '../domain';
import { useData } from '../hooks/useData';
import { Link, resultUrl, sectionUrl } from '../navigation';
import { provider } from '../provider';
import { checkLabel, checkValues, evidenceLinks, publicationScope } from '../review';

const severityLabel = { info: 'Информация', warning: 'Нужна проверка', error: 'Ошибка' } as const;

function IssueDetails({ issue, rows }: { issue: Issue; rows: Result[] }) {
  const related = issue.result_ids.map((id) => ({
    id,
    result: rows.find((row) => row.result_id === id),
  }));
  const references = evidenceLinks(issue);
  const scope = references[0] && publicationScope([issue], references[0].documentId);
  const badgeTone = scope?.tone ?? (issue.severity === 'warning' ? 'warn' : issue.severity);

  return (
    <section className="surface inspector issue-inspector" aria-labelledby="issue-title">
      <div className="inspector-title-row">
        <h2 id="issue-title">Причина замечания</h2>
        <Badge tone={badgeTone}>{scope?.label ?? severityLabel[issue.severity]}</Badge>
      </div>
      <p className="issue-reason">{issue.reason}</p>
      <InfoList
        items={[
          ['Правило', checkLabel(issue)],
          ...checkValues(issue).filter(([label]) => !scope || label !== 'Наблюдение'),
        ]}
      />
      {(related.length > 0 || references.length > 0) && (
        <div className="issue-sources">
          <h3>Связанные записи и источники</h3>
          {related.map(({ id, result }) => (
            <div key={id} className="issue-source">
              <Link href={resultUrl(id)}>
                {result ? `${publicationId(result.document_id)} · ${result.metric.name}` : id}
              </Link>
              {result && (
                <Link href={resultUrl(id, undefined, true)}>
                  {sourceLabel(result.source)}
                </Link>
              )}
            </div>
          ))}
          <ReviewSources issue={issue} />
        </div>
      )}
    </section>
  );
}

export function ChecksPage({ selected }: { selected: string | null }) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const [issues, rows] = await Promise.all([
        provider.issues(undefined, signal),
        provider.results(undefined, signal),
      ]);
      return { issues, rows };
    },
    [],
  );
  const data = useData(load);

  return (
    <WorkspacePage
      title="Проверки"
      subtitle="Замечания, причины расхождений и ссылки на первоисточники."
    >
      <LoadBoundary {...data}>
        {({ issues, rows }) => {
          const chosen = issues.find((issue) => issue.issue_id === selected) ?? issues[0];
          if (!chosen) {
            const unverified = rows.some((row) => row.extraction_status !== 'verified');
            return (
              <Feedback kind="empty" title={rows.length ? 'Опубликованных замечаний нет' : 'Пока нет сведений о проверках'}>
                {unverified ? 'Для части результатов сверка с источником ещё не подтверждена.'
                  : !rows.length ? 'Проверки появятся после обработки документов.' : undefined}
              </Feedback>
            );
          }
          return (
            <div className="split checks-layout">
              <section className="surface" aria-labelledby="checks-heading">
                <header className="section-bar">
                  <h2 id="checks-heading">Очередь проверки</h2>
                  <span className="count-label num">
                    Замечаний: <strong>{issues.length}</strong>
                  </span>
                </header>
                <ul className="review-list">
                  {issues.map((issue) => (
                    <li key={issue.issue_id}>
                      <Link
                        href={sectionUrl('checks', undefined, issue.issue_id)}
                        aria-current={chosen.issue_id === issue.issue_id ? 'true' : undefined}
                      >
                        <span
                          className={`severity-dot ${issue.severity === 'warning' ? 'warn' : issue.severity}`}
                          aria-hidden="true"
                        />
                        <span className="review-item-text">
                          <strong>{checkLabel(issue)}</strong>
                          <small>{issue.reason}</small>
                        </span>
                        <Icon name="next" size={14} />
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
              <IssueDetails issue={chosen} rows={rows} />
            </div>
          );
        }}
      </LoadBoundary>
    </WorkspacePage>
  );
}
