import type { ReactNode } from 'react';

export function Note({ title, children, warning = false }: { title: string; children: ReactNode; warning?: boolean }) {
  return (
    <aside className={`note${warning ? ' warning' : ''}`}>
      <strong>{title}</strong> <span>{children}</span>
    </aside>
  );
}
