import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from './data';
import { csvFiles, domainName, publicationId, sourceLabel } from './domain';
import type { Domain, Issue, Result } from './domain';
import { documentSourceUrl, Link, navigate, resultUrl, sectionUrl } from './navigation';
import { provider } from './provider';
import { checkLabel, checkValues, evidenceLinks, publicationScope } from './review';
import { ReviewSources, useReviewIssues } from './reviews';
import { SearchInput } from './results';
import { Badge, Feedback, Icon, InfoList, LoadBoundary, PageHeading, Ribbon, TrackTabs, useData } from './ui';
import passport from '../../passport.md?raw';

export function DocumentsPage({ domain, query }: { domain: Domain; query: string }) {
  const load = useCallback((signal: AbortSignal) => provider.documents(domain, signal), [domain]);
  const data = useData(load);
  const { state: issueState } = useReviewIssues(domain);
  const search = query.trim().toLocaleLowerCase('ru');
  return <>
    <PageHeading title="Документы" subtitle="Реестр PDF, готовность страниц и очередь распознавания." artwork />
    <TrackTabs section="documents" domain={domain}>
      <div className="toolbar short"><SearchInput value={query} update={(value) => {
        navigate(`/documents?${new URLSearchParams({ domain, ...(value ? { q: value } : {}) })}`, { replace: true });
      }} /></div>
      <LoadBoundary {...data}>{(documents) => {
        const filtered = documents.filter((doc) => [doc.document_id, doc.filename, doc.title, doc.doi, doc.DOI].join(' ').toLocaleLowerCase('ru').includes(search));
        if (!filtered.length) return <Feedback kind="empty" title={search ? 'Документы не найдены' : 'В реестре пока нет документов'} />;
        return <div className="split">
          <section className="panel" aria-labelledby="registry-heading">
            <header className="panel-heading"><h2 id="registry-heading">Реестр PDF</h2><p>Документов: {filtered.length}</p></header>
            <table className="data-table">
              <thead><tr>{['Документ', 'Публикация', 'Страниц', 'Состояние', 'Источник'].map((label) => <th key={label} scope="col">{label}</th>)}</tr></thead>
              <tbody>{filtered.map((doc) => {
                const scope = issueState.kind === 'ready' ? publicationScope(issueState.data, doc.document_id) : null;
                return <tr key={doc.document_id}>
                <td data-label="Документ"><strong>{publicationId(doc.document_id)}</strong></td>
                <td data-label="Публикация">{doc.title ?? doc.filename ?? '—'}</td>
                <td data-label="Страниц">{doc.page_count ?? '—'}</td>
                <td data-label="Состояние"><div className="document-status">{doc.pages_needing_ocr.length ? <Badge tone="warn">OCR: {doc.pages_needing_ocr.length} стр.</Badge>
                  : doc.processing_status === 'parsed_text' || provider.mode === 'mock' ? <Badge tone="info">Текст прочитан</Badge>
                    : <Badge>{doc.processing_status ?? 'Не указано'}</Badge>}
                  {scope && <Badge tone={scope.tone}>{scope.label}</Badge>}</div></td>
                <td data-label="Источник"><Link href={documentSourceUrl('documents', domain, doc.document_id, 1)} aria-label={`Открыть PDF ${publicationId(doc.document_id)}`}>Открыть PDF</Link></td>
              </tr>;
              })}</tbody>
            </table>
          </section>
          <section className="panel inspector"><h2>Состояние страниц</h2><InfoList items={[
            ['Трек', domainName[domain]], ['Документы', filtered.length],
            ['Страниц в очереди OCR', filtered.reduce((total, doc) => total + doc.pages_needing_ocr.length, 0)],
          ]} /></section>
        </div>;
      }}</LoadBoundary>
      <Ribbon title="Проверка источников">Состояние обработки страницы и статус научной записи хранятся отдельно.</Ribbon>
    </TrackTabs>
  </>;
}

const severity = { info: 'Информация', warning: 'Нужна проверка', error: 'Ошибка' } as const;

function IssueDetails({ issue, rows }: { issue: Issue; rows: Result[] }) {
  const related = issue.result_ids.map((id) => ({ id, result: rows.find((row) => row.result_id === id) }));
  const references = evidenceLinks(issue);
  const scope = references[0] && publicationScope([issue], references[0].documentId);
  return <section className="panel inspector issue-inspector" aria-labelledby="issue-title">
    <h2 id="issue-title">Причина замечания</h2>
    <Badge tone={scope?.tone ?? (issue.severity === 'warning' ? 'warn' : issue.severity)}>{scope?.label ?? severity[issue.severity]}</Badge>
    <p>{issue.reason}</p>
    <InfoList items={[
      ['Правило', checkLabel(issue)], ...checkValues(issue).filter(([label]) => !scope || label !== 'Наблюдение'),
    ]} />
    {(related.length > 0 || references.length > 0) && <div className="issue-sources">
      <h3>Связанные записи и источники</h3>
      {related.map(({ id, result }) => <div key={id} className="issue-source">
        <Link href={resultUrl(issue.domain, id)}>{result ? `${publicationId(result.document_id)} · ${result.metric.name}` : id}</Link>
        {result && <Link href={resultUrl(issue.domain, id, undefined, true)}>{sourceLabel(result.source)}</Link>}
      </div>)}
      <ReviewSources issue={issue} />
    </div>}
  </section>;
}

