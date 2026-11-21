import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import type { Download, Page } from '@playwright/test';
import fixture from '../../fixtures/demo.json' with { type: 'json' };

const apiUrl = 'http://127.0.0.1:4176';
const nav = (page: Page) => page.getByRole('navigation', { name: 'Главная навигация' });
const inspector = (page: Page) => page.getByRole('region', { name: 'Первоисточник' });
const openVoc = (page: Page) => page.getByRole('button', { name: /D001 · Voc/ }).click();
const openEm = (page: Page) => page.getByRole('button', { name: /D001 · EM/ }).click();

async function checkPdf(page: Page, pageNumber: number, expected: string) {
  await expect(page.getByRole('img', { name: new RegExp(`страница ${pageNumber}$`) })).toHaveAttribute('data-rendered-page', String(pageNumber));
  await expect(page.getByTestId('pdf-page-text')).toContainText(expected);
  await expect(page.getByRole('link', { name: 'Открыть PDF в новой вкладке' })).toHaveAttribute('href', new RegExp(`#page=${pageNumber}$`));
}

async function noOverflow(page: Page) {
  expect(await page.locator('html').evaluate((root) => root.scrollWidth - root.clientWidth)).toBeLessThanOrEqual(0);
}

test('E2E-01 launches the local test dataset with clean requests and a homepage wordmark', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('requestfailed', (request) => errors.push(request.url()));
  page.on('response', (response) => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`); });
  await page.goto('/results');
  await expect(page.getByRole('heading', { name: 'Результаты', exact: true })).toBeVisible();
  await expect(page.getByText('Локальные данные', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /D001 · Voc/ })).toBeVisible();
  const wordmark = page.getByRole('link', { name: 'Scientific PDF Intelligence', exact: true });
  await expect(wordmark).toBeVisible();
  await expect(wordmark).toHaveAttribute('href', '/');
  await page.evaluate(() => document.fonts.ready);
  expect(errors).toEqual([]);
});

test('E2E-02 selects Solar Voc and preserves search on return from detail', async ({ page }) => {
  await page.goto('/results?domain=solar');
  await page.getByRole('searchbox').fill('D001');
  await page.getByRole('combobox', { name: 'Метрика' }).selectOption('Voc');
  await openVoc(page);
  await expect(inspector(page).getByTestId('summary-raw')).toContainText('1200 мВ');
  await expect(inspector(page).getByTestId('summary-value')).toHaveText('1,2 В');
  await inspector(page).getByRole('link', { name: 'Открыть детали' }).click();
  await expect(page.getByRole('heading', { name: 'D001', exact: true })).toBeVisible();
  await expect(page.getByTestId('raw-value')).toHaveText('1200 мВ');
  await expect(page.getByTestId('normalized-value')).toHaveText('1,2 В');
  await page.reload();
  await expect(page.getByTestId('raw-value')).toHaveText('1200 мВ');
  await page.getByRole('link', { name: 'К результатам' }).click();
  await expect(page.getByRole('searchbox')).toHaveValue('D001');
  await expect(page.getByRole('combobox', { name: 'Метрика' })).toHaveValue('Voc');
});

test('E2E-03 renders the actual Solar D001 PDF page 5', async ({ page }) => {
  await page.goto('/results');
  await openVoc(page);
  await inspector(page).getByRole('link', { name: 'Страница PDF' }).click();
  await checkPdf(page, 5, 'SOLAR D001 - Page 5');
  await expect(page.getByTestId('pdf-page-text')).toContainText('Pristine | Voc | 1200 mV');
  await page.getByRole('button', { name: 'Предыдущая страница' }).click();
  await checkPdf(page, 4, 'SOLAR D001 - Page 4');
  await page.getByRole('button', { name: 'К странице источника' }).click();
  await checkPdf(page, 5, 'SOLAR D001 - Page 5');
});

test('E2E-04 shows RAG EM with its method, dataset and missing normalization on F1', async ({ page }) => {
  await page.goto('/results');
  await expect(page.getByRole('row', { name: /D001.*F1/ }).getByTestId('normalized-cell')).toHaveText('—');
  await openEm(page);
  await inspector(page).getByRole('link', { name: 'Открыть детали' }).click();
  await expect(page.getByRole('heading', { name: 'D001', exact: true })).toBeVisible();
  await expect(page.getByText('HotpotQA', { exact: true })).toBeVisible();
  await expect(page.getByTestId('raw-value')).toHaveText('0.250 fraction');
  await expect(page.getByTestId('normalized-value')).toHaveText('0,25 fraction');
});

test('E2E-05 renders RAG D001 PDF page 6 and never substitutes Solar D001', async ({ page }) => {
  await page.goto('/results?domain=rag');
  await openEm(page);
  const request = page.waitForResponse((response) => response.url().endsWith('/demo/rag-D001.pdf'));
  await inspector(page).getByRole('link', { name: 'Страница PDF' }).click();
  expect((await request).status()).toBe(200);
  await checkPdf(page, 6, 'RAG D001 - Page 6');
  await expect(page.getByTestId('pdf-page-text')).toContainText('RA-CoT | EM | 0.250');
  await expect(page.getByTestId('pdf-page-text')).not.toContainText('SOLAR');
  await page.reload();
  await checkPdf(page, 6, 'RAG D001 - Page 6');
});

test('E2E-06 filters the shared result list by either scientific context', async ({ page }) => {
  await page.goto('/results');
  const voc = page.getByRole('button', { name: /D001 · Voc/ });
  const em = page.getByRole('button', { name: /D001 · EM/ });
  const context = page.getByRole('combobox', { name: 'Контекст' });
  await expect(voc).toBeVisible();
  await expect(em).toBeVisible();
  await expect(page.getByRole('tablist')).toHaveCount(0);
  await context.selectOption('Pristine');
  await expect(voc).toBeVisible();
  await expect(em).toHaveCount(0);
  await context.selectOption('HotpotQA');
  await expect(em).toBeVisible();
  await expect(voc).toHaveCount(0);
});

test('E2E-07 distinguishes empty API data from empty filtered results', async ({ page }) => {
  await page.goto('/results');
  await page.getByRole('searchbox').fill('no-matching-document');
  await expect(page.getByRole('heading', { name: 'По фильтрам ничего не найдено' })).toBeVisible();
  await page.getByRole('button', { name: 'Сбросить' }).click();
  await expect(page.getByRole('button', { name: /D001 · Voc/ })).toBeVisible();
  await page.route(/\/api\/(documents|results|issues)$/, (route) => route.fulfill({ json: [] }));
  await page.goto(`${apiUrl}/results?domain=rag`);
  await expect(page.getByRole('heading', { name: 'Пока нет результатов' })).toBeVisible();
  await expect(page.getByText('Локальные данные', { exact: true })).toHaveCount(0);
});

test('E2E-08/12 handles server errors and recovers without falling back to fixtures', async ({ page }) => {
  let ready = false;
  await page.route('**/api/documents', (route) => route.fulfill({ json: fixture.documents }));
  await page.route('**/api/issues', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/results', (route) => ready ? route.fulfill({ json: [fixture.results[1]] }) : route.fulfill({ status: 503 }));
  await page.goto(`${apiUrl}/results?domain=solar`);
  await expect(page.getByRole('alert')).toContainText('HTTP 503');
  await expect(page.getByRole('table')).toHaveCount(0);
  ready = true;
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByRole('button', { name: /D001 · Voc/ })).toBeVisible();
  await expect(page.getByRole('tablist')).toHaveCount(0);
});

test('reports malformed responses and unknown documents without a fallback or a bogus download', async ({ page }) => {
  const downloads: Download[] = [];
  page.on('download', (download) => downloads.push(download));
  await page.route('**/api/results', (route) => route.fulfill({ contentType: 'application/json', body: '{' }));
  await page.route('**/api/documents', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/issues', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/export/results.json', (route) => route.fulfill({ status: 503 }));

  await page.goto(`${apiUrl}/results`);
  await expect(page.getByRole('alert')).toContainText('некорректный JSON');
  await expect(page.getByRole('table')).toHaveCount(0);
  await page.goto(`${apiUrl}/documents/rag%3Amissing/source?page=1`);
  await expect(page.getByRole('alert')).toContainText('Документ отсутствует в реестре');
  await expect(page.getByRole('region', { name: 'Просмотр PDF' })).toHaveCount(0);
  await page.goto(`${apiUrl}/export`);
  await page.getByRole('button', { name: 'results.json', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('HTTP 503');
  await expect(page.getByRole('button', { name: 'results.json', exact: true })).toBeEnabled();
  expect(downloads).toEqual([]);
});

test('E2E-09 downloads exporter-generated UTF-8 CSVs for both tracks', async ({ page }) => {
  await page.goto('/export?domain=solar');
  const downloads: Download[] = [];
  page.on('download', (download) => downloads.push(download));
  await page.getByRole('button', { name: 'Скачать results.csv', exact: true }).click();
  await expect.poll(() => downloads.length).toBe(2);
  for (const [index, contents] of ['demo-solar-d001-voc', 'demo-rag-d001-em'].entries()) {
    const download = downloads.find((file) => file.suggestedFilename() === (index ? 'results-2.csv' : 'results.csv'))!;
    const bytes = await readFile((await download.path())!);
    const csv = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    expect(csv.split('\r\n')[0]).toMatch(/^publication_id,result_id,/);
    expect(csv).toContain(contents);
    expect(csv).toContain('Синтетические данные');
  }
});

test('E2E-11 navigates the registry, checks, source links, passport and invalid URLs', async ({ page }) => {
  await page.goto('/results');
  await nav(page).getByRole('link', { name: 'Документы' }).click();
  await expect(page.getByText('OCR: 1 стр.', { exact: true })).toBeVisible();
  await page.getByRole('row').filter({ hasText: fixture.documents[3].title }).getByRole('link', { name: 'Открыть PDF D001' }).click();
  await checkPdf(page, 1, 'RAG D001 - Page 1');
  await nav(page).getByRole('link', { name: 'Проверки' }).click();
  await expect(page.getByText(/Сверьте условия измерения B4/).last()).toBeVisible();
  await page.getByRole('link', { name: 'стр. 13 · Fig. 10' }).click();
  await checkPdf(page, 13, 'SOLAR D021 - Page 13');
  await nav(page).getByRole('link', { name: 'Экспорт' }).click();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Скачать паспорт данных' }).click();
  expect((await download).suggestedFilename()).toBe('passport.md');
  await page.goto('/this-does-not-exist');
  await expect(page.getByRole('heading', { name: 'Страница не найдена' })).toBeVisible();
});

test('PDF and CSV recover from failed requests in API mode', async ({ page }) => {
  let available = false;
  await page.route('**/api/results/demo-rag-d001-em', (route) => route.fulfill({ json: fixture.results[4] }));
  await page.route('**/api/documents/rag%3AD001/file', (route) => available
    ? route.fulfill({ contentType: 'application/pdf', path: 'public/demo/rag-D001.pdf' }) : route.fulfill({ status: 404 }));
  await page.goto(`${apiUrl}/results/demo-rag-d001-em/source?domain=rag`);
  await expect(page.getByRole('alert')).toContainText('Не удалось открыть PDF');
  available = true;
  await page.getByRole('button', { name: 'Повторить' }).click();
  await checkPdf(page, 6, 'RAG D001 - Page 6');
  await page.route('**/fixture-csv/rag/results.csv', (route) => route.fulfill({ status: 502 }));
  await page.route('**/fixture-csv/solar/results.csv', (route) => route.fulfill({ body: 'document,value\r\nD001,1.2\r\n' }));
  await page.goto(`${apiUrl}/export?domain=rag`);
  await page.getByRole('button', { name: 'Скачать results.csv', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('HTTP 502');
  await expect(page.getByRole('button', { name: 'Скачать results.csv', exact: true })).toBeEnabled();
});

test('invalid PDF page fails explicitly instead of displaying a different page', async ({ page }) => {
  await page.goto('/documents/solar%3AD001/source?domain=solar&page=6');
  await expect(page.getByRole('alert')).toContainText('Запрошенная страница 6 отсутствует');
  await expect(page.getByRole('img', { name: /страница/ })).toHaveCount(0);
});

const viewports = [[1440, 930], [1440, 900], [1280, 800], [768, 1024], [390, 844], [320, 640]] as const;

for (const [width, height] of viewports) {
  test(`E2E-10 layout and PDF on ${width}×${height}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height });
    await page.goto('/results');
    await openVoc(page);
    await expect(inspector(page)).toBeVisible();
    await noOverflow(page);
    await page.evaluate(() => document.fonts.ready);
    const list = testInfo.outputPath(`results-${width}x${height}.png`);
    await page.screenshot({ path: list, fullPage: true, animations: 'disabled' });
    await testInfo.attach('Результаты Solar', { path: list, contentType: 'image/png' });
    await inspector(page).getByRole('link', { name: 'Страница PDF' }).click();
    await checkPdf(page, 5, 'SOLAR D001 - Page 5');
    await noOverflow(page);
    const pdf = testInfo.outputPath(`source-${width}x${height}.png`);
    await page.screenshot({ path: pdf, fullPage: true, animations: 'disabled' });
    await testInfo.attach('Источник Solar', { path: pdf, contentType: 'image/png' });
  });
}
