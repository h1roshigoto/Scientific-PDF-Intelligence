import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { getDocument, GlobalWorkerOptions } from 'pdfjs-dist';
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { DataError, errorMessage } from '../data';
import { pdfPageUrl } from '../domain';
import type { Domain } from '../domain';
import { provider } from '../provider';
import { Feedback } from './Feedback';
import { Icon } from './Icon';

GlobalWorkerOptions.workerSrc = workerUrl;

type Pdf = { doc: PDFDocumentProxy; url: string };

function PdfPage({ pdf, page, label, onPageRendered }: {
  pdf: Pdf; page: number; label: string; onPageRendered?: (page: number) => void;
}) {
  const stage = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(0);
  const [rendered, setRendered] = useState<{ page: number; text: string }>();
  const [failure, setFailure] = useState<{ page: number; message: string }>();
  const [attempt, retry] = useState(0);

  useEffect(() => {
    const target = stage.current;
    if (!target) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const target = canvas.current;
    if (!target || !width) return;
    let active = true;
    let rendering: RenderTask | undefined;

    async function draw() {
      if (!target) return;
      const source = await pdf.doc.getPage(page);
      if (!active) return;
      const natural = source.getViewport({ scale: 1 });
      const available = Math.max(width - 24, 240);
      const scale = Math.min(Math.min(available, 760) / natural.width, 1600 / natural.height);
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const viewport = source.getViewport({ scale: scale * ratio });
      target.width = Math.ceil(viewport.width);
      target.height = Math.ceil(viewport.height);
      target.style.width = `${viewport.width / ratio}px`;
      target.style.height = `${viewport.height / ratio}px`;
      rendering = source.render({ canvas: target, viewport });
      await rendering.promise;
      const content = await source.getTextContent();
      if (active) {
        setFailure(undefined);
        setRendered({
          page,
          text: content.items.flatMap((item) => ('str' in item ? [item.str] : [])).join(' '),
        });
        onPageRendered?.(page);
      }
    }

    draw().catch(() => {
      if (active) {
        setFailure({
          page,
          message:
            'Страницу PDF не удалось отобразить. Попробуйте ещё раз или откройте файл в отдельной вкладке.',
        });
      }
    });

    return () => {
      active = false;
      rendering?.cancel();
    };
  }, [pdf, page, width, attempt, onPageRendered]);

  const ready = rendered?.page === page && failure?.page !== page;

  return (
    <div
      ref={stage}
      className="pdf-stage"
      role="region"
      aria-label="Содержимое страницы PDF"
      tabIndex={0}
      aria-busy={!ready && failure?.page !== page}
    >
      {failure?.page === page ? (
        <Feedback kind="error" title="Ошибка отображения" retry={() => retry((value) => value + 1)}>
          {failure.message}
        </Feedback>
      ) : (
        !ready && (
          <p className="pdf-loading" role="status">
            Отображаем страницу…
          </p>
        )
      )}
      <canvas
        ref={canvas}
        data-rendered-page={ready ? page : undefined}
        role="img"
        aria-label={`${label} · страница ${page}`}
        style={{ visibility: ready ? 'visible' : 'hidden' }}
      />
      {ready && rendered.text && (
        <span className="sr-only" data-testid="pdf-page-text">
          {rendered.text}
        </span>
      )}
    </div>
  );
}

export default function PdfViewer({
  domain,
  documentId,
  initialPage,
  label,
  highlightsResult = true,
  actions,
  loadPdf,
  onPageRendered,
}: {
  domain: Domain;
  documentId: string;
  initialPage: number;
  label: string;
  highlightsResult?: boolean;
  actions?: ReactNode;
  loadPdf?: (signal: AbortSignal) => Promise<Blob>;
  onPageRendered?: (page: number) => void;
}) {
  const [page, setPage] = useState(initialPage);
  const [attempt, retry] = useState(0);
  const [state, setState] = useState<{ attempt: number; pdf?: Pdf; error?: string }>();

  useEffect(() => {
    const controller = new AbortController();
    let url: string | undefined;
    let task: ReturnType<typeof getDocument> | undefined;

    async function load() {
      const blob = await (loadPdf ? loadPdf(controller.signal) : provider.pdf(domain, documentId, controller.signal));
      const data = await blob.arrayBuffer();
      controller.signal.throwIfAborted();
      task = getDocument({ data, useSystemFonts: true });
      const doc = await task.promise;
      controller.signal.throwIfAborted();
      if (initialPage > doc.numPages) {
        throw new DataError(
          `В файле ${doc.numPages} стр. Запрошенная страница ${initialPage} отсутствует.`,
        );
      }
      url = URL.createObjectURL(blob);
      setState({ attempt, pdf: { url, doc } });
    }

    load().catch((error: unknown) => {
      if (!controller.signal.aborted) {
        setState({
          attempt,
          error:
            error instanceof DataError
              ? errorMessage(error)
              : 'PDF повреждён, защищён паролем или недоступен.',
        });
      }
    });

    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
      void task?.destroy().catch(() => undefined);
    };
  }, [domain, documentId, initialPage, attempt, loadPdf]);

  const current = state?.attempt === attempt ? state : undefined;
  if (current?.error) {
    return (
      <Feedback kind="error" title="Не удалось открыть PDF" retry={() => retry((value) => value + 1)}>
        {current.error}
      </Feedback>
    );
  }
  if (!current?.pdf) return <Feedback kind="loading" title="Загрузка PDF…" />;

  const { pdf } = current;
  return (
    <section className="surface pdf-panel" aria-label="Просмотр PDF">
      <div className="pdf-toolbar">
        <div className="pager">
          <button
            className="icon-button"
            type="button"
            disabled={page === 1}
            aria-label="Предыдущая страница"
            onClick={() => setPage(page - 1)}
          >
            <Icon name="back" size={15} />
          </button>
          <h2 className="page-count num" aria-live="polite">
            Страница {page} <span>из {pdf.doc.numPages}</span>
          </h2>
          <button
            className="icon-button"
            type="button"
            disabled={page === pdf.doc.numPages}
            aria-label="Следующая страница"
            onClick={() => setPage(page + 1)}
          >
            <Icon name="next" size={15} />
          </button>
        </div>
        <div className="pdf-actions">
          {page === initialPage ? (
            <span className="source-mark">{highlightsResult ? 'Здесь найдено значение' : 'Страница просмотра'}</span>
          ) : (
            <button type="button" className="button quiet" onClick={() => setPage(initialPage)}>
              К странице источника
            </button>
          )}
          {actions}
          <a
            className="button"
            href={pdfPageUrl(pdf.url, page)}
            target="_blank"
            rel="noopener noreferrer"
          >
            <Icon name="open" size={14} />
            Открыть PDF в новой вкладке
          </a>
        </div>
      </div>
      <PdfPage pdf={pdf} page={page} label={label} onPageRendered={onPageRendered} />
    </section>
  );
}
