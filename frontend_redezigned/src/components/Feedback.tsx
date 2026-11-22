import type { ReactNode } from 'react';
import type { LoadState } from '../hooks/useData';
import { Icon } from './Icon';

export function Feedback({
  kind,
  title,
  children,
  retry,
  action,
}: {
  kind: 'empty' | 'error' | 'loading';
  title: string;
  children?: ReactNode;
  retry?: () => void;
  action?: ReactNode;
}) {
  return (
    <div className={`feedback ${kind}`} role={kind === 'error' ? 'alert' : 'status'} aria-busy={kind === 'loading'}>
      <span className="feedback-symbol" aria-hidden="true">
        {kind === 'loading' ? <span className="feedback-spinner" /> : <Icon name={kind === 'error' ? 'alert' : 'file'} size={20} />}
      </span>
      <div className="feedback-body">
        <h2>{title}</h2>
        {children && <p>{children}</p>}
      </div>
      {retry && (
        <button className="button" type="button" onClick={retry}>
          Повторить
        </button>
      )}
      {action}
    </div>
  );
}

export function LoadBoundary<T>({
  state,
  retry,
  children,
}: {
  state: LoadState<T>;
  retry: () => void;
  children: (data: T) => ReactNode;
}) {
  if (state.kind === 'loading') return <Feedback kind="loading" title="Загрузка данных…" />;
  if (state.kind === 'error') {
    return (
      <Feedback kind="error" title="Не удалось загрузить данные" retry={retry}>
        {state.message}
      </Feedback>
    );
  }
  return children(state.data);
}
