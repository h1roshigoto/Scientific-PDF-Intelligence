import { createHash, randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { realApiUrl } from '../../playwright.config';

test.use({ baseURL: realApiUrl });

const endpoint = '/api/uploads';
const token = 'synthetic-playwright-token-0123456789';
const headers = { Authorization: `Bearer ${token}` };
const savedMessage = 'Файл сохранён.';
const panel = (page: Page) => page.getByRole('region', { name: 'Мои загрузки' });

async function syntheticPdf(request: APIRequestContext, domain: 'solar' | 'rag') {
  const response = await request.get(`/api/documents/${domain}%3AD001/file`);
  expect(response.ok()).toBe(true);
  return response.body();
}

async function signIn(page: Page, key = token) {
  const scope = panel(page);
  await scope.getByLabel('Токен доступа').fill(key);
  await scope.getByRole('button', { name: 'Войти' }).click();
}

test('posts a synthetic RAG PDF directly through Vite and downloads the original', async ({ request }) => {
  const pdf = await syntheticPdf(request, 'rag');
  const name = `Пример-${randomUUID()}.pdf`;
  const official = await (await request.get('/api/documents')).json();
  const response = await request.post(endpoint, {
    headers, multipart: { domain: 'rag', file: { name, mimeType: 'application/pdf', buffer: pdf } },
  });
  expect(response.status()).toBe(201);
  const entry = await response.json();
  expect(entry).toEqual({
    upload_id: expect.stringMatching(/^[a-f0-9]{32}$/),
    domain: 'rag', original_filename: name, page_count: 3, size_bytes: pdf.length,
    sha256: createHash('sha256').update(pdf).digest('hex'),
    status: 'stored', created_at: expect.any(String),
  });
  expect(await (await request.get(`${endpoint}/${entry.upload_id}`, { headers })).json()).toEqual(entry);
  const url = `${endpoint}/${entry.upload_id}/file`;
  expect((await request.get(url)).status()).toBe(401);
  const original = await request.get(url, { headers });
  expect(original.headers()['content-type']).toBe('application/pdf');
  expect(await original.body()).toEqual(pdf);
  expect(await (await request.get(endpoint, { headers })).json()).toContainEqual(entry);
  expect(await (await request.get('/api/documents')).json()).toEqual(official);
});

test('signs in, validates, retries, persists and opens an uploaded Solar PDF', async ({ page, request }) => {
  const pdf = await syntheticPdf(request, 'solar');
  const filename = `Solar-${randomUUID()}.pdf`;
  const uploadRequests: string[] = [];
  page.on('request', (event) => {
    if (new URL(event.url()).pathname.startsWith(endpoint)) uploadRequests.push(event.url());
  });
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto('/results');
  await page.getByRole('link', { name: 'Загрузить PDF' }).click();
  const scope = panel(page);
  await signIn(page, 'incorrect');
  await expect(scope.getByRole('alert')).toHaveText('Неверный токен доступа.');
  await signIn(page);
  const upload = scope.getByRole('button', { name: 'Загрузить PDF', exact: true });
  const picker = scope.getByLabel('Файл PDF');
  await expect(upload).toBeDisabled();
  await picker.setInputFiles({ name: 'oversize.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(16385) });
  await scope.getByLabel('Направление').selectOption('solar');
  await expect(scope.getByRole('alert')).toHaveText('Файл превышает допустимый размер.');
  await expect(upload).toBeDisabled();
  await picker.setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: pdf });
  await expect(scope.getByText(new RegExp(filename))).toBeVisible();
  await expect(upload).toBeEnabled();

  await page.route(`**${endpoint}`, (route) => route.fulfill({
    status: 500, json: { detail: 'upload_storage_error' },
  }), { times: 1 });
  await upload.click();
  await expect(scope.getByRole('alert')).toContainText('Сервер не смог выполнить операцию');
  await expect(upload).toBeEnabled();

  await page.route(`**${endpoint}`, (route) => route.fulfill({ status: 200, json: {} }), { times: 1 });
  await upload.click();
  await expect(scope.getByRole('alert')).toHaveText('Сервер не подтвердил сохранение PDF.');
  await expect(scope.getByRole('status').getByText(savedMessage)).toHaveCount(0);

  let resume!: () => void;
  const pending = new Promise<void>((resolve) => { resume = resolve; });
  await page.route(`**${endpoint}`, async (route) => {
    await pending;
    await route.continue();
  }, { times: 1 });
  const serverResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === endpoint && response.request().method() === 'POST');
  try {
    await upload.dblclick();
    await expect(scope.getByRole('button', { name: 'Отправляем PDF…' })).toBeDisabled();
    await expect(scope.getByRole('status').getByText(savedMessage)).toHaveCount(0);
  } finally {
    resume();
  }
  const response = await serverResponse;
  expect(response.status()).toBe(201);
  const entry = await response.json();
  expect(entry.sha256).toBe(createHash('sha256').update(pdf).digest('hex'));
  await expect(scope.getByRole('status')).toHaveText(savedMessage);

  const refresh = page.waitForResponse((reply) => new URL(reply.url()).pathname === endpoint
    && reply.request().method() === 'GET');
  await scope.getByRole('button', { name: 'Обновить список' }).click();
  expect((await refresh).status()).toBe(200);
  const row = scope.getByRole('listitem').filter({ hasText: filename });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText('Solar · 3 стр.');
  await expect(row).toContainText('stored');
  await expect(row.locator('time')).toHaveAttribute('datetime', entry.created_at);
  const original = page.waitForRequest((event) => new URL(event.url()).pathname === `${endpoint}/${entry.upload_id}/file`);
  await row.getByRole('button', { name: `Открыть оригинал ${filename}` }).click();
  expect((await (await original).allHeaders()).authorization).toBe(headers.Authorization);
  await expect(scope.getByTestId('pdf-page-text')).toHaveText('Test only SOLAR D001 page 1');
  await expect(scope.getByRole('link', { name: 'Открыть PDF в новой вкладке' })).toHaveAttribute('href', /^blob:.*#page=1$/);
  await expect(page.getByRole('region', { name: 'Реестр PDF' })).not.toContainText(filename);
  expect(await page.locator('html').evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0);

  const storage = await page.evaluate(() => JSON.stringify({
    local: { ...localStorage }, session: { ...sessionStorage }, cookies: document.cookie,
  }));
  expect(storage).not.toContain(token);
  expect([page.url(), ...uploadRequests].every((url) => !url.includes(token))).toBe(true);
  await page.reload();
  await expect(scope.getByLabel('Токен доступа')).toHaveValue('');
  await expect(row).toHaveCount(0);
  await signIn(page);
  await expect(row).toBeVisible();
});

test('runs explicit local analysis and reopens persisted PDF evidence after signing in again', async ({ page, request }) => {
  const fixture = await request.get('/api/__test/processing-fixture.pdf');
  expect(fixture.ok()).toBe(true);
  const pdf = await fixture.body();
  const filename = `Measurements-${randomUUID()}.pdf`;
  const official = await (await request.get('/api/results')).json();
  await page.goto('/documents');
  await signIn(page);
  const scope = panel(page);
  await scope.getByLabel('Файл PDF').setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: pdf });
  await scope.getByLabel('Направление').selectOption('solar');
  const uploaded = page.waitForResponse((reply) => new URL(reply.url()).pathname === endpoint
    && reply.request().method() === 'POST');
  await scope.getByRole('button', { name: 'Загрузить PDF', exact: true }).click();
  const upload = await uploaded;
  expect(upload.status()).toBe(201);
  const entry = await upload.json();
  expect(entry.status).toBe('stored');
  const base = `${endpoint}/${entry.upload_id}`;
  const row = scope.getByRole('listitem').filter({ hasText: filename });
  await expect(row).toContainText('Анализ не запущен');
  expect((await (await request.get(`${base}/processing`, { headers })).json()).status).toBe('not_started');

  const enqueued = page.waitForResponse((reply) => new URL(reply.url()).pathname === `${base}/process`);
  await row.getByRole('button', { name: 'Начать анализ' }).click();
  const job = await enqueued;
  expect(job.status()).toBe(202);
  expect(await job.json()).toMatchObject({ upload_id: entry.upload_id, status: 'queued' });
  expect((await job.request().allHeaders()).authorization).toBe(headers.Authorization);
  await expect(row).toContainText('В очереди');
  await expect(row).toContainText('Анализ завершён');
  await expect(row).toContainText('Результатов — 4');
  await row.getByRole('link', { name: 'Посмотреть результаты' }).click();
  const results = page.getByRole('list', { name: 'Научные записи' });
  await expect(results.getByRole('listitem')).toHaveCount(4);
  const pce = results.getByRole('listitem').filter({ hasText: '15.5 %' });
  await expect(pce).toContainText('PCE');
  await expect(pce).toContainText('Извлечено');
  const original = page.waitForRequest((event) => new URL(event.url()).pathname === `${base}/file`);
  await pce.getByRole('link', { name: 'Открыть источник' }).click();
  expect((await (await original).allHeaders()).authorization).toBe(headers.Authorization);
  await expect(page.getByTestId('raw-value')).toHaveText('15.5 %');
  await expect(page.locator('blockquote')).toHaveText('15.5');
  await expect(page.locator('canvas')).toHaveAttribute('data-rendered-page', '2');
  await expect(page.getByTestId('pdf-page-text')).toContainText('Table 1');
  expect((await request.get(`${base}/results`)).status()).toBe(401);
  expect(await (await request.get('/api/results')).json()).toEqual(official);

  await page.reload();
  await expect(page.getByLabel('Токен доступа')).toHaveValue('');
  await expect(page.getByTestId('raw-value')).toHaveCount(0);
  await page.getByLabel('Токен доступа').fill(token);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByTestId('raw-value')).toHaveText('15.5 %');
  await expect(page.locator('canvas')).toHaveAttribute('data-rendered-page', '2');
  await page.getByRole('link', { name: 'К результатам загрузки' }).first().click();
  await expect(page.getByText('Анализ завершён', { exact: true })).toBeVisible();
  await expect(results.getByRole('listitem')).toHaveCount(4);
});
