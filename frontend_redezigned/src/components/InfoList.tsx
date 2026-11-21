import type { ReactNode } from 'react';

export function InfoList({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="info-list">
      {items.map(([label, value], index) => (
        <div key={`${label}-${index}`}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
