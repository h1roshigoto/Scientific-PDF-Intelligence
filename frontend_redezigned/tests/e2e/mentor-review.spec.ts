import { expect, test } from '@playwright/test';

test('eligibility remains separate from PCE and exclusions do not create result rows', async ({ page }) => {
  await page.goto('/documents?domain=solar');
  const excluded = page.getByRole('row', { name: /D006/ });
  await expect(excluded).toContainText('Вне области кейса');
  await excluded.getByRole('link', { name: 'Открыть PDF Solar D006' }).click();
  const review = page.getByRole('region', { name: 'Допуск и доступность данных' });
  await expect(review.getByText('Вне области кейса', { exact: true })).toBeVisible();
  await expect(review.getByText(/двухпереходный тандем/)).toBeVisible();
  await expect(review.getByText('Формула PCE сошлась')).toBeVisible();
  await review.getByRole('link', { name: /стр. 2 · Тип устройства/ }).click();
  await expect(page.getByTestId('pdf-page-text')).toContainText('Device architecture: two-junction tandem');
  await page.goto('/results?domain=solar&q=D006');
  await expect(page.getByText('По фильтрам ничего не найдено')).toBeVisible();
});

test('duplicate evidence links are retained, and PCE disagreement preserves the original value', async ({ page }) => {
  await page.goto('/results/demo-solar-d001-pce?domain=solar');
  const review = page.getByRole('region', { name: 'Проверки записи' });
  await expect(review.getByText('Подтверждённый повтор')).toBeVisible();
  const duplicate = review.getByRole('listitem').filter({ hasText: 'Подтверждённый повтор' });
  await expect(duplicate.getByRole('link', { name: /стр. 4 · Текст результатов/ })).toBeVisible();
  await expect(duplicate.getByRole('link', { name: /стр. 5 · Table 2/ })).toBeVisible();
  await duplicate.getByRole('link', { name: /стр. 4 · Текст результатов/ }).click();
  await expect(page.getByTestId('pdf-page-text')).toContainText('FB-SC reverse scan gave a PCE of 16.00%.');

  await page.goto('/results/demo-solar-d021-table?domain=solar');
  await expect(page.getByTestId('raw-value')).toHaveText('4.00 %');
  await expect(page.getByTestId('normalized-value')).toHaveText('4 %');
  await expect(review.getByText('Расчётный PCE')).toBeVisible();
  await expect(review.getByText('5,20 %', { exact: true })).toBeVisible();
  await expect(review.getByText(/Относительное отклонение 30 %/)).toBeVisible();
  await review.getByRole('link', { name: /стр. 12 · Входы расчёта/ }).click();
  await expect(page.getByTestId('pdf-page-text')).toContainText('B4 mean | Voc: 0.8 V');
});

test('shows layer order, measurement notes and distinct field availability states', async ({ page }, testInfo) => {
  await page.goto('/results/demo-solar-d001-voc?domain=solar');
  await page.getByText('Устройство, слои и измерение', { exact: true }).click();
  await expect(page.getByRole('heading', { name: /^Слой [12]$/ })).toHaveText(['Слой 1', 'Слой 2']);
  await expect(page.getByText('Центрифугирование', { exact: true })).toBeVisible();
  await expect(page.getByText('Подложка очищена перед нанесением.', { exact: true })).toBeVisible();
  await expect(page.getByText(/солнечный симулятор Demo-01/)).toBeVisible();
  const review = page.getByRole('region', { name: 'Проверки записи' });
  await expect(review.getByText('Не указано', { exact: true })).toBeVisible();
  await expect(review.getByText('Указано, но не извлечено')).toBeVisible();
  await expect(review.getByText('Приложение не предоставлено')).toBeVisible();
  await page.getByText('Устройство, слои и измерение', { exact: true }).click();
  const image = await page.screenshot({ fullPage: true });
  await testInfo.attach('Статусы доступности данных Solar', { body: image, contentType: 'image/png' });
  await review.getByRole('link', { name: /Упоминание приложения/ }).click();
  await expect(page.getByTestId('pdf-page-text')).toContainText('Supplementary Table S1.');
  await page.goto('/results/demo-rag-d002-mean?domain=rag');
  await expect(review.getByText('Наличие не установлено')).toBeVisible();
});
