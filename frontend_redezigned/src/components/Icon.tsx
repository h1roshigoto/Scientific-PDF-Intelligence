import type { CSSProperties } from 'react';

export type IconName = 'down' | 'search' | 'next' | 'back' | 'open' | 'check' | 'alert' | 'grid' | 'file' | 'layers' | 'shield' | 'filter';

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  const style = {
    '--icon': `url(${import.meta.env.BASE_URL}assets/${name}.svg)`,
    '--icon-size': `${size}px`,
  } as CSSProperties;
  return <span className="icon" style={style} aria-hidden="true" />;
}
