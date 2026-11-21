import { lazy, Suspense } from 'react';
import type { ComponentProps } from 'react';
import { Feedback } from './Feedback';

const PdfViewer = lazy(() => import('./PdfViewer'));

export function PdfPane(props: ComponentProps<typeof PdfViewer>) {
  return (
    <Suspense fallback={<Feedback kind="loading" title="Загрузка PDF…" />}>
      <PdfViewer {...props} />
    </Suspense>
  );
}
