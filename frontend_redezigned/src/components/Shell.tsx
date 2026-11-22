import type { ReactNode } from 'react';
import { Link, sectionUrl } from '../navigation';
import type { Section } from '../navigation';
import { Icon } from './Icon';
import type { IconName } from './Icon';

const navigation: { id: Section; label: string; icon: IconName }[] = [
  { id: 'results', label: 'Результаты', icon: 'grid' },
  { id: 'documents', label: 'Документы', icon: 'file' },
  { id: 'checks', label: 'Проверки', icon: 'shield' },
  { id: 'export', label: 'Экспорт', icon: 'down' },
];

export function Shell({
  section,
  mock,
  account,
  children,
}: {
  section: Section;
  mock: boolean;
  account?: { selected: boolean; onLogout: () => void };
  children: ReactNode;
}) {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        К содержимому
      </a>
      <header className="masthead">
        <div className="masthead-inner">
          <Link className="wordmark" href="/">
            <span className="wordmark-symbol"><Icon name="layers" size={19} /></span>
            <span>Scientific PDF Intelligence</span>
          </Link>
          <nav className="primary-nav" aria-label="Главная навигация">
            {(account ? navigation.filter(({ id }) => id === 'documents') : navigation).map((item) => (
              <Link
                key={item.id}
                href={sectionUrl(item.id)}
                aria-current={section === item.id && !account?.selected ? 'page' : undefined}
              >
                <Icon name={item.icon} size={15} />
                {item.label}
              </Link>
            ))}
            {account && <Link href="/account" aria-current={account.selected ? 'page' : undefined}>Аккаунт</Link>}
          </nav>
          <div className="masthead-meta">
            {account ? <button className="button quiet" type="button" onClick={account.onLogout}>Выйти</button>
              : import.meta.env.DEV && mock ? (
              <span
                className="data-flag"
                title="Изолированный режим разработки"
              >
                Локальные данные
              </span>
            ) : (
              <span className="data-flag live">Данные сервера</span>
            )}
          </div>
        </div>
      </header>
      <main id="main-content" className="main" tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}

export function PageHeading({
  title,
  subtitle,
  back,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  back?: { href: string; label: string };
  children?: ReactNode;
}) {
  return (
    <header className="page-heading">
      {back && (
        <Link className="back-link" href={back.href}>
          <Icon name="back" size={14} />
          <span>{back.label}</span>
        </Link>
      )}
      <div className="page-heading-row">
        <div className="page-heading-text">
          <h1>{title}</h1>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {children}
      </div>
    </header>
  );
}

export function WorkspacePage({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <>
      <PageHeading title={title} subtitle={subtitle}>{action}</PageHeading>
      <div>{children}</div>
    </>
  );
}
