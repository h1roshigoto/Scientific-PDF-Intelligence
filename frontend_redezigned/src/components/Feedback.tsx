import type { ReactNode } from 'react';
import type { LoadState } from '../hooks/useData';

export function Feedback({
  kind,
  title,
  children,
  retry,
}: {
  kind: 'empty' | 'error' | 'loading';
  title: string;
  children?: ReactNode;
  retry?: () => void;
}) {
  return (
    <div className={`feedback ${kind}`} role={kind === 'error' ? 'alert' : 'status'} aria-busy={kind === 'loading'}>
      <div className="feedback-body">
        <h2>{title}</h2>
        {children && <p>{children}</p>}
      </div>
      {retry && (
        <button className="button" type="button" onClick={retry}>
          Повторить
        </button>
      )}
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
