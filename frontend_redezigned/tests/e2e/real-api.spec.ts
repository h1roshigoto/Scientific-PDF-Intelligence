import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';
import { realApiUrl } from '../../playwright.config';

test.use({ baseURL: realApiUrl });

const passport = (page: Page) => page.getByRole('region', { name: 'Паспорт проекта (PDF)' });
const passportButton = (page: Page) => passport(page).getByRole('button', { name: 'Скачать паспорт проекта (PDF)' });

async function checkPdf(page: Page, number: number, track: string) {
  await expect(page.locator('canvas')).toHaveAttribute('data-rendered-page', String(number));
  await expect(page.getByTestId('pdf-page-text')).toHaveText(`Test only ${track} D001 page ${number}`);
}

async function readPassport(page: Page, scope: string) {
  const response = page.waitForResponse((reply) => new URL(reply.url()).pathname === '/api/export/project-passport.pdf');
  const download = page.waitForEvent('download');
  await passportButton(page).click();
  const reply = await response;
  expect(reply.ok()).toBe(true);
  expect(reply.headers()['content-type']).toBe('application/pdf');
  const filename = `scientific-pdf-intelligence_passport_playwright-e2e_${scope}.pdf`;
  expect(reply.headers()['content-disposition']).toBe(`attachment; filename="${filename}"`);
  const timestamp = reply.headers()['x-spi-generated-at'];
  expect(Number.isFinite(Date.parse(timestamp))).toBe(true);
  const file = await download;
  expect(file.suggestedFilename()).toBe(filename);
  const bytes = await readFile((await file.path())!);
  expect(bytes.subarray(0, 5).toString()).toBe('%PDF-');
  expect(bytes.subarray(-24).toString().trimEnd()).toMatch(/%%EOF$/);
  await expect(passport(page).locator('time')).toHaveAttribute('datetime', timestamp);
  const loading = getDocument({ data: new Uint8Array(bytes) });
  try {
    const pdf = await loading.promise;
    expect(pdf.numPages).toBeGreaterThan(1);
    const pages = [];
    for (let index = 1; index <= pdf.numPages; index += 1) {
      const content = await (await pdf.getPage(index)).getTextContent();
      pages.push(content.items.map((item) => 'str' in item ? item.str : '').join(' '));
    }
    return pages.join(' ');
  } finally {
    await loading.destroy();
  }
}

