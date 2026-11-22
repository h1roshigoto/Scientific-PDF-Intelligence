import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { createAccountClient, errorMessage } from '../data';
import type { AccountSession } from '../data';
import { useData } from '../hooks/useData';
import { LoadBoundary } from '../components/Feedback';
import { InfoList } from '../components/InfoList';
import { WorkspacePage } from '../components/Shell';

const date = new Intl.DateTimeFormat('ru', { dateStyle: 'medium', timeStyle: 'short' });
const number = new Intl.NumberFormat('ru', { maximumFractionDigits: 2 });
const size = (bytes: number) => `${number.format(bytes / 1024 ** 2)} МиБ`;
const account = createAccountClient();

export function SignIn({ onLogin }: { onLogin: (session: AccountSession) => void }) {
  const [registering, setRegistering] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const operation = useRef<AbortController>(null);
  useEffect(() => () => operation.current?.abort(), []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (operation.current) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    const email = String(fields.get('email'));
    const password = String(fields.get('password'));
    setError('');
    setNotice('');
    if (registering && password !== fields.get('confirm')) {
      setError('Пароли не совпадают.');
      return;
    }
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    try {
      if (registering) {
        await account.register(email, password, String(fields.get('name')), String(fields.get('code')), controller.signal);
        controller.signal.throwIfAborted();
        form.reset();
        setRegistering(false);
        setNotice('Аккаунт создан. Войдите с вашим email и паролем.');
      } else {
        const session = await account.login(email, password, controller.signal);
        controller.signal.throwIfAborted();
        form.reset();
        onLogin(session);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        for (const key of ['password', 'confirm', 'code']) {
          const input = form.elements.namedItem(key);
          if (input instanceof HTMLInputElement) input.value = '';
        }
        setError(errorMessage(cause));
      }
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <main className="main account-entry">
      <WorkspacePage title={registering ? 'Регистрация по приглашению' : 'Вход'} subtitle="Scientific PDF Intelligence">
        <section className="surface upload-panel">
          <div className="upload-content">
            <form key={String(registering)} className="upload-form account-form" onSubmit={(event) => void submit(event)}>
              {registering && <label>Имя<input name="name" autoComplete="name" required maxLength={80} /></label>}
              <label>Email<input name="email" type="email" autoComplete="username" required maxLength={254} /></label>
              <label>Пароль<input name="password" type="password" autoComplete={registering ? 'new-password' : 'current-password'}
                required minLength={registering ? 15 : 1} maxLength={128} /></label>
              {registering && <>
                <label>Подтверждение пароля<input name="confirm" type="password" autoComplete="new-password" required maxLength={128} /></label>
                <label>Код приглашения<input name="code" type="password" autoComplete="off" required minLength={43} maxLength={43} /></label>
              </>}
              <button className="button" disabled={busy}>{registering ? 'Создать аккаунт' : 'Войти'}</button>
            </form>
            {error && <p role="alert">{error}</p>}
            {notice && <p role="status">{notice}</p>}
            <div className="upload-actions">
              <button className="button quiet" type="button" disabled={busy} onClick={() => {
                setError('');
                setNotice('');
                setRegistering(!registering);
              }}>{registering ? 'Ко входу' : 'У меня есть приглашение'}</button>
            </div>
          </div>
        </section>
      </WorkspacePage>
    </main>
  );
}

type AccountClient = ReturnType<typeof createAccountClient>;

export function AccountPage({ session, api, onLogout }: {
  session: AccountSession; api: AccountClient; onLogout: () => void;
}) {
  const usage = useData(api.usage);
  const sessions = useData(api.sessions);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const operation = useRef<AbortController>(null);
  useEffect(() => () => operation.current?.abort(), []);

  async function revoke(id: string) {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setError('');
    try {
      await api.revoke(id, controller.signal);
      if (!controller.signal.aborted) sessions.retry();
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <WorkspacePage title="Аккаунт" subtitle={session.display_name}>
      <div className="stack">
        <section className="surface inspector">
          <h2>Профиль и лимиты</h2>
          <InfoList items={[['Email', session.email], ['Статус email', session.email_verified ? 'Подтверждён' : 'Ожидает подтверждения']]} />
          <LoadBoundary {...usage}>{(data) => <InfoList items={[
            ['Документы', `${data.documents} / ${data.limits.max_upload_files}`],
            ['Объём PDF', `${size(data.storage_bytes)} / ${size(data.limits.max_total_storage_bytes)}`],
            ['Размер одного PDF', size(data.limits.max_pdf_bytes)],
            ['Активные анализы', `${data.active_jobs} / ${data.limits.max_active_jobs}`],
            ['В очереди', `${data.queued_jobs} / ${data.limits.max_queued_jobs}`],
            ['Запросы к модели, осталось сегодня (UTC)', data.model_requests_remaining],
            ['Экспорты, осталось сегодня (UTC)', data.exports_remaining],
          ]} />}</LoadBoundary>
        </section>
        <section className="surface inspector">
          <h2>Активные сессии</h2>
          <LoadBoundary {...sessions}>{(rows) => (
            <ul className="upload-list">{rows.map((row) => <li key={row.session_id}>
              <div className="upload-record">
                <strong>{row.current ? 'Текущая сессия' : 'Другая сессия'}</strong>
                <p>Вход {date.format(row.created_at * 1000)} · Истекает {date.format(row.expires_at * 1000)}</p>
              </div>
              {row.current
                ? <button className="button" type="button" onClick={onLogout}>Завершить текущую сессию</button>
                : <button className="button" type="button" disabled={busy} onClick={() => void revoke(row.session_id)}>Отозвать</button>}
            </li>)}</ul>
          )}</LoadBoundary>
          {error && <p role="alert">{error}</p>}
        </section>
      </div>
    </WorkspacePage>
  );
}
