import { expect, test } from '@playwright/test';
import { apiUrl } from '../../playwright.config';

test('filters conditions and warnings and displays the cited page alongside the result', async ({ page }) => {
  await page.goto('/results?domain=solar');
  await page.getByText('Условия и замечания', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Замечание' }).selectOption('pce_mismatch');
  const rows = page.getByRole('table').getByRole('row');
  await expect(rows).toHaveCount(2);
  await expect(rows.last()).toContainText('Расхождение PCE');
  await page.getByRole('button', { name: 'Сбросить' }).click();
  const conditions = page.getByRole('combobox', { name: 'Условия' });
  if (!await conditions.isVisible()) await page.getByText('Условия и замечания', { exact: true }).click();
  await conditions.selectOption({ label: 'Спектр: AM1.5G' });
  await expect(rows).toHaveCount(2);
  await page.getByRole('link', { name: 'Открыть детали' }).click();
  await expect(page.getByTestId('raw-value')).toHaveText('1200 мВ');
  await expect(page.getByTestId('pdf-page-text')).toContainText('SOLAR D001 - Page 5');
  await page.getByRole('link', { name: 'К результатам' }).click();
  await expect(conditions).toHaveValue('Спектр: AM1.5G');
});

test('reports that ingestion requires server support and keeps the registry source usable at 320px', async ({ page }) => {
  const filename = `${'LongScientificManuscriptWithoutWhitespace'.repeat(5)}.pdf`;
  const documentId = 'rag:registered-123';
  const uploadCalls: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.includes('/uploads')) uploadCalls.push(request.url());
  });
  await page.route('**/api/documents', (route) => route.fulfill({
    json: [{ document_id: documentId, domain: 'rag', filename, user_upload: true }],
  }));
  await page.route(/\/api\/(results|issues)(\?[^#]*)?$/, (route) => route.fulfill({ json: [] }));
  await page.route('**/api/documents/rag%3Aregistered-123/file', (route) => route.fulfill({
    contentType: 'application/pdf', path: 'public/demo/rag-D001.pdf',
  }));
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto(`${apiUrl}/results`);
  await page.getByText('Загрузить PDF', { exact: true }).click();
  await expect(page.getByText(/Сервер пока не принимает PDF/)).toBeVisible();
  await expect(page.locator('input[type="file"]')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Пока нет результатов' })).toBeVisible();
  await page.getByRole('link', { name: 'Документы', exact: true }).click();
  await page.getByRole('link', { name: `Открыть PDF ${filename}` }).click();
  await expect(page.getByTestId('pdf-page-text')).toContainText('RAG D001 - Page 1');
  expect(await page.locator('html').evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0);
  expect(uploadCalls).toEqual([]);
});
