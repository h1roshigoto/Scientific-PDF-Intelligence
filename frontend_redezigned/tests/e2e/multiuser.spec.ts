import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { multiuserUrl } from '../../playwright.config';

test.use({ baseURL: multiuserUrl, trace: 'off' });
const password = 'Synthetic-Kestrel-Cloud!930';
const panel = (page: Page) => page.getByRole('region', { name: 'Мои загрузки' });

async function register(page: Page, name: string, code: string) {
  await page.getByRole('button', { name: 'У меня есть приглашение' }).click();
  await page.getByLabel('Имя', { exact: true }).fill(name);
  await page.getByLabel('Email', { exact: true }).fill(`${name}@example.test`);
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByLabel('Подтверждение пароля').fill(password);
  await page.getByLabel('Код приглашения').fill(code);
  await page.getByRole('button', { name: 'Создать аккаунт' }).click();
  await expect(page.getByText('Аккаунт создан.', { exact: false })).toBeVisible();
}

async function login(page: Page, name: string) {
  await page.getByLabel('Email', { exact: true }).fill(`${name}@example.test`);
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(panel(page)).toBeVisible();
}

async function analyze(page: Page, domain: 'solar' | 'rag', name: string) {
  const fixture = await page.request.get(`/api/__test/${domain}.pdf`);
  expect(fixture.status()).toBe(200);
  const scope = panel(page);
  await scope.getByLabel('Файл PDF').setInputFiles({ name, mimeType: 'application/pdf', buffer: await fixture.body() });
  await scope.getByLabel('Направление').selectOption(domain);
  const uploaded = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/uploads'
    && response.request().method() === 'POST');
  await scope.getByRole('button', { name: 'Загрузить PDF' }).click();
  const response = await uploaded;
  expect(response.status()).toBe(201);
  const headers = await response.request().allHeaders();
  expect(headers.authorization).toBeUndefined();
  expect(headers['x-spi-csrf']).toMatch(/^[a-f0-9]{64}$/);
  const { upload_id: id } = await response.json();
  const row = scope.getByRole('listitem').filter({ hasText: name });
  await row.getByRole('button', { name: 'Начать анализ' }).click();
  await row.getByRole('link', { name: 'Посмотреть результаты' }).click();
  await expect(page.getByRole('list', { name: 'Научные записи' }).getByRole('listitem')).toHaveCount(domain === 'solar' ? 4 : 16);
  return id as string;
}

test('Alice and Bob independently register, analyze, review and export through session cookies', async ({ page, request }, info) => {
  test.setTimeout(70_000);
  const { alice, bob } = await (await request.get('/api/__test/invitations')).json();
  await page.goto('/documents');
  await register(page, 'Alice', alice);
  await register(page, 'Bob', bob);
  await login(page, 'Alice');
  expect((await page.request.get('/api/documents')).status()).toBe(404);
  const initialCookies = await page.context().cookies();
  const cookie = initialCookies.find((item) => item.name === 'spi-dev-session');
  expect(cookie?.httpOnly).toBe(true);
  expect((await page.request.post('/api/auth/logout', { headers: { Origin: 'https://foreign.example' } })).status()).toBe(403);
  expect((await page.request.post('/api/auth/logout', { headers: { Origin: multiuserUrl } })).status()).toBe(403);
  await page.reload();
  await expect(panel(page)).toBeVisible();
  await page.getByRole('link', { name: 'Scientific PDF Intelligence', exact: true }).click();
  await page.getByRole('link', { name: 'Загрузить PDF', exact: true }).click();
  await expect(panel(page)).toBeVisible();
  await expect(page.getByLabel('Токен доступа')).toHaveCount(0);
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie }));
  expect(storage).not.toContain(password);
  expect(storage).not.toContain(cookie?.value);

  const a = await analyze(page, 'solar', 'Alice Solar.pdf');
  const aInfo = page.url();
  const aBase = `/api/uploads/${a}`;
  const source = page.getByRole('list', { name: 'Научные записи' }).getByRole('listitem').filter({ hasText: '15.5 %' });
  await source.getByRole('link', { name: 'Открыть источник' }).click();
  await expect(page.getByTestId('pdf-page-text')).toContainText('Table 1');
  await expect(page.getByTestId('raw-value')).toHaveText('15.5 %');
  const review = page.getByRole('region', { name: 'Ручная проверка' });
  await review.getByLabel('Я сверил значение, единицу и контекст на странице PDF').check();
  await review.getByRole('button', { name: 'Подтвердить исходное' }).click();
  await expect(review.getByText('Подтверждено', { exact: true })).toBeVisible();
  const evidenceUrl = page.url();
  await page.getByRole('link', { name: 'К результатам загрузки' }).first().click();
  const downloads = page.getByRole('region', { name: 'Экспорт научного проекта' });
  const download = page.waitForEvent('download');
  await downloads.getByRole('button', { name: 'Скачать ZIP' }).click();
  const archive = info.outputPath('verified-alice.zip');
  await (await download).saveAs(archive);
  execFileSync('python', ['-m', 'spi.upload_export_verify', archive]);
  const hash = (await page.request.get(aBase + '/export/manifest')).headers()['x-spi-snapshot-sha256'];
  expect(hash).toMatch(/^[a-f0-9]{64}$/);

  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Вход' })).toBeVisible();
  await expect(page.getByText('Alice Solar.pdf')).toHaveCount(0);
  expect((await request.get('/api/auth/me', { headers: { Cookie: `${cookie?.name}=${cookie?.value}` } })).status()).toBe(401);
  await login(page, 'Bob');
  await expect(panel(page).getByText('Сохранённых PDF пока нет.')).toBeVisible();
  await page.goto(aInfo);
  await expect(page.getByText('Загрузка не найдена')).toBeVisible();
  expect((await page.request.get(aBase + '/file')).status()).toBe(404);
  expect((await page.request.get(aBase + '/export/project.zip', { headers: { 'X-SPI-Snapshot-SHA256': hash } })).status()).toBe(404);
  await page.getByRole('link', { name: 'Документы', exact: true }).click();
  const b = await analyze(page, 'rag', 'Bob RAG.pdf');
  const bBase = `/api/uploads/${b}`;
  const before = await (await page.request.get(bBase + '/results')).json();
  await page.getByRole('link', { name: 'Аккаунт', exact: true }).click();
  await expect(page.getByText('Bob@example.test')).toBeVisible();
  await expect(page.getByText('Активные сессии')).toBeVisible();
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await login(page, 'Alice');
  await expect(panel(page).getByText('Alice Solar.pdf')).toBeVisible();
  await expect(panel(page).getByText('Bob RAG.pdf')).toHaveCount(0);
  await page.goto(evidenceUrl);
  await expect(review.getByText('Подтверждено', { exact: true })).toBeVisible();
  expect((await page.request.get(bBase + '/results')).status()).toBe(404);
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await login(page, 'Bob');
  expect(await (await page.request.get(bBase + '/results')).json()).toEqual(before);
});