export function ChecksPage({ domain, selected }: { domain: Domain; selected: string | null }) {
  const load = useCallback(async (signal: AbortSignal) => {
    const [issues, rows] = await Promise.all([provider.issues(domain, signal), provider.results(domain, signal)]);
    return { issues, rows };
  }, [domain]);
  const data = useData(load);
  return <>
    <PageHeading title="Проверки" subtitle="Замечания, причины расхождений и ссылки на первоисточники." artwork />
    <TrackTabs section="checks" domain={domain}>
      <LoadBoundary {...data}>{({ issues, rows }) => {
        const chosen = issues.find((issue) => issue.issue_id === selected) ?? issues[0];
        if (!chosen) return <Feedback kind="empty" title="Для этого трека нет замечаний" />;
        return <div className="split checks-layout">
          <section className="panel" aria-labelledby="checks-heading">
            <header className="panel-heading"><h2 id="checks-heading">Очередь проверки</h2><p>Замечаний: {issues.length}</p></header>
            <ul className="review-list">{issues.map((issue) => <li key={issue.issue_id}>
              <Link href={sectionUrl('checks', domain, undefined, issue.issue_id)} aria-current={chosen.issue_id === issue.issue_id ? 'true' : undefined}>
                <Icon name="alert" /><span><strong>{checkLabel(issue)}</strong><small>{issue.reason}</small></span><Icon name="next" size={16} />
              </Link>
            </li>)}</ul>
          </section>
          <IssueDetails issue={chosen} rows={rows} />
        </div>;
      }}</LoadBoundary>
    </TrackTabs>
  </>;
}

function saveFile(file: Blob, filename: string) {
  const url = URL.createObjectURL(file);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

function Downloads({ domain }: { domain: Domain }) {
  const [pending, setPending] = useState<string>();
  const [feedback, setFeedback] = useState<{ error: boolean; message: string }>();
  const current = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => current.current?.abort(), []);

  async function download(filename: string, request: (signal: AbortSignal) => Promise<Blob>) {
    if (current.current) return;
    const controller = new AbortController();
    current.current = controller;
    setFeedback(undefined);
    setPending(filename);
    try {
      const blob = await request(controller.signal);
      if (!controller.signal.aborted) {
        saveFile(blob, filename);
        setFeedback({ error: false, message: `Скачивание ${filename} началось.` });
      }
    } catch (error) {
      if (!controller.signal.aborted) setFeedback({ error: true, message: `${filename} · ${errorMessage(error)}` });
    } finally {
      current.current = undefined;
      if (!controller.signal.aborted) setPending(undefined);
    }
  }

  const files = csvFiles[domain].map((name) => `${name}.csv`);
  return <div className="stack export-layout">
    {feedback && <div className={`download-status ${feedback.error ? 'error' : 'good'}`} role={feedback.error ? 'alert' : 'status'}>{feedback.message}</div>}
    <div className="split">
      <section className="panel export-panel">
        <h2>Комплект {domainName[domain]}</h2>
        <p className="muted">{provider.mode === 'mock' ? 'Демонстрационная выгрузка · 7 CSV в UTF-8' : 'Шаблон сдачи трека · 7 CSV в UTF-8'}</p>
        {!provider.csvAvailable && <Ribbon title="CSV-выгрузка пока недоступна">Подключите папку с файлами, сформированными экспортом бэкенда.</Ribbon>}
        <ul className="file-list">{files.map((name) => <li key={name}>
          <span><Icon name="file" size={16} />{name}</span>
          {provider.csvAvailable && <button className="button" type="button" disabled={Boolean(pending)} aria-label={`Скачать ${name}`} onClick={() => void download(name, (signal) => provider.csv(domain, name, signal))}>
            <Icon name="down" size={16} />{pending === name ? 'Загрузка…' : 'Скачать'}
          </button>}
        </li>)}</ul>
      </section>
      <section className="panel export-panel">
        <h2>Паспорт и отчёты</h2>
        <p className="muted">Поля, связи, размерности и правила проверки данных.</p>
        <button type="button" className="button primary" disabled={Boolean(pending)} onClick={() => void download('passport.md', async () => new Blob([passport], { type: 'text/markdown;charset=utf-8' }))}>
          <Icon name="down" size={16} />Скачать паспорт данных
        </button>
        <hr />
        <p className="muted">Служебный JSON · оба трека</p>
        <div className="button-row">{(['results.json', 'issues.json'] as const).map((name) => <button key={name} type="button" className="button" disabled={Boolean(pending)}
          onClick={() => void download(name, (signal) => provider.exportJson(name, signal))}>{pending === name ? 'Загрузка…' : name}</button>)}</div>
      </section>
    </div>
  </div>;
}

export function ExportPage({ domain }: { domain: Domain }) {
  return <>
    <PageHeading title="Экспорт" subtitle="Таблицы данных, паспорт и результаты проверок." artwork />
    <TrackTabs section="export" domain={domain}><Downloads key={domain} domain={domain} /></TrackTabs>
  </>;
}
