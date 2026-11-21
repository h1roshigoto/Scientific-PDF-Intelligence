import { formatValue } from '../domain';
import { useData } from '../hooks/useData';
import { Link, sectionUrl } from '../navigation';
import { provider } from '../provider';
import styles from './HomePage.module.css';

const features = [
  { label: 'Данные', title: 'Научные показатели', description: 'Исходные значения, единицы и условия измерений.', section: 'results' },
  { label: 'Сверка', title: 'Страницы PDF', description: 'Страницы и фрагменты для сверки результатов.', section: 'documents' },
] as const;

const steps = [
  { icon: styles.fileIcon, title: 'Откройте результаты', description: 'Найдите нужный показатель и изучите его контекст.' },
  { icon: styles.shieldIcon, title: 'Сверьте с PDF', description: 'Перейдите на страницу статьи и проверьте значение в контексте.' },
  { icon: styles.gridIcon, title: 'Экспорт данных', description: 'CSV формируется отдельным экспортёром.' },
];

const loadResults = (signal: AbortSignal) => provider.results(undefined, signal);

export function HomePage() {
  const { state, retry } = useData(loadResults);
  const featured = state.kind === 'ready' && state.data.find((row) =>
    row.normalized.value != null
    && formatValue({ ...row.normalized, unit: null }).length <= 6
    && (row.normalized.unit?.length ?? 0) <= 4
    && row.metric.name.length <= 22
    && formatValue(row.raw, true).length <= 24,
  ) || undefined;
  const caption = featured ? 'Значение из документа'
    : state.kind === 'error' ? 'Данные недоступны'
    : state.kind === 'loading' ? 'Загрузка…'
    : state.data.length ? 'Показатели доступны в результатах' : 'Пока нет результатов';
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link className={styles.wordmark} href="/">
            <span className={`${styles.icon} ${styles.fileIcon}`} aria-hidden="true" />
            <span>Scientific PDF Intelligence</span>
          </Link>
          <nav className={styles.nav} aria-label="Рабочее пространство">
            <Link href={sectionUrl('documents')}>Документы</Link>
            <Link href={sectionUrl('checks')}>Проверки</Link>
            <Link href={sectionUrl('export')}>Экспорт</Link>
          </nav>
        </div>
      </header>

      <main id="main-content" tabIndex={-1}>
        <section className={styles.hero} aria-labelledby="home-title">
          <div className={styles.heroInner}>
            <div className={styles.heroCopy}>
              <h1 id="home-title"><span>Scientific PDF</span>{' '}<span>Intelligence</span></h1>
              <p>Находите показатели в научных PDF, изучайте контекст и сверяйте значения с первоисточником.</p>
              <Link className={styles.cta} href={sectionUrl('results')}>
                Начать работу <span className={`${styles.icon} ${styles.nextIcon}`} aria-hidden="true" />
              </Link>
            </div>
            <div className={styles.trace}>
              <div className={styles.traceSheets} aria-hidden="true"><span /><span /><span /></div>
              <figure className={styles.traceCard}>
                <div className={styles.traceTop}><span>{featured?.metric.name ?? 'Показатель'}</span><span>{featured ? `PDF, стр. ${featured.source.page}` : 'Источник'}</span></div>
                <div className={styles.traceValue}><strong>{featured ? formatValue({ ...featured.normalized, unit: null }) : '—'}</strong><span>{featured?.normalized.unit}</span></div>
                <div className={styles.traceOriginal}>Исходное значение {featured ? <><mark>{formatValue({ ...featured.raw, unit: null }, true)}</mark> {featured.raw.unit}</> : '—'}</div>
                <figcaption aria-live="polite">
                  {caption}
                  {state.kind === 'error' && (
                    <button type="button" className={styles.retry} onClick={retry}>Повторить</button>
                  )}
                </figcaption>
              </figure>
            </div>
          </div>
          <div className={styles.rail}>
            {features.map(({ label, title, section }) => (
              <Link key={section} href={sectionUrl(section)}><b>{label}</b><span>{title}</span></Link>
            ))}
          </div>
        </section>

        <section className={styles.lower} aria-labelledby="home-features">
          <h2 id="home-features">Данные и первоисточники</h2>
          <div className={styles.features}>
            {features.map(({ label, title, description, section }) => (
              <Link className={styles.feature} key={section} href={sectionUrl(section)}>
                <span className={styles.featureLabel}>{label}</span>
                <h3>{title}</h3>
                <p>{description}</p>
              </Link>
            ))}
          </div>
        </section>

        <section className={styles.lower} aria-labelledby="home-workflow">
          <h2 id="home-workflow">От результата к источнику</h2>
          <div className={styles.steps}>
            {steps.map(({ icon, title, description }) => (
              <div className={styles.step} key={title}>
                <span className={styles.iconBox} aria-hidden="true"><span className={`${styles.icon} ${icon}`} /></span>
                <h3>{title}</h3>
                <p>{description}</p>
              </div>
            ))}
          </div>
        </section>
      </main>

      <footer className={styles.footer}>
        <div><span>Scientific PDF Intelligence</span><span>Научные PDF</span></div>
      </footer>
    </div>
  );
}
