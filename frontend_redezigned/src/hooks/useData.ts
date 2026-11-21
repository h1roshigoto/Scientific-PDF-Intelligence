import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '../data';

export type LoadState<T> =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; data: T };

export function useData<T>(load: (signal: AbortSignal) => Promise<T>) {
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  const [response, setResponse] = useState<{ load: typeof load; attempt: number; state: LoadState<T> }>();

  useEffect(() => {
    const controller = new AbortController();
    Promise.resolve()
      .then(() => load(controller.signal))
      .then(
        (data) => {
          if (!controller.signal.aborted) {
            setResponse({ load, attempt, state: { kind: 'ready', data } });
          }
        },
        (error: unknown) => {
          if (!controller.signal.aborted) {
            setResponse({ load, attempt, state: { kind: 'error', message: errorMessage(error) } });
          }
        },
      );
    return () => controller.abort();
  }, [load, attempt]);

  const state: LoadState<T> =
    response?.load === load && response.attempt === attempt ? response.state : { kind: 'loading' };

  return { state, retry };
}
