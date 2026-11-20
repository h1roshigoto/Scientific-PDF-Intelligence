import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';

const region = (page: Page) => page.getByRole('region', { name: 'Паспорт проекта (PDF)' });
const downloadButton = (panel: Locator) => panel.getByRole('button', { name: 'Скачать паспорт проекта (PDF)' });

async function readReport(page: Page, suffix: string) {
  const pending = page.waitForEvent('download');
  await downloadButton(region(page)).click();
  const file = await pending;
  expect(file.suggestedFilename()).toBe(`scientific-pdf-intelligence_passport_demo_${suffix}.pdf`);
  const bytes = await readFile((await file.path())!);
  const loading = getDocument({ data: new Uint8Array(bytes), useSystemFonts: true });
  try {
    const pdf = await loading.promise;
    const pages: string[] = [];
    for (let index = 1; index <= pdf.numPages; index += 1) {
      const content = await (await pdf.getPage(index)).getTextContent();
      pages.push(content.items.map((item) => 'str' in item ? item.str : '').join(' '));
    }
    return pages;
  } finally {
    await loading.destroy();
  }
}

test('downloads the complete project by default and retains the CSV and scheme exports', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/export?domain=rag');
  const panel = region(page);
  await expect(panel.getByLabel('Область экспорта')).toHaveValue('all');
  await expect(panel.getByText(/Публикаций: 5 · Результатов: 8/)).toBeVisible();
  const pages = await readReport(page, 'all');
  expect(pages.length).toBeGreaterThan(1);
  const text = pages.join(' ');
  expect(text).toContain('Паспорт проекта');
  expect(text).toContain('1200 мВ');
  expect(text).toContain('0.250 fraction');
  expect(text).toContain('Синтетические данные');
  expect(text).toContain('demo-solar-d006-scope');
  expect(text).toContain('Расхождение PCE');
  expect(text).toContain('Приложение не предоставлено');
  expect(text).toContain('Страница PDF: 6');
  expect(text).toContain('demo-rag-d002-sd');
  await expect(panel.getByRole('status')).toContainText('PDF готов');
  await expect(panel.locator('time')).toHaveAttribute('datetime', '2026-11-20T17:40:00Z');
  await expect(page.getByRole('button', { name: 'Скачать паспорт данных' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Скачать results.csv' })).toBeVisible();
  expect(await page.locator('html').evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0);
  await testInfo.attach('Экспорт паспорта на мобильном', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
});

test('exports single-track and single-publication scopes including excluded articles', async ({ page }) => {
  await page.goto('/export?domain=solar');
  const panel = region(page);
  const choice = panel.getByLabel('Область экспорта');
  await choice.selectOption('solar');
  await expect(panel.getByText(/Публикаций: 3 · Результатов: 4/)).toBeVisible();
  const solar = (await readReport(page, 'solar')).join(' ');
  expect(solar).toContain('demo-solar-d021-table');
  expect(solar).not.toContain('demo-rag-d001-em');

  await choice.selectOption('rag');
  await expect(panel.getByText(/Публикаций: 2 · Результатов: 4/)).toBeVisible();
  const rag = (await readReport(page, 'rag')).join(' ');
  expect(rag).toContain('demo-rag-d001-em');
  expect(rag).not.toContain('demo-solar-d001-voc');

  await choice.selectOption('publication');
  await expect(downloadButton(panel)).toBeDisabled();
  const publication = panel.getByRole('combobox', { name: 'Публикация', exact: true });
  await expect(publication.getByRole('option', { name: /^Solar \/ D001/ })).toHaveCount(1);
  await expect(publication.getByRole('option', { name: /^RAG \/ D001/ })).toHaveCount(1);
  await publication.selectOption('rag:D001');
  const article = (await readReport(page, 'rag_D001')).join(' ');
  expect(article).toContain('RA-CoT');
  expect(article).toContain('HotpotQA');
  expect(article).not.toContain('solar:D001');
  expect(article).not.toContain('rag:D002');

  await publication.selectOption('solar:D006');
  await expect(panel.getByText(/Публикаций: 1 · Результатов: 0/)).toBeVisible();
  const excluded = (await readReport(page, 'solar_D006')).join(' ');
  expect(excluded).toContain('Вне области кейса');
  expect(excluded).toContain('Device architecture: two-junction tandem');
  expect(excluded).toContain('Записей результатов: 0');
});

test('shows a pending and retryable failure on download without saving an error document', async ({ page }) => {
  const url = '**/demo/passports/*_all.pdf';
  let release: () => void;
  const unblock = new Promise<void>((resolve) => { release = resolve; });
  await page.route(url, async (route) => { await unblock; await route.fulfill({ status: 503 }); });
  await page.goto('/export?domain=solar');
  const panel = region(page);
  await downloadButton(panel).click();
  await expect(panel.getByRole('status')).toContainText('Формируется PDF');
  await expect(downloadButton(panel)).toBeDisabled();
  release!();
  await expect(panel.getByRole('alert')).toContainText('HTTP 503');
  await expect(downloadButton(panel)).toBeEnabled();
  await page.unroute(url);
  const pages = await readReport(page, 'all');
  expect(pages[0]).toContain('Паспорт проекта');
});
