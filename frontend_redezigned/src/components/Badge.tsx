import type { ReactNode } from 'react';
import { statusLabel } from '../domain';
import type { Status } from '../domain';

export type Tone = 'neutral' | 'info' | 'good' | 'warn' | 'error';

export const statusTone: Record<Status, Tone> = {
  verified: 'good',
  needs_review: 'warn',
  extracted: 'neutral',
};

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: Tone }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function StatusBadge({ status }: { status: Status }) {
  return <Badge tone={statusTone[status]}>{statusLabel[status]}</Badge>;
}
