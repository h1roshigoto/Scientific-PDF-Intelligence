import { expect, test } from '@playwright/test';

const title = 'Scientific PDF Intelligence';
const destinations = [
  ['Начать работу', '/results', 'Результаты'],
  ['Документы', '/documents', 'Документы'],
  ['Проверки', '/checks', 'Проверки'],
  ['Экспорт', '/export', 'Экспорт'],
] as const;
const desktop = { width: 1440, height: 900 };
const mobile = { width: 390, height: 844 };

test('shows the approved hero, universal content, example, cards and footer', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('response', (response) => {
    if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
  });
  await page.setViewportSize(desktop);
  await page.goto('/');

  await expect(page.getByRole('heading', { name: title, level: 1 })).toBeVisible();
  await expect(page.getByText('Находите показатели в научных PDF, изучайте контекст и сверяйте значения с первоисточником.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Начать работу' })).toBeVisible();
  const example = page.locator('figure').filter({ hasText: 'Значение из документа' });
  await expect(example).toContainText('PDF, стр. 5');
  await expect(example).toContainText('Исходное значение');
  await expect(page.getByRole('region', { name: 'Данные и первоисточники' }).getByRole('link')).toHaveCount(2);
  await expect(page.getByRole('region', { name: 'От результата к источнику' }).getByRole('heading', { level: 3 })).toHaveCount(3);
  await expect(page.getByText('CSV формируется отдельным экспортёром.')).toBeVisible();
  await expect(page.getByRole('contentinfo')).toContainText('Научные PDF');
  await expect(page.locator('body')).not.toContainText(/\b(?:Solar|RAG|Demo)\b|демонстрацион|два направления|две области/i);
  expect(errors).toEqual([]);
});

for (const [device, size] of [['desktop', desktop], ['mobile', mobile]] as const) {
  for (const [label, path, heading] of destinations) {
    test(`${device}: ${label} opens ${path}`, async ({ page }) => {
      await page.setViewportSize(size);
      await page.goto('/');
      const link = page.getByRole('link', { name: label, exact: true });
      await expect(link).toHaveAttribute('href', path);
      await link.click();
      await expect(page).toHaveURL((url) => url.pathname === path);
      await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible();
      await expect(page.getByRole('navigation', { name: 'Главная навигация' })).toBeVisible();
    });
  }
}

test('supports the homepage wordmark, keyboard activation and history with a legacy entry URL', async ({ page }) => {
  await page.setViewportSize(mobile);
  await page.goto('/?domain=rag');
  const cta = page.getByRole('link', { name: 'Начать работу' });
  await cta.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/results$/);
  await expect(page.getByRole('tablist')).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(/\/\?domain=rag$/);
  await expect(page.getByRole('heading', { name: title, level: 1 })).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL(/\/results$/);
  await expect(page.getByRole('heading', { name: 'Результаты', level: 1 })).toBeVisible();
  const brand = page.getByRole('link', { name: title, exact: true });
  await expect(brand).toHaveAttribute('href', '/');
  await brand.press('Enter');
  await expect(page.getByRole('heading', { name: title, level: 1 })).toBeVisible();
});

test('links both rail items and both feature cards to their working sections', async ({ page }) => {
  for (const [name, path] of [
    [/^Данные\s*Научные показатели/, '/results'],
    [/^Сверка\s*Страницы PDF/, '/documents'],
  ] as const) {
    for (let index = 0; index < 2; index += 1) {
      await page.goto('/');
      const links = page.getByRole('link', { name });
      await expect(links).toHaveCount(2);
      await links.nth(index).click();
      await expect(page).toHaveURL((url) => url.pathname === path);
    }
  }
});

test('opens and reloads the homepage and each real destination directly', async ({ page }) => {
  for (const [path, heading] of [['/', title], ...destinations.map(([, path, heading]) => [path, heading])]) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible();
    await page.reload();
    await expect(page).toHaveURL((url) => url.pathname === path);
    await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible();
  }
});

test('keeps the complete page and links within the viewport from 320px to ultrawide', async ({ page }) => {
  await page.goto('/');
  for (const width of [320, 360, 375, 390, 430, 640, 767, 768, 1024, 1280, 1440, 1920, 2560]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole('heading', { name: title, level: 1 })).toBeVisible();
    await expect(page.getByRole('contentinfo')).toBeVisible();
    for (const [label] of destinations) {
      const box = await page.getByRole('link', { name: label, exact: true }).boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    }
    expect(await page.locator('html').evaluate((element) => element.scrollWidth - element.clientWidth), `${width}px`).toBe(0);
  }
});
