import { provider } from '../provider';
import { useData } from './useData';

const load = (signal: AbortSignal) => provider.issues(undefined, signal);

export function useReviewIssues() {
  return useData(load);
}
