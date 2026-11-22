import { useEffect, useRef, useState } from 'react';
import passport from '@passport?raw';
import { Icon } from '../components/Icon';
import { Note } from '../components/Note';
import { ProjectPassportPanel } from '../components/ProjectPassportPanel';
import { WorkspacePage } from '../components/Shell';
import { errorMessage } from '../data';
import { csvFiles } from '../domain';
import { saveFile } from '../download';
import { provider } from '../provider';

const profiles = ['solar', 'rag'] as const;

function Downloads() {
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
      if (!controller.signal.aborted) {
        setFeedback({ error: true, message: `${filename} · ${errorMessage(error)}` });
      }
    } finally {
      current.current = undefined;
      if (!controller.signal.aborted) setPending(undefined);
    }
  }

  return (
    <div className="stack export-layout">
      {pending && <p className="download-status" role="status"><span className="feedback-spinner" aria-hidden="true" />Загрузка {pending}…</p>}
      {feedback && (
        <div
          className={`download-status ${feedback.error ? 'error' : 'good'}`}
          role={feedback.error ? 'alert' : 'status'}
        >
          {feedback.message}
        </div>
      )}
      <div className="split export-grid">
        <section className="surface export-panel">
          <div className="panel-intro">
            <h2><Icon name="grid" size={19} />Файлы CSV</h2>
            <p className="muted">
              Таблицы для анализа данных Solar и RAG.
            </p>
          </div>
          {!provider.csvAvailable && (
            <Note title="CSV-выгрузка пока недоступна." warning>
              Подключенные файлы CSV появятся после публикации выгрузки.
            </Note>
          )}
          {provider.csvAvailable && profiles.map((profile) => (
            <div className="export-files" key={profile}>
              <h3>{profile === 'solar' ? 'Solar' : 'RAG'}</h3>
              <ul className="file-list">
                {csvFiles[profile].map((name) => {
              const source = `${name}.csv`;
              const filename = `${profile}-${source}`;
              return (
                <li key={filename}>
                  <span className="file-name"><Icon name="file" size={15} />{source}</span>
                  <button
                    className="button"
                    type="button"
                    disabled={Boolean(pending)}
                    aria-label={`Скачать ${filename}`}
                    onClick={() => void download(filename, (signal) => provider.csv(profile, source, signal))}
                  >
                    <Icon name="down" size={14} />
                    {pending === filename ? 'Загрузка…' : 'Скачать'}
                  </button>
                </li>
              );
                })}
              </ul>
            </div>
          ))}
        </section>
        <section className="surface export-panel">
          <div className="panel-intro">
            <h2><Icon name="file" size={19} />Паспорт и отчёты</h2>
            <p className="muted">Поля, связи, размерности и правила проверки данных.</p>
          </div>
          <button
            type="button"
            className="button primary"
            disabled={Boolean(pending)}
            onClick={() =>
              void download(
                'passport.md',
                async () => new Blob([passport], { type: 'text/markdown;charset=utf-8' }),
              )
            }
          >
            <Icon name="down" size={14} />
            Скачать паспорт данных
          </button>
          <hr />
          <h3 className="export-subhead">Архивный JSON</h3>
          <p className="muted">Сохранённые файлы сервера могут отличаться от текущего списка результатов.</p>
          <div className="button-row">
            {(['results.json', 'issues.json'] as const).map((name) => (
              <button
                key={name}
                type="button"
                className="button"
                disabled={Boolean(pending)}
                onClick={() => void download(name, (signal) => provider.exportJson(name, signal))}
              >
                {pending === name ? 'Загрузка…' : name}
              </button>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

export function ExportPage() {
  return (
    <WorkspacePage
      title="Экспорт"
      subtitle="Таблицы данных, паспорт и результаты проверок."
    >
      <div className="stack">
        <ProjectPassportPanel />
        <Downloads />
      </div>
    </WorkspacePage>
  );
}