test('reads the live registry, results and evidence from FastAPI', async ({ page, request }) => {
  expect(await (await request.get('/api/health')).json()).toEqual({ status: 'ok' });
  await page.goto('/documents');
  await expect(page.getByText('Документов: 4')).toBeVisible();
  await page.getByRole('link', { name: 'Открыть PDF Solar D001' }).click();
  await checkPdf(page, 1, 'SOLAR');

  await page.goto('/results');
  await expect(page.getByText('Записей: 3')).toBeVisible();
  await expect(page.getByRole('row', { name: /D001.*F1/ })).toContainText('Извлечено');
  const metric = page.getByRole('combobox', { name: 'Метрика' });
  await metric.selectOption('Voc');
  const inspector = page.getByRole('region', { name: 'Первоисточник' });
  await inspector.getByRole('link', { name: 'Открыть детали' }).click();
  await expect(page.getByTestId('raw-value')).toHaveText('0 mV');
  await expect(page.getByTestId('normalized-value')).toHaveText('0 V');
  await expect(page.getByText('Первичный результат')).toBeVisible();
  await expect(page.getByText('Сверено', { exact: true })).toBeVisible();
  await expect(page.getByText('E2E cell', { exact: true })).toBeVisible();
  await checkPdf(page, 2, 'SOLAR');
  await page.getByRole('link', { name: 'К результатам' }).click();
  await expect(metric).toHaveValue('Voc');

  const context = page.getByRole('combobox', { name: 'Контекст' });
  const status = page.getByRole('combobox', { name: 'Статус' });
  await metric.selectOption('');
  await context.selectOption('E2E retrieval set');
  await status.selectOption('needs_review');
  await expect(page.getByText('Записей: 1')).toBeVisible();
  await inspector.getByRole('link', { name: 'Открыть детали' }).click();
  await expect(page.getByTestId('raw-value')).toHaveText('18.7 %');
  await expect(page.getByTestId('normalized-value')).toHaveText('—');
  await expect(page.getByText('Цитируемый результат · E2E-CITED')).toBeVisible();
  await checkPdf(page, 3, 'RAG');
  await page.getByRole('link', { name: 'Открыть страницу PDF' }).click();
  await checkPdf(page, 3, 'RAG');
  await page.getByRole('button', { name: 'Предыдущая страница' }).click();
  await checkPdf(page, 2, 'RAG');
  const open = page.getByRole('link', { name: 'Открыть PDF в новой вкладке' });
  await expect(open).toHaveAttribute('href', /^blob:.*#page=2$/);
  const popup = page.waitForEvent('popup');
  await open.click();
  await (await popup).close();
  await page.getByRole('link', { name: 'Назад к записи' }).click();
  await checkPdf(page, 3, 'RAG');
  await page.getByRole('link', { name: 'К результатам' }).click();
  await expect(context).toHaveValue('E2E retrieval set');
  await expect(status).toHaveValue('needs_review');
  await page.getByRole('link', { name: 'Проверки', exact: true }).click();
  await expect(page.getByText('Замечаний: 1')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Причина замечания' })).toContainText('Test only review evidence');
});

test('reports missing and damaged PDFs and renders the right source at 320px', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  for (const [id, message] of [
    ['solar%3AMISSING', 'Запрошенный файл или запись недоступны'],
    ['rag%3ACORRUPT', 'PDF повреждён'],
  ]) {
    await page.goto(`/documents/${id}/source?page=1`);
    await expect(page.getByRole('alert')).toContainText(message);
    await expect(page.getByRole('button', { name: 'Повторить' })).toBeVisible();
    await expect(page.locator('canvas')).toHaveCount(0);
  }
  await page.goto('/documents/rag%3AD001/source?page=1');
  await checkPdf(page, 1, 'RAG');
  expect(await page.locator('html').evaluate((root) => root.scrollWidth - root.clientWidth)).toBe(0);
  const stage = page.getByRole('region', { name: 'Содержимое страницы PDF' });
  expect(await stage.evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0);
});

test('downloads complete and scoped server passports and archives without publishing CSV', async ({ page, request }) => {
  await page.goto('/export');
  await expect(page.getByText('CSV-выгрузка пока недоступна.')).toBeVisible();
  await expect(page.getByRole('button', { name: /\.csv$/ })).toHaveCount(0);
  expect((await request.get('/api/export/results.csv')).status()).toBe(404);
  const full = await readPassport(page, 'all');
  for (const id of ['e2e-solar-zero', 'e2e-rag-review', 'e2e-rag-extracted', 'e2e-issue']) {
    expect(full).toContain(id);
  }
  await passport(page).getByLabel('Область экспорта').selectOption('publication');
  const publication = passport(page).getByRole('combobox', { name: 'Публикация', exact: true });
  await publication.selectOption('rag:D001');
  const rag = await readPassport(page, 'rag_D001');
  expect(rag).toContain('e2e-rag-review');
  expect(rag).not.toContain('solar:D001');
  await publication.selectOption('solar:D001');
  const solar = await readPassport(page, 'solar_D001');
  expect(solar).toContain('e2e-solar-zero');
  expect(solar).not.toContain('rag:D001');

  await expect(page.getByText('Архивный JSON')).toBeVisible();
  for (const [name, content] of [['results.json', 'e2e-solar-zero'], ['issues.json', 'e2e-issue'], ['passport.md', 'publication_id']]) {
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: name === 'passport.md' ? 'Скачать паспорт данных' : name, exact: true }).click();
    const file = await download;
    expect(file.suggestedFilename()).toBe(name);
    expect(await readFile((await file.path())!, 'utf8')).toContain(content);
  }
});

test('rejects a wrong run at FastAPI and retries failed frontend requests without fixtures', async ({ page }) => {
  await page.route('**/api/results', (route) => route.abort('connectionrefused'), { times: 1 });
  await page.goto('/results');
  await expect(page.getByRole('alert')).toContainText('Не удалось связаться с сервером');
  await expect(page.getByRole('table')).toHaveCount(0);
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('Записей: 3')).toBeVisible();

  const endpoint = '**/api/export/project-passport.pdf?*';
  await page.goto('/export');
  await page.route(endpoint, (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set('run_id', 'wrong-run');
    return route.continue({ url: url.href });
  }, { times: 1 });
  const invalidRun = page.waitForResponse((reply) => reply.url().includes('project-passport.pdf'));
  await passportButton(page).click();
  expect((await invalidRun).status()).toBe(404);
  await expect(passport(page).getByRole('alert')).toContainText('VITE_PROJECT_RUN_ID');

  // The backend regression suite produces genuine 500 and 503 responses.
  // Inject them here to check that the browser stays on the same form and retries.
  const downloads: string[] = [];
  page.on('download', (file) => downloads.push(file.suggestedFilename()));
  for (const code of [500, 503]) {
    await page.route(endpoint, (route) => route.fulfill({ status: code }), { times: 1 });
    await passportButton(page).click();
    await expect(passport(page).getByRole('alert')).toContainText(`HTTP ${code}`);
    await expect(passportButton(page)).toBeEnabled();
  }
  expect(downloads).toEqual([]);
  await readPassport(page, 'all');
});
