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
const csvNames = [...new Set(profiles.flatMap((profile) => [...csvFiles[profile]]))];

function Downloads() {
  const [pending, setPending] = useState<string>();
  const [feedback, setFeedback] = useState<{ error: boolean; message: string }>();
  const current = useRef<AbortController | undefined>(undefined);

  useEffect(() => () => current.current?.abort(), []);

  async function download(filename: string, request: (signal: AbortSignal) => Promise<{ name: string; blob: Blob }[]>) {
    if (current.current) return;
    const controller = new AbortController();
    current.current = controller;
    setFeedback(undefined);
    setPending(filename);
    try {
      const files = await request(controller.signal);
      if (!controller.signal.aborted) {
        files.forEach((file) => saveFile(file.blob, file.name));
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

  function downloadCsv(name: string) {
    const filename = `${name}.csv`;
    const sources = profiles.filter((profile) => csvFiles[profile].some((entry) => entry === name));
    return download(filename, (signal) => Promise.all(sources.map(async (profile, index) => ({
      name: index === 0 ? filename : `${name}-${index + 1}.csv`,
      blob: await provider.csv(profile, filename, signal),
    }))));
  }

  return (
    <div className="stack export-layout">
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
            <h2>Файлы CSV</h2>
            <p className="muted">
              CSV формируется отдельным экспортёром. Варианты файлов скачиваются отдельно.
            </p>
          </div>
          {!provider.csvAvailable && (
            <Note title="CSV-выгрузка пока недоступна." warning>
              Подключенные файлы CSV появятся после публикации выгрузки.
            </Note>
          )}
          {provider.csvAvailable && <ul className="file-list">
            {csvNames.map((name) => (
              <li key={name}>
                <span className="file-name">{name}.csv</span>
                  <button
                    className="button"
                    type="button"
                    disabled={Boolean(pending)}
                    aria-label={`Скачать ${name}.csv`}
                    onClick={() => void downloadCsv(name)}
                  >
                    <Icon name="down" size={14} />
                    {pending === `${name}.csv` ? 'Загрузка…' : 'Скачать'}
                  </button>
              </li>
            ))}
          </ul>}
        </section>
        <section className="surface export-panel">
          <div className="panel-intro">
            <h2>Паспорт и отчёты</h2>
            <p className="muted">Поля, связи, размерности и правила проверки данных.</p>
          </div>
          <button
            type="button"
            className="button primary"
            disabled={Boolean(pending)}
            onClick={() =>
              void download(
                'passport.md',
                async () => [{ name: 'passport.md', blob: new Blob([passport], { type: 'text/markdown;charset=utf-8' }) }],
              )
            }
          >
            <Icon name="down" size={14} />
            Скачать паспорт данных
          </button>
          <hr />
          <p className="muted export-subhead">Архивный JSON</p>
          <p className="muted">Сохранённые файлы сервера могут отличаться от текущего списка результатов.</p>
          <div className="button-row">
            {(['results.json', 'issues.json'] as const).map((name) => (
              <button
                key={name}
                type="button"
                className="button"
                disabled={Boolean(pending)}
                onClick={() => void download(name, async (signal) => [{ name, blob: await provider.exportJson(name, signal) }])}
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
